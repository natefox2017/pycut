"""整理视频：清理网盘文件夹中的重复文件与系统垃圾文件。

规则（2026-10-01 用户确认）：
- 重复文件：MD5 完全相同的文件只保留首次发现的一份，其余移入回收站。
  没有 MD5 的文件不做自动判定（只报告）。
- 系统垃圾：.DS_Store、Thumbs.db、desktop.ini、.pip_cache.json、
  macOS 资源分叉（._*）、*.tmp 等，直接移入回收站。
- 用户文档（.txt/.md 等）与图片素材：只列出、不自动删除，由用户决定。
- 删除一律走回收站（可撤销），不做永久删除。

与切片功能放在同一包内，可在 slice 之前先跑 organize。
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path

from .drive import DriveClient, DriveFile

#: 系统垃圾文件名模式（自动清理）
JUNK_PATTERNS = [
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
    ".pip_cache.json",
    "._*",
    "*.tmp",
    "*.temp",
    "~$*",
]

#: 用户文档扩展名（只报告，不自动删除）
DOC_EXTS = {".txt", ".md", ".doc", ".docx", ".pdf", ".xls", ".xlsx"}


@dataclass
class CleanReport:
    root: str = ""
    scanned: int = 0
    junk: list[DriveFile] = field(default_factory=list)
    duplicates: list[DriveFile] = field(default_factory=list)  # 被删的冗余份
    kept: list[DriveFile] = field(default_factory=list)        # 重复组中保留的
    docs: list[DriveFile] = field(default_factory=list)        # 用户文档（未删）
    trashed: list[DriveFile] = field(default_factory=list)

    def summary(self) -> str:
        L = [f"扫描文件: {self.scanned}",
             f"系统垃圾: {len(self.junk)}",
             f"重复冗余: {len(self.duplicates)}（保留 {len(self.kept)} 份）",
             f"用户文档（未动）: {len(self.docs)}",
             f"已移入回收站: {len(self.trashed)}"]
        return "\n".join(L)


def _is_junk(name: str) -> bool:
    low = name.lower()
    return any(fnmatch.fnmatch(low, p.lower()) for p in JUNK_PATTERNS)


def _is_doc(name: str) -> bool:
    return Path(name).suffix.lower() in DOC_EXTS


def scan_all(client: DriveClient, folder_id: str) -> list[DriveFile]:
    """递归扫描目录下所有文件（含子目录中的）。"""
    out: list[DriveFile] = []
    stack = [folder_id]
    while stack:
        fid = stack.pop()
        for f in client.list_folder(fid):
            if f.is_folder:
                stack.append(f.id)
            else:
                out.append(f)
    return out


def find_junk(files: list[DriveFile]) -> list[DriveFile]:
    return [f for f in files if _is_junk(f.name)]


def find_duplicates(files: list[DriveFile]) -> tuple[list[DriveFile], list[DriveFile]]:
    """按 MD5 分组。返回 (保留, 冗余)。无 MD5 的文件不参与自动判定。"""
    groups: dict[str, list[DriveFile]] = {}
    for f in files:
        if not f.md5:
            continue
        groups.setdefault(f.md5, []).append(f)
    kept, dupes = [], []
    for md5, grp in groups.items():
        if len(grp) > 1:
            kept.append(grp[0])
            dupes.extend(grp[1:])
    return kept, dupes


def organize(client: DriveClient, folder_id: str, root_name: str = "",
             dry_run: bool = False) -> CleanReport:
    """执行整理。dry_run=True 只报告不删除。"""
    report = CleanReport(root=root_name)
    files = scan_all(client, folder_id)
    report.scanned = len(files)

    junk = find_junk(files)
    kept, dupes = find_duplicates(files)
    report.junk = junk
    report.duplicates = dupes
    report.kept = kept
    report.docs = [f for f in files
                   if _is_doc(f.name) and f not in junk and f not in dupes]

    if not dry_run:
        for f in junk + dupes:
            client.trash(f.id)
            report.trashed.append(f)
    return report
