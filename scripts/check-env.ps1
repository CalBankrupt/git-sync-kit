#Requires -Version 5.1
<#
    环境自检 —— 接入共享模块 git-sync-kit 之前先跑一次。

    为什么值得单独做一件事：缺依赖的表现常常不是「报错」，而是「静默地不工作」。
    最典型的一种是用 Windows PowerShell 5.1 跑：它的默认执行策略会直接拒绝加载
    模块，而**退出码仍然是 0** —— 命令「跑完了」，却什么都没发生。与其让人去猜，
    不如在这里一次说清缺什么、怎么补。

    本文件故意写成 #Requires -Version 5.1：这样即使在 5.1 下也能跑起来，再由它
    自己把「版本不够」讲清楚。若写成 7.0，5.1 会直接拒绝执行，反而看不到提示。

    用法:
        pwsh -File scripts/check-env.ps1          只查必需项（PowerShell 7 / git）
        pwsh -File scripts/check-env.ps1 -All     连可选项（node / python）一起查

    退出码:
        0  必需项齐备
        1  缺必需项 —— 调用方应当据此中止安装或接入流程
#>
[CmdletBinding()]
param(
    [switch]$All
)

$ErrorActionPreference = 'Continue'

$script:missing = 0

function Write-Ok($msg)   { Write-Host "  [ok]   $msg" -ForegroundColor Green }
function Write-Bad($msg)  { Write-Host "  [缺失] $msg" -ForegroundColor Red }
function Write-Hint($msg) { Write-Host "         $msg" -ForegroundColor DarkGray }
function Write-Skip($msg) { Write-Host "  [跳过] $msg" -ForegroundColor DarkGray }

Write-Host ""
Write-Host "  git-sync-kit 环境自检" -ForegroundColor White
Write-Host "  ============================================" -ForegroundColor DarkGray
Write-Host ""

# ------------------------------------------------------------ 必需：PowerShell 7+
$psv = $PSVersionTable.PSVersion
if ($psv.Major -ge 7) {
    Write-Ok "PowerShell $psv"
} else {
    $script:missing++
    Write-Bad "PowerShell $psv —— 需要 7 或更高"
    Write-Hint "Windows PowerShell 5.1 不在支持范围内：它默认的执行策略常常直接拒绝"
    Write-Hint "加载模块，而退出码仍然是 0，表现为「命令跑完了但什么都没输出」。"
    Write-Hint "装一个：https://aka.ms/powershell"
    Write-Hint "装完请用 pwsh 启动，而不是 powershell。"
}

# ------------------------------------------------------------ 必需：git
$git = Get-Command git -ErrorAction SilentlyContinue
if ($git) {
    $ver = (& $git.Source --version 2>&1 | Select-Object -First 1)
    Write-Ok "$ver"
} else {
    $script:missing++
    Write-Bad "找不到 git"
    Write-Hint "检查 / 更新 / 同步的每一个动作都要用它。装一个：https://git-scm.com/download/win"
}

# ------------------------------------------------------------ 可选：按语言实现
if ($All) {
    $node = Get-Command node -ErrorAction SilentlyContinue
    if ($node) { Write-Ok "node $(& $node.Source --version)   （只有 Node 那份实现要用）" }
    else { Write-Skip "没有 node —— 只有 Node 那份实现需要它" }

    $py = Get-Command python -ErrorAction SilentlyContinue
    if ($py) { Write-Ok "python $(& $py.Source --version)   （只有 Python 那份实现要用）" }
    else { Write-Skip "没有 python —— 只有 Python 那份实现需要它" }
}

Write-Host ""
if ($script:missing -eq 0) {
    Write-Host "  自检通过。" -ForegroundColor Green
    Write-Host ""
    exit 0
}

Write-Host "  自检未通过：缺 $($script:missing) 项必需依赖，先补齐再继续。" -ForegroundColor Red
Write-Host ""
exit 1
