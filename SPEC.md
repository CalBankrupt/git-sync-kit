# git-sync-kit 规范

**版本 0.1.0** · 状态：草案

---

## 1. 这个规范解决什么问题

同一套「检查远端有没有新东西 → 决定要不要拿下来 → 拿下来」的逻辑，在几个项目里
被反复重写，每次都踩一遍同样的坑：

| 项目 | 语言 | 场景 |
|---|---|---|
| [novel-workflow](https://github.com/CalBankrupt/novel-workflow) | Node + PowerShell | 分发式更新（作者发版，用户拉取） |
| [OCP-Viewer-Desktop](https://github.com/CalBankrupt/OCP-Viewer-Desktop) | Python + JS | 同上，带图形界面 |
| [cad-workspace](https://github.com/CalBankrupt/cad-workspace) | PowerShell | **双向同步**（自己的多台机器互传） |

它们的**状态语义、安全底线、界面语言是同一套**，只有动作方向不同。本规范把这
三者固定下来，让各语言实现可以独立开发但行为一致。

**适用范围**：任何「本地目录 ↔ git 远端」的代码/文件同步场景。

---

## 2. 两种模式

| 模式 | 方向 | 典型场景 | 关键差异 |
|---|---|---|---|
| **`update`** | 单向，远端 → 本地 | 软件分发 | 可以整份覆盖（`git pull --ff-only`） |
| **`sync`** | 双向 | 多机互传工作成果 | **绝不**在本地有未提交改动时拉取 |

两种模式共用状态机与安全约束，区别只在 `install` 动作的实现：
`update` 用 `pull --ff-only`；`sync` 同样用 `pull --ff-only`，但因为本地可能有
未上传成果，它对"本地脏"的拒绝更严格，并且通常还要提供上传动作。

---

## 3. 版本号与试验版

版本号读仓库根的 `VERSION` 文件（纯文本，一行）。

| 远端 VERSION | 含义 | 行为 |
|---|---|---|
| `1.2.3` | **正式版** | 版本号变大 → `stale`，允许一键更新 |
| `1.2.4*` | **试验版** | 只看差异，不看版本号大小 → 有差异则 `trial` |
| 读不到 / 不是 git 仓库 | —— | 退到 HTTP 只读查询，`channel = "http"` |

**约定**：`*` 后缀由**发布方**在远端打，本地只管读。去掉 `*` 之后才是版本号本身。

### 为什么要有试验版

> 正式流程里更新就该升版本号；**不升版本号却推了提交，说明这是试验版**，
> 本地很可能有实验性改动，**不该让脚本自行决定覆盖**，而应把差异摊开给人看。

这是从 novel-workflow 沿用的判断。它把「可以直接覆盖」和「必须人工挑选」分成
两种发布意图，而不是让脚本去猜。

---

## 4. 状态机

界面（状态位 / 图标 / 徽章）在任何实现里都必须表达这七种状态：

| 状态 | 图形 | 颜色 | 含义 | 可点击动作 |
|---|---|---|---|---|
| `idle` | 环形箭头 | 继承 | 空闲，尚未检查 | `check` |
| `busy` | 环形箭头 + **旋转** | 浅蓝 | 正在执行 | 无（禁用） |
| `latest` | **✓** | 绿 | 已是最新 | `check` |
| `stale` | **↑** | 蓝 | 有正式版新版本 | **`install`** |
| `trial` | **?** | 琥珀 | 试验版有差异，需人工挑 | `check`（刷新清单） |
| `error` | 箭头 + 右→左斜划线 | 红 | 检查失败 | `check`（重试） |
| `unknown` | 环形箭头 | 继承 | 已检查但判断不出 | `check` |

**图形即状态，颜色只做辅助**：颜色继承宿主主题，不要硬编码成某个界面的配色。
判断不出结果时宁可用 `unknown`，也不要猜成 `latest`。

### 两段式点击（必须）

```
点击 ──┬─ 状态是 stale 或 trial ─→ 执行该状态的真正动作（install / 打开清单）
       └─ 其他状态 ──────────────→ check（原地检查，不改动任何文件）
```

**一次点击不应该直接改动文件。** 第一次点击只做只读的检查，看清状态后再点第二
次才动文件——这样误触不会造成损失。

### 旋转动画

`busy` 的旋转周期建议 **1.8 秒/圈**。太快显得焦躁，而且这里表达的只是「在跑」，
不是进度（真进度用不定进度条表达）。

> 实现注意：用 CSS `@keyframes`，**不要**用 SVG `<animateTransform>`。后者的
> `from` 需要「角度 + cx + cy」三个值，少写一个会**静默不转**。

---

## 5. JSON 契约

所有语言实现必须能产出/接受下列结构（字段名固定，大小写敏感）。

### 5.1 `check` → `Report`

```jsonc
{
  "ok": true,
  "state": "stale",           // latest | stale | trial | error | unknown
                              // （idle / busy 由调用方自行管理，不在响应里）
  "mode": "release",          // release | trial
  "local": "0.1.1",           // 本地 VERSION（去掉 * 之后）
  "remote": "0.2.0",          // 远端 VERSION（去掉 * 之后）；查不到为 null
  "has_update": true,
  "behind": 3,                // 本地落后的提交数
  "dirty": false,             // 工作区是否有未提交改动
  "is_git": true,
  "channel": "git",           // git | http | none
  "branch": "main",
  "message": "发现新版本 0.2.0（当前 0.1.1）。",
  "error": null,
  "can_install": true,        // 允许一键更新
  "can_select": false,        // 允许勾选文件应用
  "diff": null                // 试验模式才有，见 5.2
}
```

**`can_install` 必须同时满足**：
`channel == "git"` **且** `has_update` **且** `!dirty` **且** `mode == "release"`。

**`can_select` 必须同时满足**：
`channel == "git"` **且** `mode == "trial"` **且** `diff.count > 0`。

即：**试验版永远不给一键更新**，正式版永远不给勾选应用。

### 5.2 `diff` 对象（仅试验模式）

```jsonc
{
  "count": 3,
  "latestTime": "2026-10-17T01:20:00+08:00",   // ISO 8601，可为 null
  "truncated": false,                          // 是否因上限被截断
  "files": [
    { "status": "M", "path": "app/main.py",      "time": "2026-10-17T01:20:00+08:00" },
    { "status": "A", "path": "docs/note.md",     "time": "2026-10-16T22:05:00+08:00" },
    { "status": "D", "path": "legacy/old.py",    "time": "2026-10-15T09:30:00+08:00" }
  ]
}
```

`status` 取值沿用 git 的 `--name-status`：`A` 新增 / `M` 修改 / `D` 删除 /
`R` 改名 / `T` 类型变更。

**建议上限 100 个**，超出时截断并置 `truncated = true`。

**比较基准必须是「工作区 vs `origin/<branch>`」，不是 `HEAD` vs `origin/<branch>`。**

> 这一条最容易写错：选择性应用只把文件写进工作区、**并不移动 HEAD**。若按提交
> 比较，应用完之后差异依然会显示存在（实测踩过：三个文件内容早已与远端一致，
> 界面却一直列着它们）。

### 5.3 `install` → 结果

```jsonc
{
  "ok": true,
  "changed": true,            // false = 本来就没有可拉的
  "before": "a1b2c3d",        // 更新前提交号（便于回滚）
  "after": "e4f5g6h",
  "version": "0.2.0",
  "message": "更新完成：a1b2c3d → e4f5g6h（版本 0.2.0）。"
}
```

失败时 `ok: false`，`message` 说明原因；本地脏时额外带 `"dirty": true`。

### 5.4 `apply` → 结果（仅试验模式）

请求：`{ "paths": ["app/main.py", "docs/note.md"] }`

```jsonc
{
  "ok": true,
  "applied": ["app/main.py", "docs/note.md"],   // 写入的
  "removed": ["legacy/old.py"],                 // 删除的
  "backedUp": ["app/main.py", "docs/note.md"],  // 备份了的
  "backupDir": "/repo/_backup_previous/selection-20261017T012000",
  "errors": []
}
```

---

## 6. 安全约束（强制，不可省略）

以下每一条都是踩出来的，实现时必须遵守：

### 6.1 路径白名单

**只接受当前差异清单里出现过的路径。** 调用方传进来的路径若不在清单内，必须整体
拒绝并说明，不得部分执行。

> 否则调用方可以借此读写仓库里的任意文件（包括 `../` 逃逸到仓库外）。

### 6.2 应用前备份

把**受影响的现有文件**复制到 `_backup_previous/selection-<时间戳>/`，保持相对
目录结构。备份失败则**中止整个操作**，不做任何改动。

> `git checkout` 会覆盖未提交的改动，而那样的改动 **git 自己救不回来**。

### 6.3 删除走 `git rm`

远端已删除的文件用 `git rm -f --`，不要用 `checkout`（后者对不存在的路径会报错）。

### 6.4 本地脏时拒绝一键更新

工作区有未提交改动时，`install` 必须**拒绝执行**并提示先提交或 stash。

> 宁可更新失败，也不丢用户正在改的东西。**不要**用 `reset --hard` 兜底。

### 6.5 只用快进合并

`install` 用 `git pull --ff-only`。历史分叉时应当失败并让人处理，不要自动 merge
或 rebase——那会产生用户没预期的提交。

### 6.6 错误不得抛给界面

所有公开动作返回结构化结果（`ok` + `message`），不要抛异常到 UI 层。一个坏文件
或一次网络故障不该让整个应用挂掉。

---

## 7. 通道选择

**优先 git 通道，HTTP 只作兜底。**

| 通道 | 用途 | 能力 |
|---|---|---|
| `git` | fetch + 读 `origin/<branch>:VERSION` | 只读查询 + 完整安装/应用 |
| `http` | 直接取 `raw.githubusercontent.com` 上的 `VERSION` | **仅**看版本号，不能安装 |
| `none` | 都不通 | 报错 |

> 为什么不用 release tag：tag 指向发布当时的提交，之后 main 上补的文件（`VERSION`
> 本身就在其列）在 tag 里根本没有，更新会取到空。

HTTP 通道下 `can_install` 与 `can_select` **必须为 `false`** —— 看得到却装不了，
也比给一个会失败的按钮好。

---

## 8. 界面语言

### 8.1 状态位

一个纯图标按钮，图形见第 4 节。要求：

- 打开应用后**自动检查一次**，图标一上来就反映真实状态
- 悬停提示：`<状态说明> / <English>`，双语，用 ` / ` 分隔
- 试验版的悬停提示应列出**前 5 个差异文件**及总数，格式：
  `3 个文件有差异｜M app/main.py；A docs/note.md；…还有 1 个（点击查看清单）`
- 尺寸、字号、外观应**克隆宿主已有的同类按钮**，而不是自己拼一个——这样配色、
  尺寸、主题跟随全都自动一致

> 自动检查**不要**挂在进行 DOM 插入的函数里：那个函数要等锚点渲染出来才跑得成，
> 首次找不到就会早退，自动检查跟着永远不发生（实测表现：图标一直停在 `idle`）。
> 应当独立调度，只等后端通道就绪。

### 8.2 更新窗口（可选）

呈现 `Report` 的完整信息：本地/远端版本、状态、消息、日志，以及操作按钮。

试验模式下改为显示**差异清单**：复选框 + 状态徽章 + 路径 + 最近改动时间 +
「应用选中（N）」。应用前应弹确认，并说明备份位置。

---

## 9. 附：状态判定速查

```
读远端 VERSION
├── 读不到 ────────────────→ error（或退 HTTP）
├── 带 *  ─── 试验版 ──────→ diff.count > 0 ? trial : latest
└── 不带 * ── 正式版 ──────→ remote > local ? stale : latest

can_install = git ∧ has_update ∧ !dirty ∧ release
can_select  = git ∧ trial ∧ diff.count > 0

点击：
  stale → install
  trial → 打开清单
  其他  → check
```

---

## 10. 实现清单

| 语言 | 位置 | 状态 |
|---|---|---|
| Python | `python/gitsynckit/` | ✅ |
| PowerShell | `powershell/GitSyncKit/` | ✅ |
| Node | `node/index.mjs` | ✅ |
| 状态位组件 | `ui/status-seat.js` | ✅ |

各实现的公开接口、返回值必须与本规范第 5 节一致。语言特有的便利方法可以额外提供，
但不得改变上述字段的含义。

---

*本规范的判断依据来自三个项目的实际踩坑记录，每一条安全约束都对应一次真实事故
或一次险些发生的事故。修改第 6 节前请先确认你理解它防的是什么。*
