#Requires -Version 7.0
<#
    git-sync-kit —— PowerShell 实现

    严格实现仓库根的 SPEC.md：
      · 第 3 节  版本号与试验版（远端 VERSION 带 *）
      · 第 4 节  七态状态机
      · 第 5 节  JSON 契约
      · 第 6 节  六条安全约束
      · 第 7 节  通道选择（git 优先，HTTP 兜底）

    返回值一律是 PSCustomObject，字段名与 SPEC 第 5 节一致，可以直接
    ConvertTo-Json 交给界面。

    要求 PowerShell 7+（pwsh）。**Windows PowerShell 5.1 不在支持范围内**：
    它默认的执行策略常常直接拒绝加载模块，而退出码**仍然是 0** —— 表现出来
    就是「命令跑完了，什么都没输出」，从现象很难倒推原因。

    注意：本文件以 UTF-8 BOM 保存。少了 BOM，一些工具（以及 Windows
    PowerShell 5.1）会按 ANSI 代码页读它，中文注释会变成乱码并可能破坏解析。
#>

Set-StrictMode -Version Latest

$script:KitVersion   = '0.1.0'
$script:BranchDefault = 'main'
$script:TrialSuffix  = '*'
$script:DiffLimit    = 100
$script:BackupDir    = '_backup_previous'

# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
function Invoke-GitCapture {
    <#
        跑一条 git 命令，返回 @{ Code; Out; Err }。绝不抛异常。

        注意 Out 里**混着 stdout 和 stderr**（PowerShell 的 2>&1 分不开这两者），
        Err 只在 PowerShell 自己出错时才非空（比如找不到 git）。所以 git 返回
        非 0 时，调用方要把 Out 也当作错误信息的来源 —— 只读 Err 的话，界面上
        会出现「失败：」后面什么都没有。

        取值顺序对齐 Python / Node 两份实现：err → out → 各自的兜底文案。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string[]]$GitArgs,
        [int]$TimeoutSeconds = 90
    )

    try {
        $out = & git -C $Root @GitArgs 2>&1
        $code = $LASTEXITCODE
        $text = ($out | ForEach-Object { "$_" }) -join "`n"
        return [pscustomobject]@{ Code = $code; Out = $text.Trim(); Err = '' }
    }
    catch {
        return [pscustomobject]@{ Code = 1; Out = ''; Err = "$($_.Exception.Message)" }
    }
}

function ConvertTo-VersionTuple {
    param([string]$Text)
    $parts = @()
    foreach ($chunk in ("$Text".Trim().TrimStart('v', 'V') -split '\.')) {
        $digits = ($chunk.ToCharArray() | Where-Object { [char]::IsDigit($_) }) -join ''
        $parts += $(if ($digits) { [int]$digits } else { 0 })
    }
    if ($parts.Count -eq 0) { return @(0) }
    return $parts
}

function Test-VersionNewer {
    param([string]$Candidate, [string]$Current)
    $a = ConvertTo-VersionTuple $Candidate
    $b = ConvertTo-VersionTuple $Current
    $n = [Math]::Max($a.Count, $b.Count)
    for ($i = 0; $i -lt $n; $i++) {
        $x = $(if ($i -lt $a.Count) { $a[$i] } else { 0 })
        $y = $(if ($i -lt $b.Count) { $b[$i] } else { 0 })
        if ($x -ne $y) { return ($x -gt $y) }
    }
    return $false
}

function Get-GitSyncRoot {
    <#
        从某个目录向上找含 .git 的仓库根；找不到返回 $null。
    #>
    [CmdletBinding()]
    param([string]$Start = (Get-Location).Path)

    $dir = Get-Item -LiteralPath $Start -ErrorAction SilentlyContinue
    if (-not $dir) { return $null }
    if (-not $dir.PSIsContainer) { $dir = $dir.Directory }

    while ($dir) {
        if (Test-Path -LiteralPath (Join-Path $dir.FullName '.git')) { return $dir.FullName }
        $dir = $dir.Parent
    }
    return $null
}

