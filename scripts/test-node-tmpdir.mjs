/**
 * 测：临时目录不可写时，Node 实现要能自己回退（2026-10-17 修）。
 *
 * 背景（踩过的坑）
 * ---------------
 * `runGit` 原来无条件把子进程输出重定向到 `os.tmpdir()` 下的日志文件。受限进程
 * （沙箱、受限令牌）里那个目录可能**根本不可写**，于是每一条 git 命令都直接返回
 *
 *     无法创建日志文件：EPERM: operation not permitted, open
 *     'C:\Users\...\AppData\Local\Temp\gitsynckit-<pid>-<ts>-<rand>.log'
 *
 * 而 check() 会把这条当「git 通道失败」，于是整个 Node 实现看起来像坏了一样 ——
 * 实际只是没地方写日志。更糟的是文件头当时的注释把这个 EPERM **归因给了 pipe**，
 * 让人一直往错的方向查（实测 pipe 一直好用）。
 *
 * 这个测试把 TMPDIR/TMP/TEMP 全指到一个写不进去的目录，验证仍然能跑通：
 *   1. 不可写时也能 check 出正确结果（回退到能找到的目录）
 *   2. 报错里不该再出现「无法创建日志文件」
 *
 * 用法:
 *     node scripts/test-node-tmpdir.mjs
 */
import { spawnSync } from 'node:child_process'
import { existsSync, mkdirSync, rmSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const REPO = resolve(HERE, '..')
const SCRATCH = 'D:\\CAD Tool\\_tmp'
const WORK = join(SCRATCH, 'node-tmpdir-test')

let failures = 0
const check = (label, ok, detail = '') => {
  if (ok) console.log(`  ✓ ${label}`)
  else { console.log(`  ✗ ${label}   ${detail}`); failures++ }
}

const git = (args, cwd) => {
  const r = spawnSync('git', args, { cwd, encoding: 'utf8', windowsHide: true })
  if (r.status !== 0) throw new Error(`git ${args.join(' ')} 失败：${r.stderr || r.stdout}`)
  return r.stdout
}

// 一个**写不进去**的目录：Windows 的 System32（普通权限下必然被拒）
const READONLY = 'C:\\Windows\\System32'

function buildRepo() {
  rmSync(WORK, { recursive: true, force: true })
  mkdirSync(WORK, { recursive: true })
  const origin = join(WORK, 'origin.git')
  mkdirSync(origin)
  git(['init', '--bare', '--initial-branch=main', '.'], origin)

  const seed = join(WORK, 'seed')
  mkdirSync(seed)
  git(['init', '--initial-branch=main', '.'], seed)
  writeFileSync(join(seed, 'VERSION'), '1.0.0\n')
  writeFileSync(join(seed, 'README.md'), '# seed\n')
  git(['add', '-A'], seed)
  git(['-c', 'user.email=t@l', '-c', 'user.name=t', 'commit', '-m', 'seed'], seed)
  git(['remote', 'add', 'origin', origin], seed)
  git(['push', '-u', 'origin', 'main'], seed)

  const local = join(WORK, 'local')
  git(['clone', origin, local], WORK)
  return local
}

console.log()
console.log('=== 把 TMPDIR / TMP / TEMP 全指到不可写的目录 ===')
console.log(`  不可写目录: ${READONLY}`)

const local = buildRepo()

// 环境变量必须在 import 之前设好（模块可能缓存探测结果）
process.env.TMPDIR = READONLY
process.env.TMP = READONLY
process.env.TEMP = READONLY

const { default: GitSyncKit } = await import(
  `file:///${REPO.replace(/\\/g, '/').replace(/ /g, '%20')}/node/index.mjs`
)

const kit = new GitSyncKit({ root: local, branch: 'main', mode: 'sync' })
const rep = await kit.check()

console.log()
console.log('=== 检查结果 ===')
console.log(`  state=${rep.state} channel=${rep.channel} local=${rep.local} remote=${rep.remote}`)
console.log(`  message=${rep.message}`)

check('临时目录不可写时仍能跑通（回退生效）', rep.ok === true,
      `ok=${rep.ok} error=${rep.error}`)
check('channel 是 git（不是 none）', rep.channel === 'git', `channel=${rep.channel}`)
check('读到了本机版本', rep.local === '1.0.0', `local=${rep.local}`)
check('报错里不再出现「无法创建日志文件」',
      !String(rep.error || '').includes('无法创建日志文件')
      && !String(rep.message || '').includes('无法创建日志文件'),
      `error=${rep.error}`)

// 写操作也要能跑（不只是读）
writeFileSync(join(local, 'notes.md'), 'hello\n')
const diff = await kit.diff()
check('diff 也能跑（写日志同样回退）', Array.isArray(diff.files), `diff=${JSON.stringify(diff)}`)

// 清理：把可能产生的回退目录也收掉
for (const d of [join(REPO, '_tmp'), join(local, '.gitsync-tmp'), WORK]) {
  try { rmSync(d, { recursive: true, force: true }) } catch { /* 删不掉不影响结论 */ }
}

console.log()
if (failures === 0) {
  console.log('结论: 通过 —— 临时目录不可写时 Node 实现能自己回退')
  process.exit(0)
}
console.log(`结论: 失败 ${failures} 项`)
process.exit(1)
