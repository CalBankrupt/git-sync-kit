"""git-sync-kit 核心（Python）。

严格实现 SPEC.md 0.2.0：

  · 第 2 节  两种工作模式（update / sync）与各自的判定规则
  · 第 3 节  版本号与发布约定（没升版 = 没发布）
  · 第 4 节  七态状态机与两段式点击
  · 第 5 节  JSON 契约（19 个字段）
  · 第 6 节  冲突：只报告，不动文件
  · 第 7 节  八条安全约束（不可省略）
  · 第 8 节  通道选择（git 优先，HTTP 兜底）

设计取舍
--------
* 任何公开动作都不抛异常，一律返回结构化结果。一个坏文件或一次网络故障不该让
  调用方的界面挂掉（SPEC 7.8）。
* 只用 ff-only。历史分叉时宁可失败让人处理，也不产生用户没预期的提交（SPEC 7.6）。
* diff 比较工作区而不是 HEAD。选择性应用不移动 HEAD，按提交比较会导致应用完了
  差异还一直列着（SPEC 5.2，这一条是实测踩出来的）。
* 冲突只报告，不动文件（SPEC 第 6 节）。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

__version__ = "0.2.2"

BRANCH_DEFAULT = "main"

#: 工作模式（SPEC 第 2 节）
MODE_UPDATE = "update"
MODE_SYNC = "sync"
MODES = (MODE_UPDATE, MODE_SYNC)

#: 差异清单的条目上限（SPEC 5.2）
DIFF_LIMIT = 100

#: 应用前备份的目录名（SPEC 7.2）
BACKUP_DIR = "_backup_previous"

# Windows 上别让 git 弹控制台窗口（调用方可能是 pythonw 启动的 GUI）
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def find_repo(start: str | Path | None = None, markers: Iterable[str] = (".git",)) -> Path | None:
    """从 start 向上找仓库根（默认看 .git）。找不到返回 None。"""
    here = Path(start).resolve() if start else Path.cwd().resolve()
    wanted = tuple(markers)
    for candidate in (here, *here.parents):
        if all((candidate / m).exists() for m in wanted):
            return candidate
    return None


def _run_git(root: Path, *args: str, timeout: float = 90.0) -> tuple[int, str, str]:
    """跑一条 git 命令，返回 (退出码, stdout, stderr)。绝不抛异常。"""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            creationflags=_NO_WINDOW,
        )
    except FileNotFoundError:
        return 127, "", "找不到 git，请确认已安装并在 PATH 中"
    except subprocess.TimeoutExpired:
        return 124, "", f"git {' '.join(args)} 超时（{timeout:.0f} 秒）"
    except Exception as exc:  # noqa: BLE001
        return 1, "", f"{type(exc).__name__}: {exc}"
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def _parse_version(text: str) -> tuple[int, ...]:
    """把 1.2.3 变成 (1, 2, 3)，容错到不会抛异常。"""
    parts: list[int] = []
    for chunk in (text or "").strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def _is_newer(candidate: str, current: str) -> bool:
    return _parse_version(candidate) > _parse_version(current)


# ---------------------------------------------------------------------------
# 数据结构（SPEC 5.2 / 5.3）
# ---------------------------------------------------------------------------
@dataclass
class DiffEntry:
    """差异清单里的一条。"""

    status: str          # A / M / D / R / T
    path: str
    time: str | None = None

    def as_dict(self) -> dict:
        return {"status": self.status, "path": self.path, "time": self.time}


@dataclass
class ConflictEntry:
    """冲突清单里的一条（SPEC 5.3）。"""

    path: str
    reason: str = "both-modified"

    def as_dict(self) -> dict:
        return {"path": self.path, "reason": self.reason}


@dataclass
class Report:
    """一次 check 的结果 —— 对应 SPEC 5.1 的 JSON（19 个字段）。"""

    ok: bool = True
    state: str = "unknown"        # latest / stale / ahead / error / unknown
    work_mode: str = MODE_UPDATE  # update / sync
    local: str = "?"
    remote: str | None = None
    has_update: bool = False
    behind: int = 0
    ahead: int = 0
    dirty: bool = False
    is_git: bool = False
    channel: str = "none"         # git / http / none
    branch: str = BRANCH_DEFAULT
    message: str = ""
    error: str | None = None
    diff_count: int = 0
    diff_latest_time: str | None = None
    diff_truncated: bool = False
    diff_files: list[DiffEntry] = field(default_factory=list)
    conflict_files: list[ConflictEntry] = field(default_factory=list)

    @property
    def has_conflict(self) -> bool:
        return bool(self.conflict_files)

    @property
    def can_install(self) -> bool:
        """SPEC 5.1：git 通道 ∧ 有更新 ∧ 不脏 ∧ 无冲突。"""
        return bool(
            self.channel == "git"
            and self.has_update
            and not self.dirty
            and not self.has_conflict
        )

    @property
    def can_select(self) -> bool:
        """SPEC 5.1：只要有差异就能挑文件 —— 没有任何前提。"""
        return bool(self.channel == "git" and self.diff_count > 0)

    @property
    def can_push(self) -> bool:
        """SPEC 5.1：sync 工作模式 ∧ 无冲突 ∧ 有东西可送。"""
        return bool(
            self.work_mode == MODE_SYNC
            and self.channel == "git"
            and not self.has_conflict
            and (self.ahead > 0 or self.dirty)
        )

    def diff_dict(self) -> dict | None:
        if self.diff_count <= 0:
            return None
        return {
            "count": self.diff_count,
            "latestTime": self.diff_latest_time,
            "truncated": self.diff_truncated,
            "files": [f.as_dict() for f in self.diff_files],
        }

    def conflict_dict(self) -> dict | None:
        if not self.conflict_files:
            return None
        return {
            "count": len(self.conflict_files),
            "files": [c.as_dict() for c in self.conflict_files],
        }

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "state": self.state,
            "workMode": self.work_mode,
            "local": self.local,
            "remote": self.remote,
            "has_update": self.has_update,
            "behind": self.behind,
            "ahead": self.ahead,
            "dirty": self.dirty,
            "is_git": self.is_git,
            "channel": self.channel,
            "branch": self.branch,
            "message": self.message,
            "error": self.error,
            "can_install": self.can_install,
            "can_select": self.can_select,
            "can_push": self.can_push,
            "diff": self.diff_dict(),
            "conflict": self.conflict_dict(),
        }


# ---------------------------------------------------------------------------
# 主体
# ---------------------------------------------------------------------------
class GitSyncKit:
    """一个仓库的检查 / 更新 / 应用 / 推送器。

    Args:
        root:   仓库根目录。省略时从当前目录向上找 .git。
        branch: 跟踪的分支，默认 main。
        repo:   owner/name，仅用于 HTTP 兜底通道，可为空。
        mode:   update（单向分发）或 sync（双向）。
    """

    def __init__(
        self,
        root: str | Path | None = None,
        branch: str = BRANCH_DEFAULT,
        repo: str = "",
        mode: str = MODE_UPDATE,
    ) -> None:
        self.root = Path(root) if root else (find_repo() or Path.cwd())
        self.branch = branch
        self.repo = repo
        self.mode = mode if mode in MODES else MODE_UPDATE

    # ------------------------------------------------------------------
    # 基本信息
    # ------------------------------------------------------------------
    def is_git_repo(self) -> bool:
        return (self.root / ".git").exists()

    def local_version(self) -> str:
        """读仓库根的 VERSION；读不到退到 pyproject.toml。"""
        version_file = self.root / "VERSION"
        try:
            if version_file.is_file():
                text = version_file.read_text(encoding="utf-8").strip()
                if text:
                    return text.splitlines()[0].strip().lstrip("vV").strip()
        except OSError:
            pass
        try:
            pyproject = (self.root / "pyproject.toml").read_text(encoding="utf-8")
            for line in pyproject.splitlines():
                stripped = line.strip()
                if stripped.startswith("version") and "=" in stripped:
                    return stripped.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            pass
        return "0.0.0"

    def current_commit(self, rev: str = "HEAD") -> str:
        code, out, _ = _run_git(self.root, "rev-parse", "--short", rev)
        return out if code == 0 else ""

    def is_dirty(self) -> bool:
        code, out, _ = _run_git(self.root, "status", "--porcelain")
        return code == 0 and bool(out)

    def dirty_files(self) -> list[str]:
        code, out, _ = _run_git(self.root, "status", "--porcelain")
        return out.splitlines() if code == 0 else []

    # ------------------------------------------------------------------
    # 远端探测（SPEC 8）
    # ------------------------------------------------------------------
    def _remote_via_git(self) -> tuple[str | None, int, str]:
        """fetch 后读 origin/<branch>:VERSION。

        返回 (版本号, 落后提交数, 错误)。0.2.0 起不再判试验版 —— 发布意图由版本号
        表达（SPEC 第 3 节）。
        """
        code, out, err = _run_git(
            self.root, "fetch", "--quiet", "origin", self.branch, timeout=120
        )
        if code != 0:
            return None, 0, err or out or "git fetch 失败"

        code, out, err = _run_git(self.root, "show", f"origin/{self.branch}:VERSION")
        if code != 0 or not out:
            return None, 0, err or f"origin/{self.branch} 上没有 VERSION 文件"

        version = out.strip().splitlines()[0].strip().lstrip("vV").strip()

        behind = 0
        code2, out2, _ = _run_git(
            self.root, "rev-list", "--count", f"HEAD..origin/{self.branch}"
        )
        if code2 == 0 and out2.isdigit():
            behind = int(out2)
        return version, behind, ""

    def _ahead_count(self) -> int:
        """本地领先远端多少个提交（SPEC 5.1 的 ahead）。"""
        code, out, _ = _run_git(
            self.root, "rev-list", "--count", f"origin/{self.branch}..HEAD"
        )
        return int(out) if code == 0 and out.isdigit() else 0

    def _remote_via_http(self) -> tuple[str | None, str]:
        """HTTP 兜底：直接取 raw 上的 VERSION。只能看，不能装（SPEC 8）。"""
        if not self.repo:
            return None, "未配置 repo（owner/name），HTTP 通道不可用"
        url = f"https://raw.githubusercontent.com/{self.repo}/{self.branch}/VERSION"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "git-sync-kit"})
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
                text = resp.read().decode("utf-8", "replace").strip()
            if not text:
                return None, "远端 VERSION 是空的"
            return text.splitlines()[0].strip().lstrip("vV").strip(), ""
        except Exception as exc:  # noqa: BLE001
            return None, f"{type(exc).__name__}: {exc}"

    # ------------------------------------------------------------------
    # 差异清单（SPEC 5.2）
    # ------------------------------------------------------------------
    def diff(self) -> dict:
        """只读。工作区 vs origin/<branch>。

        基准是工作区而不是 HEAD：选择性应用只写工作区、不移动 HEAD，按提交比较
        会导致应用完了差异还一直列着（SPEC 5.2）。
        """
        files: list[DiffEntry] = []
        code, out, _ = _run_git(
            self.root, "diff", "--name-status", f"origin/{self.branch}"
        )
        if code == 0:
            for line in out.splitlines():
                m = re.match(r"^([AMDRT])\d*\t(.+)$", line)
                if m:
                    files.append(DiffEntry(status=m.group(1), path=m.group(2)))

        # 每个文件最近一次改动时间：git log 从新到旧，某文件第一次出现即最近
        time_map: dict[str, str] = {}
        code, out, _ = _run_git(
            self.root, "log", "--name-only", "--format=%cI",
            f"HEAD..origin/{self.branch}",
        )
        if code == 0:
            current: str | None = None
            for raw in out.splitlines():
                text = raw.strip()
                if not text:
                    continue
                if re.match(r"^\d{4}-\d{2}-\d{2}T", text):
                    current = text
                    continue
                if current and text not in time_map:
                    time_map[text] = current

        for f in files:
            f.time = time_map.get(f.path)
        times = sorted(t for t in (f.time for f in files) if t)
        return {
            "count": len(files),
            "latestTime": times[-1] if times else None,
            "files": [f.as_dict() for f in files[:DIFF_LIMIT]],
            "truncated": len(files) > DIFF_LIMIT,
        }

    # ------------------------------------------------------------------
    # 冲突（SPEC 第 6 节）
    # ------------------------------------------------------------------
    def _changed_since(self, base: str, rev: str) -> set[str]:
        code, out, _ = _run_git(self.root, "diff", "--name-only", base, rev)
        return set(out.splitlines()) if code == 0 else set()

    def _worktree_paths(self) -> set[str]:
        """工作区里未提交的路径（含未跟踪文件）。"""
        paths: set[str] = set()
        for line in self.dirty_files():
            text = line[3:].strip() if len(line) > 3 else ""
            if " -> " in text:          # 改名：取新名字
                text = text.split(" -> ", 1)[1]
            if text:
                paths.add(text.strip('"'))
        return paths

    def conflict(self) -> dict:
        """只读。本地改过的文件与远端改过的文件取交集（SPEC 第 6 节）。

        没有分叉点就不存在冲突 —— 那种情况只有「落后」或「领先」。
        """
        code, base, _ = _run_git(
            self.root, "merge-base", "HEAD", f"origin/{self.branch}"
        )
        if code != 0 or not base:
            return {"count": 0, "files": []}
        base = base.strip().splitlines()[0].strip()

        mine = self._changed_since(base, "HEAD") | self._worktree_paths()
        theirs = self._changed_since(base, f"origin/{self.branch}")
        common = sorted(mine & theirs)
        return {
            "count": len(common),
            "files": [{"path": p, "reason": "both-modified"} for p in common],
        }

    # ------------------------------------------------------------------
    # 检查（SPEC 5.1）
    # ------------------------------------------------------------------
    def check(self) -> Report:
        rep = Report(local=self.local_version(), branch=self.branch, work_mode=self.mode)
        rep.is_git = self.is_git_repo()

        if not rep.is_git:
            remote, err = self._remote_via_http()
            if not remote:
                rep.error = f"不是 git 仓库，且 HTTP 查询失败：{err}"
                rep.state = "error"
                rep.ok = False
                rep.message = rep.error
                return rep
            rep.remote = remote
            rep.channel = "http"
            rep.has_update = _is_newer(remote, rep.local)
            rep.state = "stale" if rep.has_update else "latest"
            rep.message = (
                f"发现新版本 {rep.remote}（当前 {rep.local}）。此模式下只能查看，无法更新。"
                if rep.has_update
                else f"已是最新版本（{rep.local}）。"
            )
            return rep

        rep.dirty = self.is_dirty()
        remote, behind, err = self._remote_via_git()
        if remote is not None:
            rep.remote = remote
            rep.behind = behind
            rep.channel = "git"
        else:
            http_remote, http_err = self._remote_via_http()
            if http_remote:
                rep.remote = http_remote
                rep.channel = "http"
                rep.error = (
                    f"git 通道不可用（{err}），已改用 HTTP 查询；"
                    "此模式下无法安装、应用或推送。"
                )
            else:
                rep.error = f"git 通道失败：{err}；HTTP 兜底也失败：{http_err}"
                rep.state = "error"
                rep.ok = False
                rep.message = rep.error
                return rep

        # 冲突只可能出现在真分叉上（本地和远端各有提交）
        if rep.channel == "git":
            rep.ahead = self._ahead_count()
            if rep.behind > 0 and rep.ahead > 0:
                c = self.conflict()
                rep.conflict_files = [ConflictEntry(**item) for item in c["files"]]

        # ---- 按工作模式判定（SPEC 第 2 节）----
        if self.mode == MODE_SYNC:
            if rep.channel == "git" and rep.behind > 0:
                rep.has_update = True
                rep.state = "stale"
                rep.message = (
                    f"远端有 {rep.behind} 个提交未拉取"
                    + ("；本地也有未推送的改动，先拉再推。" if rep.can_push else "。")
                )
            elif rep.channel == "git" and rep.ahead > 0:
                rep.state = "ahead"
                rep.message = (
                    f"本地有 {rep.ahead} 个提交未推送"
                    f"（工作区{'有' if rep.dirty else '无'}未提交改动）。"
                )
            elif rep.dirty:
                rep.state = "ahead"
                rep.message = "工作区有未提交的改动，可以上传。"
            else:
                rep.state = "latest"
                rep.message = f"已同步（{rep.local}）。"
        else:
            rep.has_update = bool(rep.remote and _is_newer(rep.remote, rep.local))
            if rep.has_update:
                rep.state = "stale"
                note = f"，落后 {rep.behind} 个提交" if rep.behind else ""
                rep.message = f"发现新版本 {rep.remote}（当前 {rep.local}{note}）。"
            else:
                rep.state = "latest"
                rep.message = f"已是最新版本（{rep.local}）。"

        # 差异清单：只要有得拉就算出来，与状态无关。behind 为 0 时省掉两条 git 调用。
        if rep.channel == "git" and rep.behind > 0:
            d = self.diff()
            rep.diff_count = d["count"]
            rep.diff_latest_time = d["latestTime"]
            rep.diff_truncated = d["truncated"]
            rep.diff_files = [DiffEntry(**f) for f in d["files"]]

            # SPEC 第 3 节的小补救：没升版也要让人知道，但状态位不打扰。
            if self.mode == MODE_UPDATE and rep.state == "latest":
                rep.message += (
                    f"远端另有 {rep.behind} 个提交未升版 —— 打开窗口可以看差异。"
                )

        if rep.has_conflict:
            rep.message += (
                f"{chr(10)}注意：{len(rep.conflict_files)} 个文件本地和远端都改过，"
                "拉取与上传都已停用，请先手动处理。"
            )
        return rep

    # ------------------------------------------------------------------
    # 更新（SPEC 5.4 / 7.4 / 7.5 / 7.6）
    # ------------------------------------------------------------------
    def install(self) -> dict[str, Any]:
        """git pull --ff-only。本地脏或有冲突时拒绝（SPEC 7.4 / 7.5）。"""
        if not self.is_git_repo():
            return {
                "ok": False,
                "message": "当前目录不是 git 仓库，无法自动更新。请手动下载新版。",
            }

        if self.is_dirty():
            files = self.dirty_files()
            shown = chr(10).join(f"  {line}" for line in files[:10])
            more = f"{chr(10)}  …还有 {len(files) - 10} 项" if len(files) > 10 else ""
            return {
                "ok": False,
                "dirty": True,
                "message": (
                    "本地有未提交的改动，已拒绝更新 —— 以免覆盖你正在改的东西。"
                    f"{chr(10)}{shown}{more}{chr(10)}"
                    "请先提交（或用 git stash 暂存）后再试。"
                ),
            }

        c = self.conflict()
        if c["count"]:
            names = "、".join(item["path"] for item in c["files"][:10])
            return {
                "ok": False,
                "conflict": True,
                "files": [item["path"] for item in c["files"]],
                "message": (
                    f"有 {c['count']} 个文件本地和远端都改过，已拒绝更新：{names}"
                    f"{chr(10)}请先手动处理（merge、或选一边）再试。"
                ),
            }

        before = self.current_commit()
        code, out, err = _run_git(
            self.root, "pull", "--ff-only", "origin", self.branch, timeout=300
        )
        if code != 0:
            return {
                "ok": False,
                "before": before,
                "message": f"更新失败：{chr(10)}{err or out}",
            }

        after = self.current_commit()
        version = self.local_version()
        if before == after:
            return {
                "ok": True,
                "changed": False,
                "before": before,
                "after": after,
                "version": version,
                "message": f"已经是最新的（{version}），没有需要拉取的内容。",
            }
        return {
            "ok": True,
            "changed": True,
            "before": before,
            "after": after,
            "version": version,
            "message": (
                f"更新完成：{before} → {after}（版本 {version}）。"
                f"{chr(10)}重启应用后新代码才会生效。"
            ),
        }

    # ------------------------------------------------------------------
    # 选择性应用（SPEC 5.5 / 7.1 / 7.2 / 7.3）
    # ------------------------------------------------------------------
    def apply(self, paths: list[str] | None = None) -> dict[str, Any]:
        """把远端指定的文件落到工作区。只接受差异清单里的路径。"""
        want = list(dict.fromkeys(
            str(p).strip() for p in (paths or []) if str(p).strip()
        ))
        if not want:
            return {"ok": False, "message": "没有选中任何文件"}

        allowed = {f["path"]: f["status"] for f in self.diff()["files"]}
        unknown = [p for p in want if p not in allowed]
        if unknown:
            # SPEC 7.1：整体拒绝，不部分执行
            return {
                "ok": False,
                "message": "这些文件不在差异清单里，已拒绝：" + "、".join(unknown),
            }

        # SPEC 7.2：先备份，失败则中止
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        backup_dir = self.root / BACKUP_DIR / f"selection-{stamp}"
        backed_up: list[str] = []
        try:
            for rel in want:
                src = self.root / rel
                if not src.is_file():
                    continue
                dst = backup_dir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                backed_up.append(rel)
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "message": f"备份失败，已中止（未改动任何文件）：{exc}",
            }

        # SPEC 7.3：删除走 git rm
        to_checkout = [p for p in want if allowed.get(p) != "D"]
        to_remove = [p for p in want if allowed.get(p) == "D"]
        errors: list[str] = []

        if to_checkout:
            code, out, err = _run_git(
                self.root, "checkout", f"origin/{self.branch}", "--",
                *to_checkout, timeout=180,
            )
            if code != 0:
                errors.append(err or out or "git checkout 失败")
        if to_remove:
            code, out, err = _run_git(
                self.root, "rm", "-f", "--", *to_remove, timeout=180
            )
            if code != 0:
                errors.append(err or out or "git rm 失败")

        return {
            "ok": not errors,
            "applied": to_checkout,
            "removed": to_remove,
            "backedUp": backed_up,
            "backupDir": str(backup_dir) if backed_up else "",
            "errors": errors,
            "message": (
                f"已应用 {len(to_checkout)} 个文件"
                + (f"，删除 {len(to_remove)} 个" if to_remove else "")
                + (f"，备份 {len(backed_up)} 个到 {backup_dir}" if backed_up else "")
                + ("。" if not errors else f"，但有 {len(errors)} 个错误。")
            ),
        }

    # ------------------------------------------------------------------
    # 上传（仅 sync 工作模式，SPEC 5.6 / 7.5 / 7.7）
    # ------------------------------------------------------------------
    def push(self, message: str = "") -> dict[str, Any]:
        """提交并推送本地改动。

        SPEC 7.7：调用方必须先列出待提交条目、由人确认；这里再查一次模式与冲突，
        免得有人绕过界面直接调。
        """
        if self.mode != MODE_SYNC:
            return {
                "ok": False,
                "message": "当前是单向更新模式，上传入口没开。",
            }
        if not self.is_git_repo():
            return {"ok": False, "message": "当前目录不是 git 仓库，无法上传。"}

        c = self.conflict()
        if c["count"]:
            names = "、".join(item["path"] for item in c["files"][:10])
            return {
                "ok": False,
                "conflict": True,
                "files": [item["path"] for item in c["files"]],
                "message": (
                    f"有 {c['count']} 个文件本地和远端都改过，已拒绝推送：{names}"
                    f"{chr(10)}请先手动处理（merge、或选一边）再试。"
                ),
            }

        code, branch_out, _ = _run_git(self.root, "rev-parse", "--abbrev-ref", "HEAD")
        branch = branch_out or self.branch

        dirty = self.dirty_files()
        committed = False
        if dirty:
            code, out, err = _run_git(self.root, "add", "-A")
            if code != 0:
                return {"ok": False, "message": f"git add 失败：{err or out}"}

            note = message or f"同步：{datetime.now().strftime('%Y-%m-%d %H:%M')}"
            code, out, err = _run_git(self.root, "commit", "-m", note, "--quiet")
            if code != 0:
                return {"ok": False, "message": f"git commit 失败：{err or out}"}
            committed = True

        code, out, err = _run_git(self.root, "push", "origin", branch, timeout=300)
        if code != 0:
            return {
                "ok": False,
                "committed": committed,
                "message": f"推送失败：{chr(10)}{err or out}",
            }

        # files 数的是 git status --porcelain 的**条目数**，不是文件数：一个未跟踪
        # 目录只占一行，却可能含十几个文件。字段名保留（改名是破坏性变更），
        # 文案一律说「项」，并补一个同义的 entries 让含义自解释。
        return {
            "ok": True,
            "committed": committed,
            "files": len(dirty),
            "entries": len(dirty),
            "branch": branch,
            "message": (
                f"已提交 {len(dirty)} 项改动并推送到 origin/{branch}。"
                if committed
                else f"没有本地改动，已确认 origin/{branch} 是最新的。"
            ),
        }
