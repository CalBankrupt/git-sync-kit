# 最小复现：严格模式下 $dirty.Count 的失败条件
# 另加：push 结果里 files/entries 的语义（条目数 ≠ 文件数）
# 只操作 D:\CAD Tool\_tmp 下的临时仓库，不碰任何真实仓库
$ErrorActionPreference = 'Continue'
Import-Module 'D:\CAD Tool\git-sync-kit\powershell\GitSyncKit\GitSyncKit.psd1' -Force

$base = 'D:\CAD Tool\_tmp\pushrepro'
if (Test-Path $base) { Remove-Item $base -Recurse -Force }
New-Item -ItemType Directory -Force $base | Out-Null

# 断言都在这个临时根之下 —— 任何越界的写入都直接中止（踩坑 14）
function Assert-Under($Path) {
    $full = [System.IO.Path]::GetFullPath($Path)
    if (-not $full.StartsWith($base, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "安全闸门：路径越界 $full（应位于 $base 之下）"
    }
    return $full
}

$script:Failures = 0
function Check($Label, $Condition, $Detail) {
    if ($Condition) { Write-Host "    ✓ $Label" -ForegroundColor Green }
    else {
        Write-Host "    ✗ $Label  $Detail" -ForegroundColor Red
        $script:Failures++
    }
}

function New-ReproRepo {
    param([string]$Name)
    $dir = Assert-Under (Join-Path $base $Name)
    $origin = Assert-Under (Join-Path $base "$Name-origin.git")
    # 先清干净：上一轮跑完仓库里已有提交，残留会让「预期 2 条改动」变成 1 条 —— 假失败。
    foreach ($p in @($dir, $origin)) { if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Recurse -Force } }
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
        Write-Host "    ok=$($r.ok)  files=$($r.files)  entries=$($r.entries)"
        Write-Host "    message=$((($r.message -split "`n")[0]).Trim())"
        return $r
    } catch {
        Write-Host "    ✗ 抛异常: $($_.Exception.GetType().Name): $($_.Exception.Message)" -ForegroundColor Red
        Write-Host "    位置: $($_.StackTrace -split "`n" | Select-Object -First 1)"
        $script:Failures++
        return $null
    }
}

Write-Host '=== 情形 A：干净的仓库（没有任何改动）==='
$a = New-ReproRepo 'clean'
$ra = Try-Push 'A: 干净仓库' $a
Check 'A: ok=True' ($ra -and $ra.ok) "got=$($ra.ok)"
Check 'A: committed=False' ($ra -and -not $ra.committed) "got=$($ra.committed)"
Check 'A: files=0' ($ra -and $ra.files -eq 0) "got=$($ra.files)"
Check 'A: entries=0' ($ra -and $ra.entries -eq 0) "got=$($ra.entries)"
Check 'A: 文案是「没有本地改动」' ($ra -and $ra.message -like '*没有本地改动*') "got=$($ra.message)"

Write-Host ''
Write-Host '=== 情形 B：只有一个未跟踪的目录（就是用户这次的情况）==='
$b = New-ReproRepo 'one-untracked'
New-Item -ItemType Directory -Force (Join-Path $b 'projects\03_u_channel_406') | Out-Null
Set-Content -LiteralPath (Join-Path $b 'projects\03_u_channel_406\build.py') -Value '# model' -Encoding UTF8
$rb = Try-Push 'B: 一个未跟踪目录' $b
Check 'B: ok=True（严格模式 .Count 不再炸）' ($rb -and $rb.ok) "got=$($rb.ok)"
Check 'B: files=1（条目数）' ($rb -and $rb.files -eq 1) "got=$($rb.files)"
Check 'B: entries=1' ($rb -and $rb.entries -eq 1) "got=$($rb.entries)"
Check 'B: 文案说「项」不说「文件」' ($rb -and $rb.message -like '*项改动*' -and $rb.message -notlike '*个文件*') "got=$($rb.message)"

Write-Host ''
Write-Host '=== 情形 C：两个未跟踪条目（对照：多元素时 Count 正常）==='
$c = New-ReproRepo 'two-untracked'
Set-Content -LiteralPath (Join-Path $c 'a.txt') -Value 'a' -Encoding UTF8
Set-Content -LiteralPath (Join-Path $c 'b.txt') -Value 'b' -Encoding UTF8
$rc = Try-Push 'C: 两个未跟踪文件' $c
Check 'C: ok=True' ($rc -and $rc.ok) "got=$($rc.ok)"
Check 'C: files=2' ($rc -and $rc.files -eq 2) "got=$($rc.files)"

Write-Host ''
Write-Host '=== 情形 D：一个未跟踪目录里装了多个文件（条目数 ≠ 文件数）==='
$d = New-ReproRepo 'one-dir-many-files'
$deep = Join-Path $d 'projects\03_u_channel_406'
New-Item -ItemType Directory -Force $deep | Out-Null
foreach ($n in 'build.py', 'model.step', 'model.stl', 'README.md', 'ref.png') {
    Set-Content -LiteralPath (Join-Path $deep $n) -Value "content of $n" -Encoding UTF8
}
$rd = Try-Push 'D: 一个目录含 5 个文件' $d
Check 'D: ok=True' ($rd -and $rd.ok) "got=$($rd.ok)"
Check 'D: files=1 而不是 5（数的是条目）' ($rd -and $rd.files -eq 1) "got=$($rd.files)"
Check 'D: entries=files' ($rd -and $rd.entries -eq $rd.files) "entries=$($rd.entries) files=$($rd.files)"
# 真正提交进去的文件数（用来证明「条目 1 ≠ 文件 5」）
$committedFiles = @(& git -C $d show --stat --oneline --name-only HEAD | Select-Object -Skip 1 | Where-Object { $_ })
Check 'D: 实际提交了 5 个文件' ($committedFiles.Count -eq 5) "got=$($committedFiles.Count): $($committedFiles -join ', ')"

Write-Host ''
if ($script:Failures -eq 0) {
    Write-Host '结论: 通过 —— 三种改动规模都能推送，files/entries 数的是条目、文案说「项」' -ForegroundColor Green
    exit 0
} else {
    Write-Host "结论: 失败 $($script:Failures) 项" -ForegroundColor Red
    exit 1
}