function Get-GitSyncLocalVersion {
    param([Parameter(Mandatory)][string]$Root)

    $vf = Join-Path $Root 'VERSION'
    if (Test-Path -LiteralPath $vf) {
        $text = (Get-Content -LiteralPath $vf -Raw -ErrorAction SilentlyContinue)
        if ($text) {
            $line = ($text -split "`r?`n")[0].Trim()
            if ($line) { return $line.TrimEnd($script:TrialSuffix).Trim() }
        }
    }
    # 退路：pyproject.toml
    $py = Join-Path $Root 'pyproject.toml'
    if (Test-Path -LiteralPath $py) {
        foreach ($line in (Get-Content -LiteralPath $py -ErrorAction SilentlyContinue)) {
            $t = $line.Trim()
            if ($t -like 'version*' -and $t -like '*=*') {
                return $t.Split('=', 2)[1].Trim().Trim('"').Trim("'")
            }
        }
    }
    return '0.0.0'
}

function Test-GitSyncDirty {
    param([Parameter(Mandatory)][string]$Root)
    $r = Invoke-GitCapture -Root $Root -GitArgs @('status', '--porcelain')
    return ($r.Code -eq 0 -and [bool]$r.Out)
}

function Get-GitSyncDirtyFiles {
    param([Parameter(Mandatory)][string]$Root)
    $r = Invoke-GitCapture -Root $Root -GitArgs @('status', '--porcelain')
    if ($r.Code -ne 0 -or -not $r.Out) { return @() }
    return @($r.Out -split "`r?`n" | Where-Object { $_ })
}

# ---------------------------------------------------------------------------
# 远端探测
# ---------------------------------------------------------------------------
function Get-GitSyncRemote {
    <#
        返回 @{ Version; Behind; Error; Mode }。版本号已去掉 *。
        判断的是远端而不是本地 —— 标记由发布方打。
    #>
    param(
        [Parameter(Mandatory)][string]$Root,
        [string]$Branch = $script:BranchDefault
    )

    $f = Invoke-GitCapture -Root $Root -GitArgs @('fetch', '--quiet', 'origin', $Branch) -TimeoutSeconds 120
    if ($f.Code -ne 0) {
        # git 自己失败时原因在 Out 里（见 Invoke-GitCapture 的说明）。
        # 原先只读 Err，于是界面显示成「git 通道失败：；HTTP 兜底也失败：…」——
        # 用户看不到 fatal: ... 到底是什么。
        $why = if ($f.Err) { $f.Err } elseif ($f.Out) { $f.Out } else { 'git fetch 失败' }
        return [pscustomobject]@{ Version = $null; Behind = 0; Error = $why; Mode = 'release' }
    }

    $s = Invoke-GitCapture -Root $Root -GitArgs @('show', "origin/${Branch}:VERSION")
    if ($s.Code -ne 0 -or -not $s.Out) {
        # 与 fetch 失败分支同理：git 的原因在 Out 里（见 Invoke-GitCapture）。
        $why = if ($s.Err) { $s.Err } elseif ($s.Out) { $s.Out } else { "origin/${Branch} 上没有 VERSION 文件" }
        return [pscustomobject]@{ Version = $null; Behind = 0; Error = $why; Mode = 'release' }
    }

    $raw = ($s.Out -split "`r?`n")[0].Trim()
    $mode = if ($raw.EndsWith($script:TrialSuffix)) { 'trial' } else { 'release' }
    $version = $raw.TrimEnd($script:TrialSuffix).Trim()

    $behind = 0
    $b = Invoke-GitCapture -Root $Root -GitArgs @('rev-list', '--count', "HEAD..origin/${Branch}")
    if ($b.Code -eq 0 -and $b.Out -match '^\d+$') { $behind = [int]$b.Out }

    return [pscustomobject]@{ Version = $version; Behind = $behind; Error = ''; Mode = $mode }
}

