#!/usr/bin/env python3
r"""跨语言一致性测试：同一仓库状态下，三份实现必须给出同一份报告。

SPEC.md 第 5 节规定 `check()` 返回 16 个字段，三份实现（Python / PowerShell /
Node）应当逐字段相同。这份脚本把那条约定变成可执行的检查 —— 否则某门语言悄悄
掉队时谁也不会发现（这个模块最初就是靠手工比对才发现三份都一致的，手工比对
没法重复，也没法在下次改动时自动重跑）。

两种测法
--------
1. **夹具**（默认）—— 在临时目录里现造几个 git 仓库，各自覆盖一种状态：

   ==========  ==========================================================
   `latest`      本地与远端同提交
   `stale`       远端版本号更高，本地落后
   `trial`       远端 VERSION 带 `*`，且工作区与远端有差异
   ==========  ==========================================================

   每个夹具都用**本地裸仓库**当 origin，全程离线，不碰任何真实远端，也不改
   你现有的任何仓库。

2. **真实仓库**（`--root`）—— 额外拿你指定的仓库跑一遍，看它在**当前状态**
   下的三份报告是否一致。

对 PowerShell 的要求
--------------------
只支持 **PowerShell 7+（pwsh）**，**不支持 Windows PowerShell 5.1**。

5.1 的默认执行策略常常是 Restricted，`Import-Module` 会被直接拒绝，而**退出码
仍然是 0** —— 表现出来就是「命令跑完了，什么都没输出」，从现象很难倒推原因。
控制台编码、`ConvertTo-Json` 对空值的处理也和 7 不一样，为它单独兜底不划算。

所以这里**只找 pwsh**，不退回 powershell.exe；`--ps` 指到 7 以下的版本会直接报错。

只测 `check()`
---------------
`install` / `apply` 会真的动文件，不适合放进自动检查里跑。这里只比对
只读的 `check()` —— 那也是三份实现之间最容易漂移的部分。

用法::

    python scripts/consistency.py
    python scripts/consistency.py --root "D:/CAD Tool/cad-workspace"

退出码 0 = 全部一致；1 = 有差异（差异逐字段列出来）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Windows 上 stdout 默认走 GBK 代码页，中文报告会乱码甚至抛 UnicodeEncodeError
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

KIT = Path(__file__).resolve().parent.parent
NL = chr(10)

#: SPEC 5.1 规定的 16 个字段，顺序照 SPEC 里写的抄
FIELDS = (
    "ok", "state", "mode", "local", "remote", "has_update", "behind", "dirty",
    "is_git", "channel", "branch", "message", "error", "can_install",
    "can_select", "diff",
)

#: 提交夹具时用的身份，免得依赖机器上的 git config
GIT_ID = ("-c", "user.name=consistency", "-c", "user.email=consistency@local")


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise RuntimeError(f"git {' '.join(args)} 失败：{detail}")
    return proc


def run(cmd: list[str], timeout: float = 120.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout,
    )


#: 本脚本要求的 PowerShell 主版本。5.1 不在支持范围内，见文件头说明。
PS_MIN_MAJOR = 7


def ps_argv(exe: str) -> list[str]:
    """PowerShell 的公共命令行参数。

    -ExecutionPolicy Bypass 仍然带上：就算用的是 pwsh 7，组策略也可能把它设成
    AllSigned 之类，而那种失败同样不会改退出码。
    """
    argv = [exe, "-NoProfile", "-NonInteractive"]
    if os.name == "nt":
        argv += ["-ExecutionPolicy", "Bypass"]
    return argv


def ps_major(exe: str) -> int | None:
    """问一下 PowerShell 的主版本号；问不出来返回 None。"""
    try:
        proc = run(ps_argv(exe) + ["-Command", "$PSVersionTable.PSVersion.Major"])
    except Exception:  # noqa: BLE001
        return None
    for line in reversed(proc.stdout.strip().splitlines()):
        text = line.strip()
        if text.isdigit():
            return int(text)
    return None


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def commit_all(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, *GIT_ID, "commit", "-m", message, "--quiet")


def show(value) -> str:
    """把字段值渲染成一行，便于并排比较。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value).replace(NL, " / ")


