#!/usr/bin/env python3
"""无人值守每日分析（headless，不启动 GUI）。

绕过 PyQt6 界面，直接复用项目底层组件跑「阶段一诊断 → 阶段二决策」完整流水线，
供 Windows 计划任务每天定时调用。

用法（在仓库根目录）：::

    .venv\\Scripts\\python.exe tools\\run_daily_analysis.py
    .venv\\Scripts\\python.exe tools\\run_daily_analysis.py --symbol 518880 --timeframe 1w --bars 100
    .venv\\Scripts\\python.exe tools\\run_daily_analysis.py --dry-run      # 只拉数据建快照，不调用 AI
    .venv\\Scripts\\python.exe tools\\run_daily_analysis.py --force        # K 线未更新也强制分析
    .venv\\Scripts\\python.exe tools\\run_daily_analysis.py --source eastmoney
    .venv\\Scripts\\python.exe tools\\run_daily_analysis.py --no-proxy

产物：
    records/pending/<时间>_<代码>_<周期>.json  完整分析记录（与 GUI 提交分析同一目录、同一格式）
    records/daily/<时间>_<代码>_<周期>.md      人读版摘要
    logs/pa_agent.log                          运行日志（含 API key 自动掩码）

退出码：
    0 = 正常结束（含「按规则跳过」）
    1 = 失败（数据源 / 网络 / 编排异常）
    2 = 配置错误（如未配置 API Key）
"""
from __future__ import annotations

import argparse
import ctypes
import glob
import json
import os
import re
import socket
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

# 计划任务/重定向场景下 stdout 未必是 UTF-8，中文日志会炸
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass

STATE_FILE = ROOT / "records" / "daily" / "_state.json"
DAILY_DIR = ROOT / "records" / "daily"

# Clash 内核进程名优先精确匹配。GUI 壳进程（如 "Clash for Windows.exe"）
# 也持有自己的监听端口（实测 2295），必须排除以免误选。
_CLASH_CORE_PATTERNS: tuple[str, ...] = (
    "clash-win64%",
    "clash-win32%",
    "clash-meta%",
    "clash-core%",
    "clash.exe",
    "mihomo%",
    "verge-mihomo%",
)

# 内核命名未知时的宽松兜底
_CLASH_ANY_PATTERNS: tuple[str, ...] = ("clash%", "%mihomo%", "%verge%")

# Clash 常见安装位置下的数据目录（config.yaml 所在处）
_CLASH_COMMON_DIRS: tuple[str, ...] = (
    r"C:\Program Files\Clash for Windows\data",
    r"C:\Program Files (x86)\Clash for Windows\data",
    r"C:\Program Files\Clash Verge\*",
    r"C:\Program Files\Clash Verge\*\*",
    r"%LOCALAPPDATA%\Programs\Clash for Windows\data",
    r"%LOCALAPPDATA%\clash-verge",
    r"%APPDATA%\clash",
    r"%USERPROFILE%\.config\clash",
    r"D:\Program Files\Clash.for.Windows-*\data",
    r"D:\Program Files\Clash for Windows\data",
    r"D:\Program Files\Clash Verge\*",
)

# mixed-port 是混合端口（HTTP+SOCKS）；老配置只有 port（HTTP）
_PORT_RE = re.compile(
    r"^\s*(?:mixed-port|port)\s*:\s*[\"']?(\d{2,5})",
    re.IGNORECASE | re.MULTILINE,
)

# 走代理的境外数据源（国内 A 股数据源直连更快）
_NEEDS_PROXY_SOURCES = frozenset({"tradingview", "yfinance", "mt5"})

# A 股时区（用于判断「今日日线是否已收盘」）
_CN_TZ = timezone(timedelta(hours=8))

# A 股日线收盘时刻（当日 15:00 前都算未收盘）
_A_SHARE_DAILY_CLOSE_MIN = 15 * 60


def _log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ── 代理 ──────────────────────────────────────────────────────────────────────


_AF_INET = 2
_TCP_TABLE_OWNER_PID_LISTENER = 3

# CONNECT 探测目标：代理能出网即可建立隧道
_PROBE_TARGET = "www.gstatic.com:443"


def _wmi_processes(patterns: tuple[str, ...]) -> list[tuple[int, str, str]]:
    """按进程名 LIKE 模式查询 [(pid, name, cmdline), ...]（经 WMI）。"""
    try:
        import win32com.client  # type: ignore[import]
    except Exception:  # noqa: BLE001  pywin32 未安装时静默跳过
        return []

    out: list[tuple[int, str, str]] = []
    try:
        wmi = win32com.client.GetObject("winmgmts:")
        where = " OR ".join(f"Name LIKE '{pat}'" for pat in patterns)
        for row in wmi.ExecQuery(
            f"SELECT Name, CommandLine, ProcessId FROM Win32_Process WHERE {where}"
        ):
            out.append(
                (
                    int(getattr(row, "ProcessId", 0) or 0),
                    str(getattr(row, "Name", "") or ""),
                    str(getattr(row, "CommandLine", "") or ""),
                )
            )
    except Exception:  # noqa: BLE001  WMI 不可用时走其它兜底
        return out
    return out