function Get-GitSyncRemoteHttp {
    param(
        [string]$Repo,
        [string]$Branch = $script:BranchDefault
    )

    if (-not $Repo) {
        return [pscustomobject]@{ Version = $null; Error = '未配置 repo（owner/name），HTTP 通道不可用' }
    }
    $url = "https://raw.githubusercontent.com/$Repo/$Branch/VERSION"
    try {
        $resp = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 30
        $text = "$($resp.Content)".Trim()
        if (-not $text) { return [pscustomobject]@{ Version = $null; Error = '远端 VERSION 是空的' } }
        $line = ($text -split "`r?`n")[0].Trim().TrimEnd($script:TrialSuffix).Trim()
        return [pscustomobject]@{ Version = $line; Error = '' }
    }
    catch {
        return [pscustomobject]@{ Version = $null; Error = "$($_.Exception.Message)" }
    }
}

# ---------------------------------------------------------------------------
# 差异清单（SPEC 5.2）
# ---------------------------------------------------------------------------
function Get-GitSyncDiff {
    <#
        只读。工作区 vs origin/<Branch>。

        基准是工作区而不是 HEAD：选择性应用只写工作区、不移动 HEAD，
        按提交比较会导致应用完了差异还一直列着。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Root,
        [string]$Branch = $script:BranchDefault
    )

    $files = New-Object System.Collections.ArrayList

    $st = Invoke-GitCapture -Root $Root -GitArgs @('diff', '--name-status', "origin/$Branch")
    if ($st.Code -eq 0 -and $st.Out) {
        foreach ($line in ($st.Out -split "`r?`n")) {
            if ($line -match '^([AMDRT])\d*\t(.+)$') {
                [void]$files.Add([pscustomobject]@{
                    status = $Matches[1]
                    path   = $Matches[2]
                    time   = $null
                })
            }
        }
    }

    # 每个文件最近一次改动时间：git log 从新到旧，第一次出现即最近
    $timeMap = @{}
    $lg = Invoke-GitCapture -Root $Root -GitArgs @(
        'log', '--name-only', '--format=%cI', "HEAD..origin/$Branch"
    )
    if ($lg.Code -eq 0 -and $lg.Out) {
        $current = $null
        foreach ($raw in ($lg.Out -split "`r?`n")) {
            $text = $raw.Trim()
            if (-not $text) { continue }
            if ($text -match '^\d{4}-\d{2}-\d{2}T') { $current = $text; continue }
            if ($current -and -not $timeMap.ContainsKey($text)) { $timeMap[$text] = $current }
        }
    }

    foreach ($f in $files) {
        if ($timeMap.ContainsKey($f.path)) { $f.time = $timeMap[$f.path] }
    }

    $times = @($files | ForEach-Object { $_.time } | Where-Object { $_ } | Sort-Object)
    $limit = [Math]::Min($files.Count, $script:DiffLimit)
    $slice = @()
    for ($i = 0; $i -lt $limit; $i++) { $slice += $files[$i] }

    return [pscustomobject]@{
        count       = $files.Count
        latestTime  = $(if ($times.Count) { $times[-1] } else { $null })
        files       = $slice
        truncated   = ($files.Count -gt $script:DiffLimit)
    }
}

