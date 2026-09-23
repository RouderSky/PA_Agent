# gitprox.ps1 — 动态探测 Clash 代理端口并执行 git 命令（不硬编码端口）
#
# 用法（在仓库根目录执行）：
#   .\tools\gitprox.ps1 fetch upstream --prune
#   .\tools\gitprox.ps1 pull origin main --no-rebase --no-edit
#   .\tools\gitprox.ps1 push origin main
#
# 原理：Clash 的 mixed-port 每次启动都可能变，所以每次运行都从
#       clash-win64.exe 的 -d <数据目录> 参数定位 config.yaml，再读 mixed-port。
#       端口变了也无所谓，脚本自己会重新探测。

param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$GitArgs
)

$ErrorActionPreference = 'Stop'

function Get-ClashMixedPort {
    # 1) 从进程命令行取数据目录
    $proc = Get-CimInstance Win32_Process -Filter "name='clash-win64.exe'" -ErrorAction SilentlyContinue |
            Select-Object -First 1
    if (-not $proc) { throw "未找到 clash-win64.exe 进程，请先启动 Clash。" }

    $cmdline = $proc.CommandLine
    $dataDir = $null
    if ($cmdline -match '-d\s+"([^"]+)"') {
        $dataDir = $Matches[1]
    } elseif ($cmdline -match '-d\s+(\S+)') {
        $dataDir = $Matches[1]
    }
    if (-not $dataDir) { throw "无法从命令行解析 Clash 数据目录：$cmdline" }

    $cfg = Join-Path $dataDir 'config.yaml'
    if (-not (Test-Path $cfg)) { throw "找不到 Clash 配置文件：$cfg" }

    # 2) 从 config.yaml 读 mixed-port（注意可能有引号）
    $line = Select-String -Path $cfg -Pattern '^\s*mixed-port\s*:\s*(\d+)' |
            Select-Object -First 1
    if (-not $line) { throw "无法在 $cfg 中解析 mixed-port。" }

    return $line.Matches[0].Groups[1].Value
}

$port = Get-ClashMixedPort
$proxy = "http://127.0.0.1:$port"

Write-Host "[gitprox] Clash mixed-port = $port" -ForegroundColor DarkGray

# 3) 探活：确认代理真的可用（连不上就直接报错，避免 git 卡住 20 秒）
$code = & curl.exe -s -o NUL -w "%{http_code}" --max-time 8 -x $proxy https://github.com
if ($code -ne '200') {
    throw "代理 $proxy 不可用（https://github.com 返回 $code）。请检查 Clash 是否开启系统代理/TUN。"
}

# 4) 只为本次调用注入代理，不写进任何配置文件
& git -c "http.proxy=$proxy" -c "https.proxy=$proxy" @GitArgs
exit $LASTEXITCODE