def _clash_processes() -> list[tuple[int, str, str]]:
    """Clash 进程列表：先精确匹配内核，无结果再宽松匹配。"""
    core = _wmi_processes(_CLASH_CORE_PATTERNS)
    return core or _wmi_processes(_CLASH_ANY_PATTERNS)


def _clash_dirs_from_processes() -> list[Path]:
    """从 Clash 进程命令行的 ``-d`` 参数解析生效的数据目录。

    注意：Clash 数据目录下的 ``config.yaml`` 才是生效配置；
    ``~/.config/clash/config.yaml`` 常是过期副本，不能当权威来源。
    """
    dirs: list[Path] = []
    for _pid, _name, cmdline in _clash_processes():
        match = re.search(r'-d\s+"([^"]+)"', cmdline) or re.search(r"-d\s+(\S+)", cmdline)
        if match:
            dirs.append(Path(match.group(1)))
    return dirs


def _listening_ports(pid: int) -> set[int]:
    """查该进程此刻正在 LISTEN 的 TCP 端口（iphlpapi，无需第三方依赖）。

    比配置文件更权威：配置写的是「打算监听什么」，这里拿到的是「实际在监听什么」。
    """
    if sys.platform != "win32" or not pid:
        return set()
    try:
        from ctypes import wintypes

        iphlpapi = ctypes.windll.iphlpapi  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return set()

    class _MibTcpRowOwnerPid(ctypes.Structure):
        _fields_ = [
            ("dwState", wintypes.DWORD),
            ("dwLocalAddr", wintypes.DWORD),
            ("dwLocalPort", wintypes.DWORD),
            ("dwRemoteAddr", wintypes.DWORD),
            ("dwRemotePort", wintypes.DWORD),
            ("dwOwningPid", wintypes.DWORD),
        ]

    size = wintypes.DWORD(0)
    iphlpapi.GetExtendedTcpTable(
        None, ctypes.byref(size), False, _AF_INET, _TCP_TABLE_OWNER_PID_LISTENER, 0
    )
    if not size.value:
        return set()
    buffer = ctypes.create_string_buffer(size.value)
    ret = iphlpapi.GetExtendedTcpTable(
        buffer, ctypes.byref(size), False, _AF_INET, _TCP_TABLE_OWNER_PID_LISTENER, 0
    )
    if ret != 0:
        return set()

    # 表头是一个 DWORD 条数，随后是连续的 MIB_TCPROW_OWNER_PID 数组
    count = ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD)).contents.value
    row_size = ctypes.sizeof(_MibTcpRowOwnerPid)
    base = ctypes.sizeof(wintypes.DWORD)
    ports: set[int] = set()
    for index in range(count):
        row = _MibTcpRowOwnerPid.from_buffer(buffer, base + index * row_size)
        if row.dwOwningPid == pid:
            ports.add(socket.ntohs(row.dwLocalPort & 0xFFFF))
    return ports


def _clash_listen_ports() -> set[int]:
    """Clash 进程实际在监听的全部 TCP 端口。"""
    ports: set[int] = set()
    for pid, _name, _cmdline in _clash_processes():
        ports |= _listening_ports(pid)
    return ports


def _is_http_proxy_port(port: int, timeout: float = 2.0) -> bool:
    """用 CONNECT 语义判断端口是不是 HTTP 代理。

    HTTP/mixed 代理会对 CONNECT 回 200（隧道建立）；
    Clash 的 external-controller（REST API）之类只会回 404/405。
    """
    request = (
        f"CONNECT {_PROBE_TARGET} HTTP/1.1\r\nHost: {_PROBE_TARGET}\r\n\r\n"
    ).encode("ascii")
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
            sock.sendall(request)
            head = sock.recv(64)
    except OSError:
        return False
    return head.startswith(b"HTTP/") and b" 200" in head


def _clash_data_dirs() -> list[Path]:
    """按可靠性收集候选 Clash 数据目录（去重且保持顺序）。"""
    dirs: list[Path] = []

    env_dir = (os.environ.get("PA_AGENT_CLASH_DIR") or "").strip()
    if env_dir:
        dirs.append(Path(env_dir))

    dirs.extend(_clash_dirs_from_processes())

    for pattern in _CLASH_COMMON_DIRS:
        expanded = os.path.expandvars(pattern)
        if any(ch in expanded for ch in "*?"):
            dirs.extend(Path(p) for p in glob.glob(expanded) if Path(p).is_dir())
        else:
            candidate = Path(expanded)
            if candidate.is_dir():
                dirs.append(candidate)

    seen: set[str] = set()
    unique: list[Path] = []
    for d in dirs:
        key = str(d).lower()
        if key not in seen:
            seen.add(key)
            unique.append(d)
    return unique