# ---------------------------------------------------------------------------
# 检查（SPEC 5.1）
# ---------------------------------------------------------------------------
function Get-GitSyncReport {
    <#
        跑一次检查，返回符合 SPEC 5.1 的对象。
    #>
    [CmdletBinding()]
    param(
        [string]$Root = (Get-Location).Path,
        [string]$Branch = $script:BranchDefault,
        [string]$Repo = ''
    )

    $isGit = Test-Path -LiteralPath (Join-Path $Root '.git')
    $rep = [ordered]@{
        ok = $true; state = 'unknown'; mode = 'release'
        local = Get-GitSyncLocalVersion -Root $Root
        remote = $null; has_update = $false; behind = 0
        dirty = $false; is_git = $isGit; channel = 'none'
        branch = $Branch; message = ''; error = $null
        can_install = $false; can_select = $false; diff = $null
    }

    if ($isGit) {
        $rep.dirty = Test-GitSyncDirty -Root $Root
        $remote = Get-GitSyncRemote -Root $Root -Branch $Branch
        if ($remote.Version) {
            $rep.remote = $remote.Version
            $rep.behind = $remote.Behind
            $rep.mode = $remote.Mode
            $rep.channel = 'git'
        }
        else {
            $http = Get-GitSyncRemoteHttp -Repo $Repo -Branch $Branch
            if ($http.Version) {
                $rep.remote = $http.Version
                $rep.channel = 'http'
                $rep.error = "git 通道不可用（$($remote.Error)），已改用 HTTP 查询；此模式下无法安装或应用。"
            }
            else {
                $rep.ok = $false; $rep.state = 'error'
                $rep.error = "git 通道失败：$($remote.Error)；HTTP 兜底也失败：$($http.Error)"
                $rep.message = $rep.error
                return [pscustomobject]$rep
            }
        }
    }
    else {
        $http = Get-GitSyncRemoteHttp -Repo $Repo -Branch $Branch
        if ($http.Version) {
            $rep.remote = $http.Version; $rep.channel = 'http'
        }
        else {
            $rep.ok = $false; $rep.state = 'error'
            $rep.error = "不是 git 仓库，且 HTTP 查询失败：$($http.Error)"
            $rep.message = $rep.error
            return [pscustomobject]$rep
        }
    }

    # ---- 试验版：只看有没有差异 ----
    if ($rep.mode -eq 'trial' -and $rep.channel -eq 'git') {
        $d = Get-GitSyncDiff -Root $Root -Branch $Branch
        $rep.diff = $d
        $rep.has_update = ($d.count -gt 0)
        if ($d.count -gt 0) {
            $rep.state = 'trial'
            $rep.message = "试验版（远端 VERSION 带 $($script:TrialSuffix)）：$($d.count) 个文件与远端有差异。`n" +
                           '这种情况不会自动覆盖 —— 请挑要应用的文件，或先看清单再决定。'
            $rep.can_select = $true
        }
        else {
            $rep.state = 'latest'
            $rep.message = "试验版（$($rep.local)），工作区与远端没有差异。"
        }
        return [pscustomobject]$rep
    }

    # ---- 正式版：版本号变大才算有更新 ----
    $rep.has_update = ($rep.remote -and (Test-VersionNewer -Candidate $rep.remote -Current $rep.local))
    if ($rep.error) {
        $rep.state = 'unknown'
    }
    elseif ($rep.has_update) {
        $rep.state = 'stale'
        $tail = $(if ($rep.behind) { "，落后 $($rep.behind) 个提交" } else { '' })
        $rep.message = "发现新版本 $($rep.remote)（当前 $($rep.local)$tail）。"
    }
    else {
        $rep.state = 'latest'
        $rep.message = "已是最新版本（$($rep.local)）。"
    }

    # SPEC 5.1：试验版永远不给一键更新
    $rep.can_install = [bool](
        $rep.channel -eq 'git' -and $rep.has_update -and -not $rep.dirty -and $rep.mode -eq 'release'
    )
    return [pscustomobject]$rep
}

