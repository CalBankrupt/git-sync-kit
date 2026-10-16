# 最小复现：严格模式下 $dirty.Count 的失败条件
# 只操作 D:\CAD Tool\_tmp 下的临时仓库，不碰任何真实仓库
$ErrorActionPreference = 'Continue'
Import-Module 'D:\CAD Tool\git-sync-kit\powershell\GitSyncKit\GitSyncKit.psd1' -Force

$base = 'D:\CAD Tool\_tmp\pushrepro'
if (Test-Path $base) { Remove-Item $base -Recurse -Force }
New-Item -ItemType Directory -Force $base | Out-Null

function New-ReproRepo {
    param([string]$Name)
    $dir = Join-Path $base $Name
    $origin = Join-Path $base "$Name-origin.git"
    New-Item -ItemType Directory -Force $dir | Out-Null
    & git -C $dir init --initial-branch=main --quiet 2>&1 | Out-Null
    & git -C $dir config user.email 'test@local'
    & git -C $dir config user.name 'test'
    Set-Content -LiteralPath (Join-Path $dir 'README.md') -Value '# seed' -Encoding UTF8
    & git -C $dir add -A 2>&1 | Out-Null
    & git -C $dir commit -q -m 'seed' 2>&1 | Out-Null
    & git init --bare --initial-branch=main --quiet $origin 2>&1 | Out-Null
    & git -C $dir remote add origin $origin 2>&1 | Out-Null
    & git -C $dir push -q -u origin main 2>&1 | Out-Null
    return $dir
}

function Try-Push {
    param([string]$Label, [string]$Dir)
    Write-Host "--- $Label ---"
    $st = & git -C $Dir status --porcelain
    Write-Host "    git status: $(if ($st) { @($st).Count } else { 0 }) 行 $($st -join ' | ')"
    try {
        $r = Invoke-GitSyncPush -Root $Dir -Branch main -Mode sync -Message 'repro'
        Write-Host "    ok=$($r.ok)  files=$($r.files)"
        Write-Host "    message=$((($r.message -split "`n")[0]).Trim())"
    } catch {
        Write-Host "    ✗ 抛异常: $($_.Exception.GetType().Name): $($_.Exception.Message)"
        Write-Host "    位置: $($_.StackTrace -split "`n" | Select-Object -First 1)"
    }
}

Write-Host '=== 情形 A：干净的仓库（没有任何改动）==='
$a = New-ReproRepo 'clean'
Try-Push 'A: 干净仓库' $a

Write-Host ''
Write-Host '=== 情形 B：只有一个未跟踪的目录（就是用户这次的情况）==='
$b = New-ReproRepo 'one-untracked'
New-Item -ItemType Directory -Force (Join-Path $b 'projects\03_u_channel_406') | Out-Null
Set-Content -LiteralPath (Join-Path $b 'projects\03_u_channel_406\build.py') -Value '# model' -Encoding UTF8
Try-Push 'B: 一个未跟踪目录' $b

Write-Host ''
Write-Host '=== 情形 C：两个未跟踪条目（对照：多元素时 Count 正常）==='
$c = New-ReproRepo 'two-untracked'
Set-Content -LiteralPath (Join-Path $c 'a.txt') -Value 'a' -Encoding UTF8
Set-Content -LiteralPath (Join-Path $c 'b.txt') -Value 'b' -Encoding UTF8
Try-Push 'C: 两个未跟踪文件' $c