def freeze(value) -> str:
    """比较用的稳定指纹（字典顺序无关）。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


# ---------------------------------------------------------------------------
# 夹具：造几个只覆盖单一状态的本地仓库
# ---------------------------------------------------------------------------
def bootstrap(base: Path, name: str, *, version: str | None = "1.0.0") -> tuple[Path, Path]:
    """建一个裸 origin 和一份推好第一个版本的 local。返回 (origin, local)。

    version=None 表示**不写 VERSION 文件** —— 用来造「远端读不到版本号」的仓库。
    """
    origin = base / f"{name}-origin.git"
    local = base / f"{name}-local"

    origin.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--bare", "--quiet", str(origin)], check=True,
                   capture_output=True)
    # 显式定成 main：三份实现默认都看 origin/main，别让 git 的默认分支名搅局
    git(origin, "symbolic-ref", "HEAD", "refs/heads/main")

    local.mkdir(parents=True, exist_ok=True)
    git(local, "init", "--quiet")
    git(local, "symbolic-ref", "HEAD", "refs/heads/main")
    git(local, "remote", "add", "origin", str(origin))

    if version is not None:
        write(local / "VERSION", version + NL)
    write(local / "app.py", "print('v1')" + NL)
    commit_all(local, "初始提交")
    git(local, "push", "--quiet", "-u", "origin", "main")
    return origin, local


def push_second_version(local: Path, version: str, app: str) -> None:
    """在 local 上再造一个提交推上去，然后把 local 退回来 —— 于是远端领先、本地干净。"""
    write(local / "VERSION", version + NL)
    write(local / "app.py", app + NL)
    commit_all(local, f"推高远端到 {version}")
    git(local, "push", "--quiet", "origin", "main")
    git(local, "reset", "--hard", "--quiet", "HEAD~1")


def build_no_remote(base: Path) -> Path:
    """一个没有 remote 的 git 仓库。

    git 通道必然失败，HTTP 兜底又没配 repo —— 用来覆盖 error 分支，
    顺便看看三份实现对「查不到」的说法是否一致。
    """
    repo = base / "no-remote"
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "--quiet")
    git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
    write(repo / "VERSION", "1.0.0" + NL)
    commit_all(repo, "初始版本 1.0.0")
    return repo


def build_fixtures(base: Path) -> list[tuple[str, Path, dict]]:
    """返回 [(名字, 仓库路径, 期望的字段值), ...]。

    期望值不只是 state —— 少了这些断言，夹具造错了也看不出来，
    而夹具造错恰恰是最容易让「一致性测试」变成自我安慰的方式。
    """
    made: list[tuple[str, Path, dict]] = []

    _, latest = bootstrap(base, "latest")
    made.append(("latest", latest, {"state": "latest", "dirty": False,
                                    "can_install": False}))

    _, stale = bootstrap(base, "stale")
    push_second_version(stale, "2.0.0", "print('v1')")
    made.append(("stale", stale, {"state": "stale", "has_update": True,
                                  "behind": 1, "can_install": True,
                                  "can_select": False}))

    _, trial = bootstrap(base, "trial")
    push_second_version(trial, "2.0.0*", "print('v2')")
    made.append(("trial", trial, {"state": "trial", "mode": "trial",
                                  "can_install": False, "can_select": True}))

    _, dirty = bootstrap(base, "dirty")
    write(dirty / "app.py", "print('local edit')" + NL)   # 只改，不提交
    made.append(("dirty", dirty, {"state": "latest", "dirty": True,
                                  "can_install": False}))

    made.append(("no-remote", build_no_remote(base),
                 {"state": "error", "channel": "none", "can_install": False}))

    # 仓库正常、但远端根本没有 VERSION 文件 —— 覆盖「读不到远端版本号」这条分支。
    # 它和 fetch 失败走的是两个不同的错误出口，文案也各有各的兜底。
    _, no_version = bootstrap(base, "no-version", version=None)
    made.append(("no-version", no_version,
                 {"state": "error", "remote": None, "channel": "none",
                  "can_install": False}))

    return made


# ---------------------------------------------------------------------------
# 三份实现，各跑一次 check()
# ---------------------------------------------------------------------------
def check_python(root: Path) -> dict:
    from gitsynckit import GitSyncKit  # noqa: PLC0415

    return GitSyncKit(str(root)).check().as_dict()


def check_powershell(root: Path, exe: str, kit: Path) -> dict:
    psd1 = kit / "powershell" / "GitSyncKit" / "GitSyncKit.psd1"
    q = chr(39)
    quote = lambda text: q + str(text).replace(q, q + q) + q  # noqa: E731
    script = ";".join([
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8",
        f"Import-Module {quote(psd1)} -Force",
        f"$report = Get-GitSyncReport -Root {quote(root)}",
        "$report | ConvertTo-Json -Depth 10 -Compress",
    ])
    proc = run(ps_argv(exe) + ["-Command", script])
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip() or "PowerShell 退出码非 0")
    text = proc.stdout.strip()
    if not text:
        # 模块没加载成功时 PowerShell 常常一声不吭（退出码还是 0），
        # 干巴巴的 JSONDecodeError 说明不了问题，把 stderr 一起抛出来。
        detail = proc.stderr.strip()
        raise RuntimeError("PowerShell 没有输出任何 JSON" + (f"；stderr: {detail}" if detail else ""))
    return json.loads(text)


def check_node(root: Path, exe: str, kit: Path) -> dict:
    url = kit.as_posix()
    target = root.as_posix()
    q = chr(39)
    quote = lambda text: q + str(text).replace(q, q + q) + q  # noqa: E731
    js = (
        "import('file:///" + url + "/node/index.mjs').then(async (m) => {"
        "  const r = await new m.GitSyncKit({root: " + quote(target) + "}).check();"
        "  console.log(JSON.stringify(r));"
        "})"
    )
    proc = run([exe, "--input-type=module", "-e", js])
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip() or "node 退出码非 0")
    return json.loads(proc.stdout.strip())


# ---------------------------------------------------------------------------
# 比对
# ---------------------------------------------------------------------------
def compare(reports: dict[str, dict]) -> list[str]:
    """返回差异清单；空列表表示逐字段一致。"""
    problems: list[str] = []

    for lang, report in reports.items():
        missing = [f for f in FIELDS if f not in report]
        extra = [k for k in report if k not in FIELDS]
        if missing:
            problems.append(f"  [{lang}] 缺字段：{', '.join(missing)}")
        if extra:
            problems.append(f"  [{lang}] 多出字段：{', '.join(extra)}")

    for field in FIELDS:
        values = {lang: report.get(field, "<缺失>") for lang, report in reports.items()}
        if len({freeze(v) for v in values.values()}) > 1:
            problems.append(f"  [字段 {field}]")
            width = max(len(lang) for lang in values)
            for lang, value in values.items():
                problems.append(f"      {lang.ljust(width)} = {show(value)}")

    return problems


def run_suite(title: str, root: Path, runners: dict) -> bool:
    """在 root 上跑三份实现并比对。返回是否一致。"""
    reports: dict[str, dict] = {}
    failures: dict[str, str] = {}

    for lang, runner in runners.items():
        try:
            reports[lang] = runner(root)
        except Exception as exc:  # noqa: BLE001
            failures[lang] = f"{type(exc).__name__}: {exc}"

    if failures:
        print(f"  {title}  —— 有实现没跑起来")
        for lang, note in failures.items():
            print(f"      {lang}: {note}")
        if reports:
            print(f"      （其余 {len(reports)} 份跑通了：{', '.join(reports)}）")
        return False

    states = {lang: rep.get("state") for lang, rep in reports.items()}
    problems = compare(reports)
    if problems:
        print(f"  {title}  —— 不一致（{len(problems)} 处）")
        for line in problems:
            print(line)
        return False

    state = next(iter(states.values()), "?")
    print(f"  {title}  —— 一致：16/16 字段，state={state}")
    return True


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description="同一仓库状态下比对 Python / PowerShell / Node 三份实现",
    )
    parser.add_argument("--root", action="append", default=[], metavar="PATH",
                        help="额外测一个真实仓库（可重复给）")
    parser.add_argument("--workdir", default="", metavar="PATH",
                        help="夹具放哪，默认 <kit>/../.cache/consistency")
    parser.add_argument("--keep", action="store_true", help="跑完保留夹具目录")
    parser.add_argument("--ps", default="",
                        help="指定 pwsh 的路径（必须是 PowerShell 7+，5.1 不支持）")
    parser.add_argument("--node", default="", help="指定 node 可执行文件")
    args = parser.parse_args()

    # 只找 pwsh —— 不退回 powershell.exe。5.1 的执行策略与编码差异会以
    # 「退出码 0 但什么也没输出」的形式出现，那种失败排查起来比不支持更贵。
    ps_exe = args.ps or shutil.which("pwsh")
    node_exe = args.node or shutil.which("node")
    if not ps_exe:
        print("找不到 PowerShell 7（pwsh）。本脚本不支持 Windows PowerShell 5.1。")
        print("装一个：https://aka.ms/powershell   或把 pwsh 的完整路径给 --ps")
        return 2
    if not node_exe:
        print("找不到 node，用 --node 指定")
        return 2

    major = ps_major(ps_exe)
    if major is None:
        print(f"问不出 {ps_exe} 的版本号，放弃。")
        return 2
    if major < PS_MIN_MAJOR:
        print(f"{ps_exe} 是 PowerShell {major}.x，本脚本要求 {PS_MIN_MAJOR}+（不支持 5.1）。")
        return 2
    if not shutil.which("git"):
        print("找不到 git")
        return 2

    sys.path.insert(0, str(KIT / "python"))

    runners = {
        "python": lambda root: check_python(root),
        "powershell": lambda root: check_powershell(root, ps_exe, KIT),
        "node": lambda root: check_node(root, node_exe, KIT),
    }

    workdir = Path(args.workdir) if args.workdir else KIT.parent / ".cache" / "consistency"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = workdir / stamp
    base.mkdir(parents=True, exist_ok=True)

    print("跨语言一致性测试")
    print(f"  共享模块 : {KIT}")
    print(f"  PowerShell: {ps_exe}")
    print(f"  node      : {node_exe}")
    print(f"  夹具目录  : {base}")
    print()

    ok = True
    try:
        print("夹具（离线自造，各覆盖一种状态）：")
        for name, root, expect in build_fixtures(base):
            wanted = "、".join(f"{k}={v}" for k, v in expect.items())
            passed = run_suite(f"{name:<11}（期望 {wanted}）", root, runners)
            if passed:
                actual = check_python(root)
                wrong = {k: actual.get(k) for k, v in expect.items()
                         if actual.get(k) != v}
                if wrong:
                    print(f"      ⚠ 夹具本身没造对：{wrong}")
                    passed = False
            ok = passed and ok

        if args.root:
            print()
            print("真实仓库（当前状态）：")
            for raw in args.root:
                root = Path(raw)
                if not root.is_dir():
                    print(f"  {raw}  —— 目录不存在")
                    ok = False
                    continue
                ok = run_suite(root.name, root, runners) and ok
    finally:
        if args.keep:
            print()
            print(f"夹具保留在：{base}")
        else:
            shutil.rmtree(base, ignore_errors=True)

    print()
    if ok:
        print("结论: 通过 —— 三份实现逐字段一致")
        return 0
    print("结论: 失败 —— 见上面的差异（这通常意味着某份实现掉队了）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
