# git-sync-kit

**一套「检查远端有没有新东西 → 决定要不要拿下来 → 拿下来」的标准实现**，三个语言各一份，行为完全一致。

写这个是因为同一套逻辑在几个项目里被反复重写，每次都踩一遍同样的坑——覆盖了用户正在改的文件、把 `git checkout` 用在已删除的路径上、差异比较基准搞错导致清单永不消失……

现在这些结论被固定成一份[规范](SPEC.md)，三份实现都遵守它。

---

## 三份实现，一个契约

| 语言 | 位置 | 用途 |
|---|---|---|
| **Python** | [`python/gitsynckit/`](python/gitsynckit/) | pywebview / CLI 工具 |
| **PowerShell** | [`powershell/GitSyncKit/`](powershell/GitSyncKit/) | Windows 脚本、WinForms 工具 |
| **Node** | [`node/index.mjs`](node/index.mjs) | Node 服务、MCP server |
| **UI 组件** | [`ui/status-seat.js`](ui/status-seat.js) | 任何有 DOM 的地方 |

三份实现的 `check()` 都返回**同样 16 个字段**，可以直接互相替换：

```
ok  state  mode  local  remote  has_update  behind  dirty  is_git
channel  branch  message  error  can_install  can_select  diff
```

---

## 快速开始

### Python

```python
import sys
sys.path.insert(0, r"...\git-sync-kit\python")
from gitsynckit import GitSyncKit

kit = GitSyncKit(r"D:\my-project", repo="owner/name")
report = kit.check()

print(report.state)          # latest / stale / trial / error / unknown
print(report.message)

if report.can_install:
    print(kit.install()["message"])          # 一键更新
elif report.can_select:
    kit.apply([f["path"] for f in report.diff_dict()["files"]])   # 勾选应用
```

### PowerShell

```powershell
Import-Module "...\git-sync-kit\powershell\GitSyncKit\GitSyncKit.psd1"

$report = Get-GitSyncReport -Root "D:\my-project" -Repo "owner/name"
$report.state

if ($report.can_install) { Invoke-GitSyncInstall -Root "D:\my-project" }
elseif ($report.can_select) {
    $diff = Get-GitSyncDiff -Root "D:\my-project"
    Invoke-GitSyncApply -Root "D:\my-project" -Paths $diff.files.path
}
```

### Node

```js
import { GitSyncKit } from 'file:///.../git-sync-kit/node/index.mjs'

const kit = new GitSyncKit({ root: 'D:/my-project', repo: 'owner/name' })
const report = await kit.check()
if (report.can_install) await kit.install()
```

### UI 状态位

```js
import { createStatusSeat } from 'file:///.../git-sync-kit/ui/status-seat.js'

const seat = createStatusSeat({
  anchor: '#existing-toolbar-button',      // 克隆它，样式自动继承
  onActivate: async (state) => {
    if (state === 'stale' || state === 'trial') { openUpdateWindow(); return null }
    return await myApi.check()             // { state, message, diff }
  },
})
```