# ---------------------------------------------------------------------------
# 更新（SPEC 5.3 / 6.4 / 6.5）
# ---------------------------------------------------------------------------
function Invoke-GitSyncInstall {
    [CmdletBinding()]
    param(
        [string]$Root = (Get-Location).Path,
        [string]$Branch = $script:BranchDefault
    )

    if (-not (Test-Path -LiteralPath (Join-Path $Root '.git'))) {
        return [pscustomobject]@{ ok = $false; message = '当前目录不是 git 仓库，无法自动更新。' }
    }

    if (Test-GitSyncDirty -Root $Root) {
        $files = Get-GitSyncDirtyFiles -Root $Root
        $shown = ($files | Select-Object -First 10 | ForEach-Object { "  $_" }) -join "`n"
        $more = $(if ($files.Count -gt 10) { "`n  …还有 $($files.Count - 10) 项" } else { '' })
        return [pscustomobject]@{
            ok = $false; dirty = $true
            message = "本地有未提交的改动，已拒绝更新 —— 以免覆盖你正在改的东西。`n" +
                      "$shown$more`n请先提交（或用 git stash 暂存）后再试。"
        }
    }

    $before = (Invoke-GitCapture -Root $Root -GitArgs @('rev-parse', '--short', 'HEAD')).Out
    $p = Invoke-GitCapture -Root $Root -GitArgs @('pull', '--ff-only', 'origin', $Branch) -TimeoutSeconds 300
    if ($p.Code -ne 0) {
        return [pscustomobject]@{ ok = $false; before = $before; message = "更新失败：`n$($p.Err)$($p.Out)" }
    }

    $after = (Invoke-GitCapture -Root $Root -GitArgs @('rev-parse', '--short', 'HEAD')).Out
    $version = Get-GitSyncLocalVersion -Root $Root

    if ($before -eq $after) {
        return [pscustomobject]@{
            ok = $true; changed = $false; before = $before; after = $after; version = $version
            message = "已经是最新的（$version），没有需要拉取的内容。"
        }
    }
    return [pscustomobject]@{
        ok = $true; changed = $true; before = $before; after = $after; version = $version
        message = "更新完成：$before → $after（版本 $version）。`n重启应用后新代码才会生效。"
    }
}

# ---------------------------------------------------------------------------
# 选择性应用（SPEC 5.4 / 6.1 / 6.2 / 6.3）
# ---------------------------------------------------------------------------
function Invoke-GitSyncApply {
    <#
        把远端指定的文件落到工作区。只接受差异清单里的路径。
    #>
    [CmdletBinding()]
    param(
        [string]$Root = (Get-Location).Path,
        [string]$Branch = $script:BranchDefault,
        [Parameter(Mandatory)][string[]]$Paths
    )

    $want = @($Paths | ForEach-Object { "$_".Trim() } | Where-Object { $_ } | Select-Object -Unique)
    if ($want.Count -eq 0) {
        return [pscustomobject]@{ ok = $false; message = '没有选中任何文件' }
    }

    $diff = Get-GitSyncDiff -Root $Root -Branch $Branch
    $allowed = @{}
    foreach ($f in $diff.files) { $allowed[$f.path] = $f.status }

    $unknown = @($want | Where-Object { -not $allowed.ContainsKey($_) })
    if ($unknown.Count -gt 0) {
        # SPEC 6.1：整体拒绝，不部分执行
        return [pscustomobject]@{
            ok = $false
            message = "这些文件不在差异清单里，已拒绝：$($unknown -join '、')"
        }
    }

    # SPEC 6.2：先备份，失败则中止
    $stamp = (Get-Date).ToString('yyyyMMddTHHmmss')
    $backupDir = Join-Path (Join-Path $Root $script:BackupDir) "selection-$stamp"
    $backedUp = New-Object System.Collections.ArrayList
    try {
        foreach ($rel in $want) {
            $src = Join-Path $Root $rel
            if (-not (Test-Path -LiteralPath $src -PathType Leaf)) { continue }
            $dst = Join-Path $backupDir $rel
            $dstDir = Split-Path -Parent $dst
            if (-not (Test-Path -LiteralPath $dstDir)) { New-Item -ItemType Directory -Path $dstDir -Force | Out-Null }
            Copy-Item -LiteralPath $src -Destination $dst -Force
            [void]$backedUp.Add($rel)
        }
    }
    catch {
        return [pscustomobject]@{
            ok = $false
            message = "备份失败，已中止（未改动任何文件）：$($_.Exception.Message)"
        }
    }

    # SPEC 6.3：删除走 git rm
    $toCheckout = @($want | Where-Object { $allowed[$_] -ne 'D' })
    $toRemove = @($want | Where-Object { $allowed[$_] -eq 'D' })
    $errors = New-Object System.Collections.ArrayList

    if ($toCheckout.Count -gt 0) {
        $c = Invoke-GitCapture -Root $Root -GitArgs (@('checkout', "origin/$Branch", '--') + $toCheckout) -TimeoutSeconds 180
        if ($c.Code -ne 0) { [void]$errors.Add($c.Err + $c.Out) }
    }
    if ($toRemove.Count -gt 0) {
        $r = Invoke-GitCapture -Root $Root -GitArgs (@('rm', '-f', '--') + $toRemove) -TimeoutSeconds 180
        if ($r.Code -ne 0) { [void]$errors.Add($r.Err + $r.Out) }
    }

    $msg = "已应用 $($toCheckout.Count) 个文件"
    if ($toRemove.Count) { $msg += "，删除 $($toRemove.Count) 个" }
    if ($backedUp.Count) { $msg += "，备份 $($backedUp.Count) 个到 $backupDir" }
    $msg += $(if ($errors.Count -eq 0) { '。' } else { "，但有 $($errors.Count) 个错误。" })

    return [pscustomobject]@{
        ok        = ($errors.Count -eq 0)
        applied   = $toCheckout
        removed   = $toRemove
        backedUp  = @($backedUp)
        backupDir = $(if ($backedUp.Count) { $backupDir } else { '' })
        errors    = @($errors)
        message   = $msg
    }
}