def _port_from_config(config_path: Path) -> int | None:
    """从 Clash config.yaml 读出 mixed-port（或老配置的 http port）。"""
    try:
        text = config_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    match = _PORT_RE.search(text)
    if not match:
        return None
    port = int(match.group(1))
    return port if 0 < port < 65536 else None


def _proxy_from_clash() -> str:
    """确定 Clash 的 HTTP 代理端口。

    先用「配置文件语义（mixed-port）+ 进程实际监听端口」交叉验证；
    配置读不到或对不上时，退化为在监听端口里做 CONNECT 探测。
    """
    listen = _clash_listen_ports()
    listen_note = "、".join(str(p) for p in sorted(listen)) if listen else "未知"

    for data_dir in _clash_data_dirs():
        for name in ("config.yaml", "config.yml"):
            config = data_dir / name
            if not config.is_file():
                continue
            port = _port_from_config(config)
            if not port:
                continue
            if listen and port not in listen:
                _log(f"{config} 里的 mixed-port={port} 实际未监听，忽略该配置")
                continue
            _log(
                f"代理来源：Clash 配置 {config}（mixed-port={port}；"
                f"进程监听 {listen_note}）"
            )
            return f"http://127.0.0.1:{port}"

    for port in sorted(listen):
        if _is_http_proxy_port(port):
            _log(f"代理来源：Clash 监听端口 CONNECT 探测（{port}；进程监听 {listen_note}）")
            return f"http://127.0.0.1:{port}"
    return ""


def _resolve_proxy(explicit: str) -> str:
    """返回可用的代理 URL；返回空串表示不走代理。

    优先级：显式参数 > 环境变量 > Clash（配置语义 × 进程实际监听端口）。
    显式传 ``none`` 表示强制直连，不再向下探查。
    """
    if explicit:
        return "" if explicit.lower() == "none" else explicit
    for key in ("PA_AGENT_PROXY", "HTTPS_PROXY", "https_proxy"):
        val = (os.environ.get(key) or "").strip()
        if val:
            return val

    return _proxy_from_clash()


def _apply_proxy(proxy: str) -> None:
    if not proxy:
        return
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        os.environ.setdefault(key, proxy)
    _log(f"代理已启用：{proxy}")


# ── 数据 ──────────────────────────────────────────────────────────────────────


def _fetch_frame(settings: argparse.Namespace | object, args: argparse.Namespace, logger) -> object:
    """连接数据源并构建「仅已收盘 K 线」分析快照，带外层重试。"""
    from pa_agent.data.base import DataSourceTransientError
    from pa_agent.data.factory import create_data_source, normalize_data_source_kind
    from pa_agent.data.snapshot import build_analysis_frame

    kind = normalize_data_source_kind(args.source or settings.general.last_data_source)
    last_exc: BaseException | None = None

    for attempt in range(1, args.fetch_retries + 1):
        source = None
        try:
            source = create_data_source(kind)
            source.connect()
            if hasattr(source, "set_exchange"):
                exchange = args.exchange or getattr(
                    settings.general, "last_tradingview_exchange", ""
                )
                source.set_exchange(exchange or "")
            source.subscribe(args.symbol, args.timeframe)

            bars = source.latest_snapshot(args.bars + 30)
            _log(f"数据源 {kind} 返回 {len(bars)} 根原始 K 线（含未收盘棒）")

            frame = build_analysis_frame(bars, args.bars, args.symbol, args.timeframe)
            if frame is None:
                raise DataSourceTransientError(
                    f"可用已收盘 K 线不足 {args.bars} 根，无法构建分析快照"
                )
            _log(
                f"分析快照：{frame.symbol} {frame.timeframe} {len(frame.bars)} 根已收盘 K 线，"
                f"K1 收盘价 {frame.bars[0].close}"
            )
            return frame
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.warning("拉取数据失败（第 %s/%s 次）：%s", attempt, args.fetch_retries, exc)
            if attempt < args.fetch_retries:
                time.sleep(args.fetch_retry_wait)
        finally:
            if source is not None:
                try:
                    source.disconnect()
                except Exception:  # noqa: BLE001
                    pass

    raise RuntimeError(f"数据获取失败，已重试 {args.fetch_retries} 次：{last_exc}") from last_exc


def _fingerprint(frame) -> dict:
    """K 线指纹：最新一根已收盘 K 线变了，才值得再花 token 分析。"""
    k1 = frame.bars[0]
    return {
        "symbol": frame.symbol,
        "timeframe": frame.timeframe,
        "bar_count": len(frame.bars),
        "k1_ts_open": int(k1.ts_open),
        "k1_close": float(k1.close),
    }


