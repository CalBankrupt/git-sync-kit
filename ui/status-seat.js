/**
 * git-sync-kit —— 状态位组件
 *
 * 一个纯图标按钮，图形本身就是状态（见 SPEC.md 第 4 节）。零依赖，可以在
 * 任何有 DOM 的地方用：原生页面、WebView、Electron、pywebview……
 *
 * 用法
 * ----
 *   const seat = GitSyncSeat.create({
 *     anchor: '#my-button',            // 克隆哪个已有按钮（推荐：能继承宿主样式）
 *     tooltipPrefix: '我的项目：',      // 可选
 *     onActivate: async (state) => {   // 点击时调用，返回新的状态对象
 *       if (state === 'stale' || state === 'trial') { openUpdateWindow(); return null; }
 *       return await myApi.check();    // { state, message, diff }
 *     },
 *   });
 *   seat.setState('latest', '已是最新版本');
 *
 * 为什么克隆已有按钮而不是自己拼一个：尺寸、class、SVG 宽高、主题配色全都
 * 由宿主决定，克隆能一次全继承。自己画一个必然要逐个对齐，上游一改就掉队。
 */

const SHAPES = {
  idle: [
    '<path d="M21 12a9 9 0 1 1-3.2-6.9"/>',
    '<path d="M21 3v6h-6"/>',
  ],
  latest: ['<path d="M20 6L9 17l-5-5"/>'],
  stale: ['<path d="M12 19V5"/>', '<path d="M5 12l7-7 7 7"/>'],
  // 问号 = 试验版有差异，要人来定
  trial: [
    '<path d="M9.1 9a3 3 0 1 1 4.2 2.7c-.8.4-1.3 1.1-1.3 2"/>',
    '<path d="M12 17.5h.01"/>',
  ],
  error: [
    '<path d="M21 12a9 9 0 1 1-3.2-6.9"/>',
    '<path d="M21 3v6h-6"/>',
    '<path d="M20.5 3.5L3.5 20.5"/>',
  ],
}
// busy 与 unknown 复用环形箭头；busy 额外旋转
SHAPES.busy = SHAPES.idle
SHAPES.unknown = SHAPES.idle

const TITLES = {
  idle: '检查更新 / Check for updates',
  busy: '正在检查更新… / Checking for updates...',
  latest: '已是最新版本 / Up to date',
  stale: '有新版本可用，点击查看 / Update available, click to review',
  trial: '试验版有差异，点击查看清单（不会改动文件） / Trial build differs, click to review (read-only)',
  unknown: '已检查，但无法判断是否有新版本 / Checked, cannot tell',
  error: '检查失败，点击重试 / Check failed, click to retry',
}

// 颜色只做辅助：状态已经由图形表达。留空 = 继承宿主主题。
const COLORS = {
  idle: '',
  busy: '#93c5fd',
  latest: '#86efac',
  stale: '#60a5fa',
  trial: '#fbbf24',
  unknown: '',
  error: '#fca5a5',
}

const STYLE_ID = 'gitsync-seat-style'
const CSS = `
@keyframes gitsync-seat-spin { to { transform: rotate(360deg); } }
@keyframes gitsync-seat-pulse { 0%, 100% { opacity: 1; } 50% { opacity: .4; } }
.gitsync-seat-busy svg { animation: gitsync-seat-spin 1.8s linear infinite; transform-origin: center; }
.gitsync-seat-pulse { animation: gitsync-seat-pulse 1.6s ease-in-out infinite; }
`

function ensureStyle(doc) {
  if (doc.getElementById(STYLE_ID)) return
  const style = doc.createElement('style')
  style.id = STYLE_ID
  style.textContent = CSS
  doc.head.appendChild(style)
}

/**
 * 创建一个状态位。
 *
 * @param {object}   opts
 * @param {Element|string} opts.anchor   已有按钮（元素或选择器），克隆它
 * @param {string}   [opts.id]           新按钮的 id，默认 gitsync-seat
 * @param {string}   [opts.tooltipPrefix] 提示前缀，如「novel-workflow：」
 * @param {Function} [opts.onActivate]   点击回调 (state) => Promise<report|null>
 * @param {boolean}  [opts.autoCheck]    是否在创建后自动查一次，默认 true
 * @param {number}   [opts.autoCheckDelay] 自动检查延迟（毫秒），默认 3000
 * @returns {{ setState, getState, setDetail, el, destroy }}
 */
export function createStatusSeat(opts = {}) {
  const doc = opts.document || document
  const anchor = typeof opts.anchor === 'string'
    ? doc.querySelector(opts.anchor)
    : opts.anchor
  if (!anchor || !anchor.parentElement) {
    throw new Error('git-sync-kit: anchor 不存在或没有父元素')
  }
  ensureStyle(doc)

  const btn = anchor.cloneNode(true)
  btn.removeAttribute('id')
  btn.id = opts.id || 'gitsync-seat'
  ;['data-tooltip', 'data-base-tooltip', 'title'].forEach((a) => btn.removeAttribute(a))
  btn.style.cursor = 'pointer'

  const svg = btn.querySelector('svg')
  if (svg) {
    svg.setAttribute('viewBox', '0 0 24 24')
    svg.innerHTML = SHAPES.idle.join('')
  }

  // 放在锚点之后（调用方也可以自行移动）
  anchor.parentElement.insertBefore(btn, anchor.nextSibling)

  let state = 'idle'
  let detail = ''
  const prefix = opts.tooltipPrefix || ''

  function apply(stateName, message) {
    state = SHAPES[stateName] ? stateName : 'idle'
    if (message !== undefined) detail = message || ''

    const icon = btn.querySelector('svg')
    if (icon) icon.innerHTML = (SHAPES[state] || SHAPES.idle).join('')

    btn.classList.toggle('gitsync-seat-busy', state === 'busy')
    btn.classList.toggle('gitsync-seat-pulse', state === 'stale')
    btn.style.color = COLORS[state] || ''

    let tip = prefix + (TITLES[state] || TITLES.idle)
    if (detail) tip += ' — ' + detail
    btn.setAttribute('data-tooltip', tip)
    btn.setAttribute('data-base-tooltip', tip)
    btn.disabled = state === 'busy'
  }

  async function activate() {
    if (state === 'busy') return
    if (typeof opts.onActivate !== 'function') return
    const previous = state
    apply('busy')
    try {
      const report = await opts.onActivate(previous)
      if (report && report.state) apply(report.state, report.message)
      else apply(previous === 'busy' ? 'idle' : previous)
    } catch (error) {
      apply('error', String(error && error.message ? error.message : error))
    }
  }

  btn.addEventListener('click', activate)
  apply('idle')

  // 自动检查：独立调度，不等按钮被插入 DOM 的时机。
  // （把它挂在插入函数里会导致首次找不到锚点时永远不触发，实测踩过。）
  let timer = null
  if (opts.autoCheck !== false) {
    timer = setTimeout(() => {
      if (state === 'idle') activate()
    }, opts.autoCheckDelay ?? 3000)
  }

  return {
    el: btn,
    getState: () => state,
    setState: (s, message) => apply(s, message),
    setDetail: (text) => apply(state, text),
    destroy() {
      if (timer) clearTimeout(timer)
      btn.removeEventListener('click', activate)
      btn.remove()
    },
  }
}

export const shapes = SHAPES
export const titles = TITLES
export const colors = COLORS

export default { create: createStatusSeat, shapes: SHAPES, titles: TITLES, colors: COLORS }
