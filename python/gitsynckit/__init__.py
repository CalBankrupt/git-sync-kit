"""git-sync-kit 的 Python 实现。

与 :mod:`gitsynckit.core` 的关系见 ``SPEC.md``：本模块只做导出，逻辑都在 core。

快速使用::

    from gitsynckit import GitSyncKit

    kit = GitSyncKit("/path/to/repo")          # 默认 update 模式
    report = kit.check()
    if report.can_install:
        print(kit.install()["message"])

双向同步（自己的多台机器互传）::

    kit = GitSyncKit("/path/to/repo", mode="sync")
    kit.check()
    kit.push()          # 上传本地改动
    kit.install()       # 拉取远端改动（本地脏时会被拒绝）
"""
from .core import (
    BRANCH_DEFAULT,
    DiffEntry,
    GitSyncKit,
    Report,
    __version__,
    find_repo,
)

__all__ = [
    "GitSyncKit",
    "Report",
    "DiffEntry",
    "find_repo",
    "BRANCH_DEFAULT",
    "__version__",
]