七种状态的图形与语义见 [SPEC 第 4 节](SPEC.md#4-状态机)。

---

## 七种状态

| 状态 | 图形 | 含义 |
|---|---|---|
| `idle` | 环形箭头 | 空闲，点一下检查 |
| `busy` | 环形箭头 + 旋转 | 正在执行 |
| `latest` | **✓** | 已是最新 |
| `stale` | **↑** | 有正式版新版本，可一键更新 |
| `trial` | **?** | **试验版**有差异，需人工挑文件 |
| `error` | 箭头 + 斜划线 | 检查失败 |
| `unknown` | 环形箭头 | 已检查但判断不出 |

**两段式点击**：平时点一下只做**原地检查**（图标转一圈，不动文件）；查出 `stale` 或 `trial` 后再点，才执行真正的动作。**一次点击不会直接改动文件。**

---

## 试验版：为什么需要它

远端 `VERSION` 以 `*` 结尾（如 `0.1.2*`）→ **试验版**：

- 状态是 **?** 而不是 ↑
- **不给**一键更新，改为列出**差异清单**让人勾选
- 应用前自动备份

> 正式流程里更新就该升版本号；**不升版本号却推了提交，说明这是试验版**，本地很可能有实验性改动，不该让脚本自行决定覆盖。

这是从实际使用中总结出来的：把「可以直接覆盖」和「必须人工挑选」分成两种发布意图，而不是让脚本去猜。

---

## 安全约束

每一条都对应一次真实事故或险些发生的事故，详见 [SPEC 第 6 节](SPEC.md#6-安全约束强制不可省略)：

1. **路径白名单** —— 只接受差异清单里出现过的路径，整体拒绝而非部分执行
2. **应用前备份** —— 受影响的现有文件先复制到 `_backup_previous/selection-<时间>/`，备份失败则中止
3. **删除走 `git rm`** —— 不用 `checkout`（它对不存在的路径会报错）
4. **本地脏时拒绝更新** —— 宁可失败也不丢用户正在改的东西
5. **只用 `--ff-only`** —— 分叉时失败让人处理，不产生意外的合并提交
6. **错误不抛给界面** —— 一切返回结构化结果

---

## 跨语言一致性测试

`scripts/consistency.py` 把「三份实现输出同一份报告」从口头约定变成可执行的检查：

```powershell
python scripts/consistency.py
python scripts/consistency.py --root "D:/my-project"     # 额外测一个真实仓库
```

它在临时目录里现造几个 git 仓库，各覆盖一种状态，用**本地裸仓库**当 origin ——
全程离线，不碰任何真实远端，也不改你现有的仓库：

| 夹具 | 覆盖的分支 |
|---|---|
| `latest` | 本地与远端同提交 |
| `stale` | 远端版本号更高，本地落后 |
| `trial` | 远端 VERSION 带 `*`，工作区与远端有差异 |
| `dirty` | 有未提交的改动（拒绝更新的那条路） |
| `no-remote` | 没有 origin —— git 通道必然失败 |
| `no-version` | 远端没有 VERSION 文件（与 fetch 失败是两个错误出口） |

每个夹具还会断言自己**造对了**（state / dirty / can_install 等），否则夹具造错
会让「一致性测试」变成自我安慰。只比对只读的 `check()` —— `install` /
`apply` 会动文件，不适合放进自动检查。

> **它第一次跑就抓到三处真实漂移**，都是手工比对看不见的：
>
> 1. 「落后 N 个提交」这一句，Python 写在括号外，另外两份写在括号内；
> 2. PowerShell 只读 `Err` 不看 `Out`，于是错误信息只剩一个冒号
>    —— 它的 `2>&1` 把 stdout 和 stderr 合在了一起，git 的原因在 `Out` 里；
> 3. Node 的 `show` 分支漏了 `out` 兜底，报的是笼统的「没有 VERSION 文件」
>    而不是 git 的原话。

### 对 PowerShell 的要求

**只支持 PowerShell 7+（`pwsh`），不支持 Windows PowerShell 5.1。**

5.1 的默认执行策略常常是 Restricted，`Import-Module` 会被直接拒绝，而
**退出码仍然是 0** —— 表现为「命令跑完了，什么都没输出」，从现象很难倒推原因。
控制台编码、`ConvertTo-Json` 对空值的处理也和 7 不一样，为它单独兜底不划算。

脚本因此**只找 `pwsh`**，不退回 `powershell.exe`；`--ps` 指到 7 以下的
版本会明确报错退出。

---

## 怎么在项目里引用

三种方式，按环境选：

**① 环境变量指定**（多项目共用一份时推荐）

```powershell
$env:GITSYNC_KIT = "D:\CAD Tool\git-sync-kit"
```

**② 同级目录**（几个仓库平级放置时最省事）

```
<某个目录>\
├── git-sync-kit\          ← 本仓库
├── my-project-a\
└── my-project-b\
```

**③ 项目内 vendored**（要自包含时）

```
my-project\vendor\git-sync-kit\
```

> 无论用哪种，**在引用的地方写明来源和版本**，例如：
> ```powershell
> # 引用共享模块 git-sync-kit（规约见其 SPEC.md）
> # 位置解析：$env:GITSYNC_KIT → 同级目录 → vendor/
> ```

---

## 用在哪些项目

| 项目 | 语言实现 | 模式 |
|---|---|---|
| [OCP-Viewer-Desktop](https://github.com/CalBankrupt/OCP-Viewer-Desktop) | Python + UI | `update` |
| [novel-workflow](https://github.com/CalBankrupt/novel-workflow) | Node + PowerShell | `update` |
| [cad-workspace](https://github.com/CalBankrupt/cad-workspace) | PowerShell | `sync`（双向） |

> 本仓库的逻辑是从这三个项目的实际实现中抽取的；抽取时以 OCP-Viewer-Desktop
> 那一份为基准（它的功能最完整：状态位、差异清单、勾选应用、备份）。

---

## 两种模式

| 模式 | 方向 | 动作 |
|---|---|---|
| `update` | 远端 → 本地 | `install`（pull --ff-only） |
| `sync` | 双向 | `install`（拉取）+ `push`（提交并推送） |

`sync` 模式用于自己的多台机器互传工作成果——那种场景下**本地可能有还没上传的
东西**，所以对"本地脏就拒绝拉取"这条执行得更严格。

---

## 目录结构

```
git-sync-kit/
├── SPEC.md                     ★ 规范（语言无关，改实现前先读它）
├── README.md
├── VERSION
├── LICENSE                     MIT
├── ui/
│   └── status-seat.js          状态位组件（零依赖）
├── python/gitsynckit/
│   ├── __init__.py
│   └── core.py
├── powershell/GitSyncKit/
│   ├── GitSyncKit.psm1         （UTF-8 BOM，免得被按 ANSI 代码页读）
│   └── GitSyncKit.psd1
├── node/
│   └── index.mjs
└── scripts/
    └── consistency.py          跨语言一致性测试（离线夹具 + 真实仓库）
```

---

## 许可证

MIT