def _k1_is_today_unclosed(frame) -> bool:
    """K1 是否为「今天且尚未收盘」的 A 股日线。

    项目里 ``is_bar_still_forming`` 用「是否处于连续竞价时段（09:30-11:30 / 13:00-15:00）」
    判断日线是否收盘，午休（11:30-13:00）会误判为已收盘，从而把当日未收盘的
    日线当成 K1 送进分析。这里补一道独立防线：同一天且未到 15:00 → 视为未收盘。
    """
    if str(getattr(frame, "timeframe", "") or "").lower() != "1d":
        return False
    bars = getattr(frame, "bars", None)
    if not bars:
        return False
    from pa_agent.data.market_defaults import normalize_ashare_tv_code

    code = normalize_ashare_tv_code(getattr(frame, "symbol", "") or "")
    if not (len(code) == 6 and code.isdigit()):
        return False

    now_cn = datetime.now(_CN_TZ)
    k1_dt = datetime.fromtimestamp(int(bars[0].ts_open) / 1000, tz=_CN_TZ)
    if k1_dt.date() != now_cn.date():
        return False
    return (now_cn.hour * 60 + now_cn.minute) < _A_SHARE_DAILY_CLOSE_MIN


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_state(state: dict) -> None:
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ── 高周期背景（HTF）────────────────────────────────────────────────────────

# 低周期 → 应当参考的高周期
HTF_MAP: dict[str, str] = {"1h": "1d", "4h": "1d", "1d": "1w", "1w": "1M"}

# 高周期结论超过这个天数就不再作为背景（周线约每周更新一次）
HTF_MAX_AGE_DAYS = 14


def _format_htf_brief(s1: dict, s2: dict, *, htf_tf: str, source: Path, age_days: float) -> str:
    """把高周期分析结论浓缩成一段可注入提示词的背景文本。"""
    bar_analysis = s1.get("bar_analysis") or {}
    dec = (s2 or {}).get("decision") or {}
    lines = [
        "【高周期背景 · 由本系统最近一次同标的高周期分析提供】",
        f"- 周期：{htf_tf}（{age_days:.1f} 天前的分析，来自 {source.name}）",
        f"- 周期定位：{s1.get('cycle_position')}",
        f"- 方向：{s1.get('direction')}",
        f"- 闸门结论：{s1.get('gate_result')}",
    ]
    if bar_analysis.get("always_in"):
        lines.append(f"- Always In：{bar_analysis.get('always_in')}")
    if s1.get("support_levels"):
        lines.append("- 高周期支撑：" + "、".join(str(v) for v in s1["support_levels"][:3]))
    if s1.get("resistance_levels"):
        lines.append("- 高周期阻力：" + "、".join(str(v) for v in s1["resistance_levels"][:3]))
    if s1.get("htf_context"):
        lines.append(f"- 当时的更大背景判断：{s1['htf_context']}")
    if dec.get("order_type"):
        lines.append(
            f"- 当时的决策：{dec.get('order_type')}（交易置信度 {dec.get('trade_confidence')}）"
        )
    lines.append(
        "- 要求：本次低周期诊断需与该高周期方向保持一致，"
        "若出现冲突必须在诊断中明确指出冲突点及处理理由。"
    )
    return "\n".join(lines)


