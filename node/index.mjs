/**
 * git-sync-kit —— Node 实现
 *
 * 严格实现仓库根的 SPEC.md（第 3~7 节）。返回对象字段与第 5 节一致，
 * 可以直接 JSON.stringify 交给前端。
 *
 * 设计取舍
 * --------
 * · 任何公开动作都不抛异常，一律 resolve 出结构化结果（SPEC 6.6）
 * · 只用 ff-only，历史分叉宁可失败（SPEC 6.5）
 * · diff 比较工作区而不是 HEAD（SPEC 5.2，实测踩过）
 * · 子进程输出重定向到临时文件，不走 pipe
 *
 * 为什么不用 pipe：Windows 沙箱下 child_process 的 stdio:'pipe' 会因命名
 * 管道被拒而 EPERM。参考实现在这里踩过，所以统一重定向到临时文件。
 */
import { spawn } from 'node:child_process'
import { existsSync, readFileSync, writeFileSync, mkdirSync, copyFileSync, unlinkSync, openSync, closeSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'

export const VERSION = '0.1.0'
export const BRANCH_DEFAULT = 'main'
export const TRIAL_SUFFIX = '*'
export const DIFF_LIMIT = 100
export const BACKUP_DIR = '_backup_previous'

// ---------------------------------------------------------------------------
// 工具
// ---------------------------------------------------------------------------

/** 跑一条 git 命令。输出走临时文件（见文件头说明），永不 reject。 */
function runGit(root, args, timeoutMs = 90000) {
  return new Promise((resolveResult) => {
    const logPath = join(
      tmpdir(),
      `gitsynckit-${process.pid}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}.log`,
    )
    let fd
    try {
      fd = openSync(logPath, 'w')
    } catch (error) {
      resolveResult({ code: 1, out: '', err: `无法创建日志文件：${error.message}` })
      return
    }

    let settled = false
    const finish = (payload) => {
      if (settled) return
      settled = true
      try { closeSync(fd) } catch { /* 已经关了 */ }
      let log = ''
      try { log = readFileSync(logPath, 'utf8') } catch { /* 读不到就算了 */ }
      try { unlinkSync(logPath) } catch { /* 删不掉也不影响结果 */ }
      resolveResult({ ...payload, out: (log || '').trim() })
    }

    let child
    try {
      child = spawn('git', ['-C', root, ...args], {
        windowsHide: true,
        stdio: ['ignore', fd, fd],
      })
    } catch (error) {
      finish({ code: 1, err: `无法启动 git：${error.message}` })
      return
    }

    const timer = setTimeout(() => {
      try { child.kill() } catch { /* 已经退出了 */ }
      finish({ code: 124, err: `git ${args.join(' ')} 超时（${Math.round(timeoutMs / 1000)} 秒）` })
    }, timeoutMs)

    child.on('error', (error) => {
      clearTimeout(timer)
      finish({ code: 1, err: `无法启动 git：${error.message}` })
    })
    child.on('close', (code) => {
      clearTimeout(timer)
      finish({ code: code === 0 ? 0 : (code ?? 1), err: '' })
    })
  })
}

/** 把 '1.2.3' 变成 [1,2,3]，容错到不会抛。 */
function parseVersion(text) {
  return String(text || '').trim().replace(/^[vV]/, '').split('.')
    .map((chunk) => {
      const digits = chunk.replace(/\D/g, '')
      return digits ? Number(digits) : 0
    })
}

function isNewer(candidate, current) {
  const a = parseVersion(candidate)
  const b = parseVersion(current)
  const n = Math.max(a.length, b.length)
  for (let i = 0; i < n; i += 1) {
    const x = a[i] ?? 0
    const y = b[i] ?? 0
    if (x !== y) return x > y
  }
  return false
}

/** 从某个目录向上找含 .git 的仓库根；找不到返回 null。 */
export function findRepo(start = process.cwd()) {
  let dir = resolve(start)
  for (;;) {
    if (existsSync(join(dir, '.git'))) return dir
    const parent = dirname(dir)
    if (parent === dir) return null
    dir = parent
  }
}

// ---------------------------------------------------------------------------
// 主体
// ---------------------------------------------------------------------------
export class GitSyncKit {
  /**
   * @param {object} [opts]
   * @param {string} [opts.root]   仓库根，省略则从 cwd 向上找
   * @param {string} [opts.branch] 跟踪分支，默认 main
   * @param {string} [opts.repo]   owner/name，仅 HTTP 兜底用
   * @param {string} [opts.mode]   'update'（单向）或 'sync'（双向）
   */
  constructor(opts = {}) {
    this.root = opts.root ? resolve(opts.root) : (findRepo() ?? process.cwd())
    this.branch = opts.branch || BRANCH_DEFAULT
    this.repo = opts.repo || ''
    this.mode = opts.mode === 'sync' ? 'sync' : 'update'
  }

  // ---- 基本信息 ----
  isGitRepo() {
    return existsSync(join(this.root, '.git'))
  }

  localVersion() {
    const vf = join(this.root, 'VERSION')
    try {
      if (existsSync(vf)) {
        const text = readFileSync(vf, 'utf8').trim()
        if (text) {
          return text.split(/\r?\n/)[0].trim().replace(/\*+$/, '').trim()
        }
      }
    } catch { /* 读不到就走退路 */ }
    try {
      const py = readFileSync(join(this.root, 'pyproject.toml'), 'utf8')
      for (const line of py.split(/\r?\n/)) {
        const t = line.trim()
        if (t.startsWith('version') && t.includes('=')) {
          return t.split('=').slice(1).join('=').trim().replace(/^["']|["']$/g, '')
        }
      }
    } catch { /* 没有就算了 */ }
    return '0.0.0'
  }

  async currentCommit(rev = 'HEAD') {
    const r = await runGit(this.root, ['rev-parse', '--short', rev])
    return r.code === 0 ? r.out : ''
  }

  async isDirty() {
    const r = await runGit(this.root, ['status', '--porcelain'])
    return r.code === 0 && Boolean(r.out)
  }

  async dirtyFiles() {
    const r = await runGit(this.root, ['status', '--porcelain'])
    return r.code === 0 && r.out ? r.out.split(/\r?\n/) : []
  }

  // ---- 远端探测（SPEC 7）----
  async _remoteViaGit() {
    const f = await runGit(this.root, ['fetch', '--quiet', 'origin', this.branch], 120000)
    if (f.code !== 0) {
      return { version: null, behind: 0, error: f.err || f.out || 'git fetch 失败' }
    }
    const s = await runGit(this.root, ['show', 'origin/' + this.branch + ':VERSION'])
    if (s.code !== 0 || !s.out) {
      // runGit 把 stdout 和 stderr 一起写进同一个临时文件，所以真正的错误在
      // out 里（err 只在 spawn 失败时才有值）。fetch 分支已经这么兜底了，这里
      // 漏过 —— 于是用户看到「origin/main 上没有 VERSION 文件」，而 git 其实
      // 说的是 fatal: path 'VERSION' does not exist in 'origin/main'。
      const name = 'origin/' + this.branch + ' 上没有 VERSION 文件'
      return { version: null, behind: 0, error: s.err || s.out || name }
    }
    const raw = s.out.split(/\r?\n/)[0].trim()
    const version = raw.replace(/^[vV]/, '').trim()

    let behind = 0
    const b = await runGit(this.root, ['rev-list', '--count', `HEAD..origin/${this.branch}`])
    if (b.code === 0 && /^\d+$/.test(b.out)) behind = Number(b.out)
    return { version, behind, error: '' }
  }

  async _remoteViaHttp() {
    if (!this.repo) return { version: null, error: '未配置 repo（owner/name），HTTP 通道不可用' }
    const url = `https://raw.githubusercontent.com/${this.repo}/${this.branch}/VERSION`
    try {
      const resp = await fetch(url, { headers: { 'User-Agent': 'git-sync-kit' } })
      if (!resp.ok) return { version: null, error: `HTTP ${resp.status}` }
      const text = (await resp.text()).trim()
      if (!text) return { version: null, error: '远端 VERSION 是空的' }
      return { version: text.split(/\r?\n/)[0].trim().replace(/\*+$/, '').trim(), error: '' }
    } catch (error) {
      return { version: null, error: String(error && error.message ? error.message : error) }
    }
  }

  // ---- 差异清单（SPEC 5.2）----
  async diff() {
    const files = []
    const st = await runGit(this.root, ['diff', '--name-status', `origin/${this.branch}`])
    if (st.code === 0 && st.out) {
      for (const line of st.out.split(/\r?\n/)) {
        const m = line.match(/^([AMDRT])\d*\t(.+)$/)
        if (m) files.push({ status: m[1], path: m[2], time: null })
      }
    }

    const timeMap = new Map()
    const lg = await runGit(this.root, ['log', '--name-only', '--format=%cI', `HEAD..origin/${this.branch}`])
    if (lg.code === 0 && lg.out) {
      let current = null
      for (const raw of lg.out.split(/\r?\n/)) {
        const text = raw.trim()
        if (!text) continue
        if (/^\d{4}-\d{2}-\d{2}T/.test(text)) { current = text; continue }
        if (current && !timeMap.has(text)) timeMap.set(text, current)
      }
    }
    for (const f of files) f.time = timeMap.get(f.path) ?? null

    const times = files.map((f) => f.time).filter(Boolean).sort()
    return {
      count: files.length,
      latestTime: times.length ? times[times.length - 1] : null,
      files: files.slice(0, DIFF_LIMIT),
      truncated: files.length > DIFF_LIMIT,
    }
  }

  async _aheadCount() {
    const r = await runGit(this.root, ['rev-list', '--count',
      'origin/' + this.branch + '..HEAD'])
    return (r.code === 0 && /^\d+$/.test(r.out)) ? Number(r.out) : 0
  }

  /**
   * 冲突清单（SPEC 第 6 节）：以 merge-base 为基准，本地改过的文件与远端改过的
   * 文件取交集。**只读，不动任何文件。**
   *
   * 没有分叉点就不存在冲突 —— 那种情况只有「落后」或「领先」。
   */
  async conflict() {
    const empty = { count: 0, files: [] }
    const mb = await runGit(this.root, ['merge-base', 'HEAD', 'origin/' + this.branch])
    if (mb.code !== 0 || !mb.out) return empty
    const base = mb.out.split(/\r?\n/)[0].trim()
    if (!base) return empty

    const mine = new Set()
    const m = await runGit(this.root, ['diff', '--name-only', base, 'HEAD'])
    if (m.code === 0) {
      for (const p of m.out.split(/\r?\n/)) if (p.trim()) mine.add(p.trim())
    }

    // 工作区里未提交的也算「本地改过」
    for (const line of await this.dirtyFiles()) {
      let text = line.length > 3 ? line.slice(3).trim() : ''
      if (text.includes(' -> ')) text = text.split(' -> ').pop()
      if (text) mine.add(text.replace(/^"|"$/g, ''))
    }

    const common = []
    const t = await runGit(this.root, ['diff', '--name-only', base, 'origin/' + this.branch])
    if (t.code === 0) {
      for (const raw of t.out.split(/\r?\n/)) {
        const p = raw.trim()
        if (p && mine.has(p)) common.push(p)
      }
    }
    common.sort()
    return {
      count: common.length,
      files: common.map((p) => ({ path: p, reason: 'both-modified' })),
    }
  }

  // ---- 检查（SPEC 5.1）----
  async check() {
    const NL = String.fromCharCode(10)
    const rep = {
      ok: true, state: 'unknown', workMode: this.mode,
      local: this.localVersion(), remote: null,
      has_update: false, behind: 0, ahead: 0, dirty: false,
      is_git: this.isGitRepo(), channel: 'none',
      branch: this.branch, message: '', error: null,
      can_install: false, can_select: false, can_push: false,
      diff: null, conflict: null,
    }

    if (rep.is_git) {
      rep.dirty = await this.isDirty()
      const remote = await this._remoteViaGit()
      if (remote.version) {
        rep.remote = remote.version
        rep.behind = remote.behind
        rep.channel = 'git'
      } else {
        const http = await this._remoteViaHttp()
        if (http.version) {
          rep.remote = http.version
          rep.channel = 'http'
          rep.error = `git 通道不可用（${remote.error}），已改用 HTTP 查询；此模式下无法安装或应用。`
        } else {
          rep.ok = false
          rep.state = 'error'
          rep.error = `git 通道失败：${remote.error}；HTTP 兜底也失败：${http.error}`
          rep.message = rep.error
          return rep
        }
      }
    } else {
      const http = await this._remoteViaHttp()
      if (http.version) {
        rep.remote = http.version
        rep.channel = 'http'
        rep.has_update = isNewer(http.version, rep.local)
        rep.state = rep.has_update ? 'stale' : 'latest'
        rep.message = rep.has_update
          ? '发现新版本 ' + rep.remote + '（当前 ' + rep.local + '）。此模式下只能查看，无法更新。'
          : '已是最新版本（' + rep.local + '）。'
      } else {
        rep.ok = false
        rep.state = 'error'
        rep.error = '不是 git 仓库，且 HTTP 查询失败：' + http.error
        rep.message = rep.error
      }
      return rep
    }

    // 冲突只可能出现在真分叉上（本地和远端各有提交）
    if (rep.channel === 'git') {
      rep.ahead = await this._aheadCount()
      if (rep.behind > 0 && rep.ahead > 0) {
        rep.conflict = await this.conflict()
      }
    }
    const hasConflict = Boolean(rep.conflict && rep.conflict.count > 0)

    if (rep.error) {
      rep.state = 'unknown'
      rep.message = rep.error
      return rep
    }

    // ---- 按工作模式判定（SPEC 第 2 节）----
    if (this.mode === 'sync') {
      if (rep.channel === 'git' && rep.behind > 0) {
        rep.has_update = true
        rep.state = 'stale'
        rep.message = '远端有 ' + rep.behind + ' 个提交未拉取。'
      } else if (rep.channel === 'git' && rep.ahead > 0) {
        rep.state = 'ahead'
        rep.message = '本地有 ' + rep.ahead + ' 个提交未推送（工作区'
          + (rep.dirty ? '有' : '无') + '未提交改动）。'
      } else if (rep.dirty) {
        rep.state = 'ahead'
        rep.message = '工作区有未提交的改动，可以上传。'
      } else {
        rep.state = 'latest'
        rep.message = '已同步（' + rep.local + '）。'
      }
    } else {
      rep.has_update = Boolean(rep.remote && isNewer(rep.remote, rep.local))
      if (rep.has_update) {
        rep.state = 'stale'
        const tail = rep.behind ? '，落后 ' + rep.behind + ' 个提交' : ''
        rep.message = '发现新版本 ' + rep.remote + '（当前 ' + rep.local + tail + '）。'
      } else {
        rep.state = 'latest'
        rep.message = '已是最新版本（' + rep.local + '）。'
      }
    }

    // ---- 差异清单：只要有得拉就算出来，与状态无关 ----
    if (rep.channel === 'git' && rep.behind > 0) {
      rep.diff = await this.diff()
      if (this.mode === 'update' && rep.state === 'latest') {
        rep.message += '远端另有 ' + rep.behind + ' 个提交未升版 —— 打开窗口可以看差异。'
      }
    }

    if (hasConflict) {
      rep.message += NL + '注意：' + rep.conflict.count
        + ' 个文件本地和远端都改过，拉取与上传都已停用，请先手动处理。'
    }

    // SPEC 5.1：三个能力字段
    rep.can_install = rep.channel === 'git' && rep.has_update && !rep.dirty && !hasConflict
    rep.can_select = rep.channel === 'git' && Boolean(rep.diff && rep.diff.count > 0)
    rep.can_push = this.mode === 'sync' && rep.channel === 'git' && !hasConflict
      && (rep.ahead > 0 || rep.dirty)
    return rep
  }

  // ---- 更新（SPEC 5.3 / 6.4 / 6.5）----
  async install() {
    if (!this.isGitRepo()) {
      return { ok: false, message: '当前目录不是 git 仓库，无法自动更新。请手动下载新版。' }
    }
    if (await this.isDirty()) {
      const files = await this.dirtyFiles()
      const shown = files.slice(0, 10).map((l) => `  ${l}`).join('\n')
      const more = files.length > 10 ? `\n  …还有 ${files.length - 10} 项` : ''
      return {
        ok: false, dirty: true,
        message: '本地有未提交的改动，已拒绝更新 —— 以免覆盖你正在改的东西。\n'
          + shown + more + '\n请先提交（或用 git stash 暂存）后再试。',
      }
    }

    // SPEC 7.5：有冲突时拒绝拉取。带着分叉去快进合并，git 只会给一句和「冲突」
    // 无关的报错；不如在这里就把它翻译成人话。
    const c = await this.conflict()
    if (c.count) {
      const names = c.files.slice(0, 10).map((f) => f.path).join('、')
      return {
        ok: false, conflict: true, files: c.files.map((f) => f.path),
        message: '有 ' + c.count + ' 个文件本地和远端都改过，已拒绝更新：' + names
          + '\n请先手动处理（merge、或选一边）再试。',
      }
    }

    const before = await this.currentCommit()
    const p = await runGit(this.root, ['pull', '--ff-only', 'origin', this.branch], 300000)
    if (p.code !== 0) {
      return { ok: false, before, message: `更新失败：\n${p.err || p.out}` }
    }

    const after = await this.currentCommit()
    const version = this.localVersion()
    if (before === after) {
      return {
        ok: true, changed: false, before, after, version,
        message: `已经是最新的（${version}），没有需要拉取的内容。`,
      }
    }
    return {
      ok: true, changed: true, before, after, version,
      message: `更新完成：${before} → ${after}（版本 ${version}）。\n重启应用后新代码才会生效。`,
    }
  }

  // ---- 选择性应用（SPEC 5.4 / 6.1 / 6.2 / 6.3）----
  async apply(paths = []) {
    const want = [...new Set((paths || []).map((p) => String(p).trim()).filter(Boolean))]
    if (!want.length) return { ok: false, message: '没有选中任何文件' }

    const d = await this.diff()
    const allowed = new Map(d.files.map((f) => [f.path, f.status]))
    const unknown = want.filter((p) => !allowed.has(p))
    if (unknown.length) {
      // SPEC 6.1：整体拒绝，不部分执行
      return { ok: false, message: `这些文件不在差异清单里，已拒绝：${unknown.join('、')}` }
    }

    // SPEC 6.2：先备份，失败则中止
    const stamp = new Date().toISOString().replace(/[-:]/g, '').replace(/\..+$/, '')
    const backupDir = join(this.root, BACKUP_DIR, `selection-${stamp}`)
    const backedUp = []
    try {
      for (const rel of want) {
        const src = join(this.root, rel)
        if (!existsSync(src)) continue
        const dst = join(backupDir, rel)
        mkdirSync(dirname(dst), { recursive: true })
        copyFileSync(src, dst)
        backedUp.push(rel)
      }
    } catch (error) {
      return { ok: false, message: `备份失败，已中止（未改动任何文件）：${error.message}` }
    }

    // SPEC 6.3：删除走 git rm
    const toCheckout = want.filter((p) => allowed.get(p) !== 'D')
    const toRemove = want.filter((p) => allowed.get(p) === 'D')
    const errors = []

    if (toCheckout.length) {
      const c = await runGit(this.root, ['checkout', `origin/${this.branch}`, '--', ...toCheckout], 180000)
      if (c.code !== 0) errors.push(c.err || c.out || 'git checkout 失败')
    }
    if (toRemove.length) {
      const r = await runGit(this.root, ['rm', '-f', '--', ...toRemove], 180000)
      if (r.code !== 0) errors.push(r.err || r.out || 'git rm 失败')
    }

    let message = `已应用 ${toCheckout.length} 个文件`
    if (toRemove.length) message += `，删除 ${toRemove.length} 个`
    if (backedUp.length) message += `，备份 ${backedUp.length} 个到 ${backupDir}`
    message += errors.length ? `，但有 ${errors.length} 个错误。` : '。'

    return {
      ok: errors.length === 0,
      applied: toCheckout,
      removed: toRemove,
      backedUp,
      backupDir: backedUp.length ? backupDir : '',
      errors,
      message,
    }
  }

  // ---- 上传（仅 sync 模式）----
  async push(message = '') {
    // SPEC 7.7：调用方必须先列出待提交条目、由人确认；这里再查一次模式与冲突，
    // 免得有人绕过界面直接调。
    if (this.mode !== 'sync') {
      return { ok: false, message: '当前是单向更新模式，上传入口没开。' }
    }
    if (!this.isGitRepo()) {
      return { ok: false, message: '当前目录不是 git 仓库，无法上传。' }
    }

    // SPEC 7.5：有冲突时拒绝推送
    const cf = await this.conflict()
    if (cf.count) {
      const names = cf.files.slice(0, 10).map((f) => f.path).join('、')
      return {
        ok: false, conflict: true, files: cf.files.map((f) => f.path),
        message: '有 ' + cf.count + ' 个文件本地和远端都改过，已拒绝推送：' + names
          + '\n请先手动处理（merge、或选一边）再试。',
      }
    }
    const b = await runGit(this.root, ['rev-parse', '--abbrev-ref', 'HEAD'])
    const branch = b.out || this.branch

    const dirty = await this.dirtyFiles()
    let committed = false
    if (dirty.length) {
      const a = await runGit(this.root, ['add', '-A'])
      if (a.code !== 0) return { ok: false, message: `git add 失败：${a.err || a.out}` }

      const note = message || `同步：${new Date().toISOString().slice(0, 16).replace('T', ' ')}`
      const c = await runGit(this.root, ['commit', '-m', note, '--quiet'])
      if (c.code !== 0) return { ok: false, message: `git commit 失败：${c.err || c.out}` }
      committed = true
    }

    const p = await runGit(this.root, ['push', 'origin', branch], 300000)
    if (p.code !== 0) {
      return { ok: false, committed, message: `推送失败：\n${p.err || p.out}` }
    }
    return {
      ok: true, committed, files: dirty.length, branch,
      message: committed
        ? `已提交 ${dirty.length} 个文件并推送到 origin/${branch}。`
        : `没有本地改动，已确认 origin/${branch} 是最新的。`,
    }
  }
}

export default GitSyncKit