# ---------------------------------------------------------------------------
# 上传（仅 sync 模式）
# ---------------------------------------------------------------------------
function Invoke-GitSyncPush {
    <#
        提交并推送本地改动。与 Invoke-GitSyncInstall 对称。
    #>
    [CmdletBinding()]
    param(
        [string]$Root = (Get-Location).Path,
        [string]$Branch = $script:BranchDefault,
        [string]$Message = ''
    )

    if (-not (Test-Path -LiteralPath (Join-Path $Root '.git'))) {
        return [pscustomobject]@{ ok = $false; message = '当前目录不是 git 仓库，无法上传。' }
    }

    $dirty = Get-GitSyncDirtyFiles -Root $Root
    $committed = $false

    if ($dirty.Count -gt 0) {
        $a = Invoke-GitCapture -Root $Root -GitArgs @('add', '-A')
        if ($a.Code -ne 0) {
            return [pscustomobject]@{ ok = $false; message = "git add 失败：$($a.Err)$($a.Out)" }
        }
        $note = $(if ($Message) { $Message } else { "同步：$((Get-Date).ToString('yyyy-MM-dd HH:mm'))" })
        $c = Invoke-GitCapture -Root $Root -GitArgs @('commit', '-m', $note, '--quiet')
        if ($c.Code -ne 0) {
            return [pscustomobject]@{ ok = $false; message = "git commit 失败：$($c.Err)$($c.Out)" }
        }
        $committed = $true
    }

    $p = Invoke-GitCapture -Root $Root -GitArgs @('push', 'origin', $Branch) -TimeoutSeconds 300
    if ($p.Code -ne 0) {
        return [pscustomobject]@{
            ok = $false; committed = $committed
            message = "推送失败：`n$($p.Err)$($p.Out)"
        }
    }

    return [pscustomobject]@{
        ok = $true; committed = $committed; files = $dirty.Count; branch = $Branch
        message = $(if ($committed) {
            "已提交 $($dirty.Count) 个文件并推送到 origin/$Branch。"
        } else {
            "没有本地改动，已确认 origin/$Branch 是最新的。"
        })
    }
}

Export-ModuleMember -Function @(
    'Get-GitSyncRoot'
    'Get-GitSyncReport'
    'Invoke-GitSyncInstall'
    'Get-GitSyncDiff'
    'Invoke-GitSyncApply'
    'Invoke-GitSyncPush'
)