def _load_htf_brief(symbol: str, timeframe: str, logger) -> str:
    """读取最近一次高周期分析记录并生成背景文本（不额外消耗 AI）。"""
    htf_tf = HTF_MAP.get(str(timeframe or "").lower())
    if not htf_tf:
        return ""
    try:
        from pa_agent.config.paths import RECORDS_PENDING_DIR
    except Exception:  # noqa: BLE001
        return ""

    candidates = sorted(
        RECORDS_PENDING_DIR.glob(f"*_{symbol}_{htf_tf}.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        s1 = data.get("stage1_diagnosis") or {}
        if not s1:
            continue  # 跳过失败/残缺记录
        meta = data.get("meta") or {}
        ts_ms = int(meta.get("timestamp_local_ms") or path.stat().st_mtime * 1000)
        age_days = (time.time() * 1000 - ts_ms) / 86_400_000
        if age_days > HTF_MAX_AGE_DAYS:
            logger.info("高周期记录过旧（%.1f 天），不作为背景：%s", age_days, path.name)
            return ""
        logger.info("高周期背景取自 %s（%.1f 天前）", path.name, age_days)
        return _format_htf_brief(
            s1,
            data.get("stage2_decision") or {},
            htf_tf=htf_tf,
            source=path,
            age_days=age_days,
        )
    return ""


def _append_htf(messages: list[dict], htf_brief: str) -> list[dict]:
    """把高周期背景追加到最后一条 user 消息末尾（不动 system 前缀以保住 KV 缓存）。"""
    if not htf_brief:
        return messages
    out = [dict(m) for m in messages]
    for index in range(len(out) - 1, -1, -1):
        if str(out[index].get("role") or "").lower() == "user":
            out[index]["content"] = f"{out[index].get('content', '')}\n\n{htf_brief}"
            return out
    out.append({"role": "user", "content": htf_brief})
    return out


class _HtfAssemblerProxy:
    """给阶段一提示词注入高周期背景的 assembler 代理（不改项目源码）。

    orchestrator 只调用 assembler 的 build_stage1 / build_incremental_stage1 /
    build_stage2_continuation，因此只覆写需要注入的两个阶段一入口。
    """

    def __init__(self, inner, htf_brief: str) -> None:
        self.__dict__["_inner"] = inner
        self.__dict__["_htf_brief"] = htf_brief

    def __getattr__(self, name: str):
        inner = self.__dict__.get("_inner")
        if inner is None:
            raise AttributeError(name)
        return getattr(inner, name)

    def build_stage1(self, *args, **kwargs):
        return _append_htf(self._inner.build_stage1(*args, **kwargs), self._htf_brief)

    def build_incremental_stage1(self, *args, **kwargs):
        return _append_htf(
            self._inner.build_incremental_stage1(*args, **kwargs), self._htf_brief
        )


# ── 编排 ──────────────────────────────────────────────────────────────────────


def _run_pipeline(settings, frame, logger, htf_brief: str = "", previous_record=None):
    """组装真实组件并跑完整两阶段分析。"""
    from pa_agent.ai.client_factory import create_ai_client
    from pa_agent.ai.json_validator import JsonValidator
    from pa_agent.ai.prompt_assembler import PromptAssembler
    from pa_agent.ai.router import route_strategy_files
    from pa_agent.config.paths import EXPERIENCE_DIR, PROMPT_DIR, RECORDS_PENDING_DIR
    from pa_agent.orchestrator.two_stage import TwoStageOrchestrator
    from pa_agent.records.experience_reader import ExperienceReader
    from pa_agent.records.pending_writer import PendingWriter
    from pa_agent.util.event_bus import EventBus
    from pa_agent.util.threading import CancelToken, OrchestratorEvent

    event_bus = EventBus()
    exp_reader = ExperienceReader(experience_dir=EXPERIENCE_DIR, logger=logger)
    base_assembler = PromptAssembler(
        prompt_dir=PROMPT_DIR,
        experience_reader=exp_reader,
        prompt_settings=settings.prompt,
    )
    assembler = (
        _HtfAssemblerProxy(base_assembler, htf_brief) if htf_brief else base_assembler
    )
    validator = JsonValidator(settings.validation)
    pending_writer = PendingWriter(
        pending_dir=RECORDS_PENDING_DIR,
        event_bus=event_bus,
        api_key=settings.provider.api_key,
    )
    client = create_ai_client(settings.provider, logger_=logger)

    orchestrator = TwoStageOrchestrator(
        client=client,
        assembler=assembler,
        router=route_strategy_files,
        validator=validator,
        pending_writer=pending_writer,
        exp_reader=exp_reader,
        settings=settings,
    )

    events: list[OrchestratorEvent] = []

    def on_event(event: OrchestratorEvent) -> None:
        events.append(event)
        _log(f"  事件：{event.name}")

    # 阶段流式输出不回显，避免计划任务日志被 token 洪流淹没
    def on_reasoning(chunk: str) -> None:  # noqa: ARG001
        pass

    record = orchestrator.submit(
        frame=frame,
        cancel_token=CancelToken(),
        on_event=on_event,
        on_stage1_reasoning=on_reasoning,
        on_stage2_reasoning=on_reasoning,
        previous_record=previous_record,
    )
    return record, events


# ── 摘要 ──────────────────────────────────────────────────────────────────────


def _fmt_num(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, (int, float)):
        return f"{value:g}"
    return str(value)


def _fmt_list(value: object) -> str:
    if not value:
        return "—"
    if isinstance(value, (list, tuple)):
        return "、".join(str(v) for v in value)
    return str(value)


def _build_summary_markdown(record, frame, events: list) -> str:
    s1 = record.stage1_diagnosis or {}
    s2 = record.stage2_decision or {}
    dec = s2.get("decision") or {}
    bar_analysis = s1.get("bar_analysis") or {}
    meta = record.meta

    lines: list[str] = []
    lines.append(f"# 每日分析报告 · {meta.symbol} · {meta.timeframe}")
    lines.append("")
    lines.append(f"- 分析时间：{meta.timestamp_local_iso}")
    lines.append(f"- K 线根数：{meta.bar_count}（已收盘，K1 为最新）")
    lines.append(f"- 模型：{meta.ai_provider.get('model', '?')}")
    lines.append(f"- K1 收盘价：{_fmt_num(frame.bars[0].close)}")
    lines.append(f"- 流水线事件：{' → '.join(e.name for e in events)}")
    lines.append("")

    lines.append("## 一、阶段一 · 市场诊断")
    lines.append("")
    lines.append("| 项目 | 结论 |")
    lines.append("| --- | --- |")
    lines.append(f"| 闸门结果 | {_fmt_num(s1.get('gate_result'))} |")
    lines.append(f"| 周期定位 | {_fmt_num(s1.get('cycle_position'))} |")
    lines.append(f"| 备选周期 | {_fmt_num(s1.get('alternative_cycle_position'))} |")
    lines.append(f"| 方向 | {_fmt_num(s1.get('direction'))} |")
    lines.append(f"| 市场阶段 | {_fmt_num(s1.get('market_phase'))} |")
    lines.append(f"| 高潮风险 | {_fmt_num(s1.get('climax_risk'))} |")
    lines.append(f"| 诊断置信度 | {_fmt_num(s1.get('diagnosis_confidence'))} |")
    lines.append(f"| Always In | {_fmt_num(bar_analysis.get('always_in'))} |")
    lines.append(f"| 入场架构 | {_fmt_num(s1.get('entry_setup') or bar_analysis.get('entry_setup_type'))} |")
    lines.append(f"| 识别形态 | {_fmt_list(s1.get('detected_patterns'))} |")
    lines.append(f"| 支撑位 | {_fmt_list(s1.get('support_levels'))} |")
    lines.append(f"| 阻力位 | {_fmt_list(s1.get('resistance_levels'))} |")
    lines.append("")
    if s1.get("htf_context"):
        lines.append(f"**更大周期背景**：{s1['htf_context']}")
        lines.append("")
    if s1.get("key_signals"):
        lines.append(f"**关键信号**：{_fmt_list(s1.get('key_signals'))}")
        lines.append("")
    if s1.get("risk_warning"):
        lines.append(f"**风险提示**：{s1['risk_warning']}")
        lines.append("")

    lines.append("## 二、阶段二 · 交易决策")
    lines.append("")
    lines.append("| 项目 | 结论 |")
    lines.append("| --- | --- |")
    lines.append(f"| 订单类型 | {_fmt_num(dec.get('order_type'))} |")
    lines.append(f"| 方向 | {_fmt_num(dec.get('order_direction'))} |")
    lines.append(f"| 入场价 | {_fmt_num(dec.get('entry_price'))} |")
    lines.append(f"| 止损价 | {_fmt_num(dec.get('stop_loss_price'))} |")
    lines.append(f"| 目标价 1 | {_fmt_num(dec.get('take_profit_price'))} |")
    lines.append(f"| 目标价 2 | {_fmt_num(dec.get('take_profit_price_2'))} |")
    lines.append(f"| 交易置信度 | {_fmt_num(dec.get('trade_confidence'))} |")
    lines.append(f"| 预估胜率 | {_fmt_num(dec.get('estimated_win_rate'))} |")
    lines.append("")
    if dec.get("reasoning"):
        lines.append(f"**决策理由**：{dec['reasoning']}")
        lines.append("")
    if dec.get("entry_rule"):
        lines.append(f"**入场规则**：{dec['entry_rule']}")
        lines.append("")
    if dec.get("key_factors"):
        lines.append(f"**关键因素**：{_fmt_list(dec.get('key_factors'))}")
        lines.append("")
    if dec.get("watch_points"):
        lines.append(f"**观察要点**：{_fmt_list(dec.get('watch_points'))}")
        lines.append("")
    if dec.get("invalidation_condition"):
        lines.append(f"**失效条件**：{dec['invalidation_condition']}")
        lines.append("")
    if dec.get("risk_assessment"):
        lines.append(f"**风险评估**：{dec['risk_assessment']}")
        lines.append("")

    if record.strategy_files_used:
        lines.append("## 三、本局加载的策略文件")
        lines.append("")
        for name in record.strategy_files_used:
            lines.append(f"- {name}")
        lines.append("")

    if record.exception:
        lines.append("## ⚠ 异常信息")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(record.exception, ensure_ascii=False, indent=2))
        lines.append("```")
        lines.append("")

    if record.usage_total:
        lines.append("## 四、Token 用量")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(record.usage_total, ensure_ascii=False, indent=2))
        lines.append("```")
        lines.append("")

    return "\n".join(lines)


# ── 主流程 ────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="无人值守每日分析（headless）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--symbol", default="518880", help="标的代码")
    parser.add_argument("--timeframe", default="", help="周期；留空则用 settings.json 的 last_timeframe")
    parser.add_argument("--bars", type=int, default=0, help="送 AI 的已收盘 K 线根数；0 = 用 settings 的 analysis_bar_count")
    parser.add_argument("--source", default="", help="数据源（tradingview/eastmoney/...）；留空用 settings")
    parser.add_argument("--exchange", default="", help="TradingView 交易所（如 SSE）；留空用 settings")
    parser.add_argument("--proxy", default="", help="代理 URL；传 none 表示不走代理")
    parser.add_argument("--no-proxy", action="store_true", help="等价于 --proxy none")
    parser.add_argument("--dry-run", action="store_true", help="只拉数据建快照，不调用 AI")
    parser.add_argument("--force", action="store_true", help="即使 K 线与上次相同也强制分析")
    parser.add_argument(
        "--allow-weekend",
        action="store_true",
        help="周末也运行（默认跳过周六周日）",
    )
    parser.add_argument("--fetch-retries", type=int, default=3, help="数据拉取重试次数")
    parser.add_argument("--fetch-retry-wait", type=float, default=20.0, help="重试间隔秒数")
    parser.add_argument(
        "--pipeline-retries",
        type=int,
        default=1,
        help="阶段校验失败时整体重跑流水线的额外次数（模型偶发输出不合规时的补救）",
    )
    parser.add_argument(
        "--allow-forming-bar",
        action="store_true",
        help="允许 K1 为当日尚未收盘的 K 线（默认拦截，避免用盘中数据得出不可靠结论）",
    )
    parser.add_argument(
        "--no-htf",
        action="store_true",
        help="不注入高周期背景（默认会把上级周期的分析结论拼进提示词）",
    )
    parser.add_argument(
        "--no-continuity",
        action="store_true",
        help="不读取上一份记录（默认会让阶段二与校验器做决策连续性判断）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # EventBus 是 QObject 子类；headless 下给一个无 GUI 的 Qt 应用实例最稳妥
    try:
        from PyQt6.QtCore import QCoreApplication

        if QCoreApplication.instance() is None:
            QCoreApplication([])
    except Exception:  # noqa: BLE001
        pass

    from pa_agent.config.paths import SETTINGS_JSON_PATH
    from pa_agent.config.settings import load_settings, provider_api_key_configured
    from pa_agent.util.logging import configure_logging

    settings = load_settings(SETTINGS_JSON_PATH)
    configure_logging(api_key=settings.provider.api_key)
    import logging

    logger = logging.getLogger("pa_agent.daily")
    logger.setLevel(logging.INFO)

    symbol = args.symbol
    timeframe = args.timeframe or settings.general.last_timeframe
    bars = args.bars or settings.general.analysis_bar_count

    _log("=" * 68)
    _log(f"每日分析启动：{symbol} {timeframe}，K 线 {bars} 根")
    _log(f"项目目录：{ROOT}")

    # 1) 周末跳过（A 股/现货周末无新 K 线）
    today = datetime.now()
    if today.weekday() >= 5 and not args.allow_weekend:
        _log(f"今天是周{'六日'[today.weekday() - 5]}，非交易日，跳过。")
        _log("=" * 68)
        return 0

    # 2) 代理
    source_kind = (args.source or settings.general.last_data_source or "").strip().lower()
    if args.no_proxy:
        args.proxy = "none"
    if source_kind in _NEEDS_PROXY_SOURCES or not source_kind:
        proxy = _resolve_proxy(args.proxy)
        if proxy:
            _apply_proxy(proxy)
        elif (args.proxy or "").strip().lower() == "none":
            _log("按 --no-proxy 要求：不使用代理，直连。")
        else:
            _log("未探测到可用代理，按直连尝试（Clash 未运行？）")
    else:
        _log(f"数据源 {source_kind} 为国内接口，跳过代理设置。")

    if not args.dry_run and not provider_api_key_configured(settings):
        _log(f"配置错误：{SETTINGS_JSON_PATH} 中未配置可用 API Key。")
        return 2

    # 3) 拉数据 + 建快照
    try:
        frame = _fetch_frame(settings, args, logger)
    except Exception as exc:  # noqa: BLE001
        _log(f"数据获取失败：{exc}")
        logger.error("数据获取失败", exc_info=True)
        _log("=" * 68)
        return 1

    state = _load_state()
    state_key = f"{frame.symbol}_{frame.timeframe}"
    fingerprint = _fingerprint(frame)

    # 3.5) 高周期背景：直接复用最近一次上级周期分析记录，零额外 AI 成本
    htf_brief = (
        "" if args.no_htf else _load_htf_brief(frame.symbol, frame.timeframe, logger)
    )
    if htf_brief:
        _log(
            f"已加载高周期背景（{HTF_MAP.get(str(frame.timeframe).lower(), '?')}"
            f"，{len(htf_brief)} 字）"
        )
    else:
        _log("未找到可用的高周期分析记录，本次不注入背景。")

    # 3.6) 上一份成功记录：阶段二与校验器据此做决策连续性判断
    #      （例如上次说「等反弹到 9.62 做空」，这次要看该计划是否被跟踪/失效）
    previous_record = None
    if not args.no_continuity:
        try:
            from pa_agent.records.analysis_history import find_latest_successful_record

            previous_record = find_latest_successful_record(
                symbol=frame.symbol, timeframe=frame.timeframe
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取上一份成功记录失败：%s", exc)
        if previous_record is not None:
            prev_dec = (previous_record.stage2_decision or {}).get("decision") or {}
            _log(
                "已加载上一份记录（"
                f"{previous_record.meta.timestamp_local_iso}，"
                f"上次决策：{prev_dec.get('order_type')}）用于决策连续性校验。"
            )
        else:
            _log("未找到同标的同周期的上一份成功记录，本次不做连续性校验。")

    # dry-run 只做诊断，不受「数据未变则跳过」影响
    if args.dry_run:
        if _k1_is_today_unclosed(frame):
            _log(
                "提示：K1 是今天尚未收盘的日线，正式运行会被拦截"
                "（除非加 --allow-forming-bar）。"
            )
        _log("--dry-run：数据与快照均正常，未调用 AI。")
        _log(f"指纹：{json.dumps(fingerprint, ensure_ascii=False)}")
        _log("=" * 68)
        return 0

    # 4) 拦截「当日未收盘的日线」：用它做分析会得出不可靠结论（详见函数注释）
    if _k1_is_today_unclosed(frame) and not args.allow_forming_bar:
        _log(
            "K1 是今天尚未收盘的日线（A 股日线 15:00 才收盘），"
            "此时分析结论不可靠，本次跳过。"
        )
        _log("如确需用盘中数据跑，请加 --allow-forming-bar。")
        _log("=" * 68)
        return 0

    # 4) 数据未变化则跳过（周线在一周内、节假日/停牌期间都不会产生新的已收盘 K 线）
    previous = state.get(state_key)
    if previous and not args.force:
        same = all(previous.get(k) == v for k, v in fingerprint.items() if k != "symbol")
        if same:
            _log(
                "最新已收盘 K 线与上次分析完全一致"
                f"（K1 开盘 {datetime.fromtimestamp(fingerprint['k1_ts_open'] / 1000):%Y-%m-%d %H:%M}，"
                f"收 {fingerprint['k1_close']}），跳过本次 AI 调用以免重复消耗 token。"
            )
            _log("如需强制分析，请加 --force。")
            _log("=" * 68)
            return 0

    # 5) 完整两阶段分析
    #    模型偶发输出不合规（例如 gate_trace 的 reason 填了套话）会让阶段校验失败，
    #    导致当天完全没有产出，因此允许把整条流水线重跑；只有拿到阶段二决策才算成功。
    attempts = max(1, args.pipeline_retries + 1)
    record = None
    events: list = []
    for attempt in range(1, attempts + 1):
        try:
            record, events = _run_pipeline(settings, frame, logger, htf_brief, previous_record)
        except Exception as exc:  # noqa: BLE001
            _log(f"编排异常（第 {attempt}/{attempts} 轮）：{exc}")
            logger.error("编排异常", exc_info=True)
            record, events = None, []
        if record is not None and record.stage2_decision:
            break
        if attempt < attempts:
            _log(f"第 {attempt} 轮未产出阶段二决策，整体重跑流水线…")
        else:
            _log(f"已尝试 {attempts} 轮，仍未拿到阶段二决策。")

    if record is None:
        _log("=" * 68)
        return 1

    ok = bool(record.stage2_decision)

    # 6) 写人读版摘要（完整记录由 PendingWriter 在 submit() 内落盘）
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    summary_path = DAILY_DIR / f"{stamp}_{frame.symbol}_{frame.timeframe}.md"
    try:
        summary_path.write_text(
            _build_summary_markdown(record, frame, events), encoding="utf-8"
        )
        _log(f"摘要已写入：{summary_path}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("写摘要失败：%s", exc)

    # 7) 只有产出完整决策才登记指纹；失败时保持原状态，
    #    这样下次运行（K 线未变）会重新尝试同一根 K 线，而不是误判为“已分析过”而跳过。
    if ok:
        state[state_key] = {
            **fingerprint,
            "last_run": datetime.now().isoformat(timespec="seconds"),
        }
        _save_state(state)
    else:
        _log("本轮未产出完整决策，不更新指纹；下次运行会重新尝试这根 K 线。")

    s1 = record.stage1_diagnosis or {}
    s2 = record.stage2_decision or {}
    dec = s2.get("decision") or {}
    _log("─" * 68)
    _log(f"阶段一：闸门={s1.get('gate_result')} 周期={s1.get('cycle_position')} 方向={s1.get('direction')}")
    _log(f"阶段二：{dec.get('order_type')} {dec.get('order_direction') or ''} 入场={dec.get('entry_price')} 止损={dec.get('stop_loss_price')}")
    if record.exception:
        _log(f"存在异常：{json.dumps(record.exception, ensure_ascii=False)}")

    exc_type = (record.exception or {}).get("type", "")
    _log("=" * 68)
    if not ok:
        return 1
    return 1 if exc_type == "network_error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
