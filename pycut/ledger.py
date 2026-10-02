"""网盘台账 EasyCut-处理进度.md 的读写：下载 -> 解析 -> 追加 -> 上传覆盖。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .drive import DriveClient, DriveFile

CST = timezone(timedelta(hours=8))


def now_str() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M")


@dataclass
class SliceRecord:
    """功能一：整理切片的一条记录。"""
    source_path: str = ""     # 源视频网盘路径
    filename: str = ""
    md5: str = ""
    size: str = ""
    slice_count: int = 0
    output_dir: str = ""      # 切片输出目录（网盘相对路径）
    status: str = ""          # done / skipped_short / skipped_done / failed
    note: str = ""            # 失败原因等备注
    time: str = ""

    def to_row(self, idx: int) -> str:
        return (f"| {idx} | {self.source_path} | {self.filename} | {self.md5} | "
                f"{self.size} | {self.slice_count} | {self.output_dir} | "
                f"{self.status} | {self.note} | {self.time} |")


@dataclass
class MixRecord:
    """功能二：混剪成片的一条记录。"""
    source: str = ""          # 使用切片目录
    script: str = ""          # 话术行号/版本
    count: int = 0            # 成片数
    output_dir: str = ""
    status: str = ""
    time: str = ""
    seed: str = ""            # 二期：seed（可复现）
    batch: str = ""           # 二期：批次名（网盘 成片/<批次>/）

    def to_row(self, idx: int) -> str:
        return (f"| {idx} | {self.source} | {self.script} | {self.count} | "
                f"{self.output_dir} | {self.status} | {self.time} | "
                f"{self.seed} | {self.batch} |")


class Ledger:
    """台账文件在网盘项目根目录，文件名见 DriveLayout.ledger_name。"""

    SLICE_ANCHOR = "## 功能一：整理切片"
    MIX_ANCHOR = "## 功能二：混剪成片"

    def __init__(self, client: DriveClient, project_root_id: str,
                 ledger_name: str, workdir: Path):
        self.client = client
        self.project_root_id = project_root_id
        self.ledger_name = ledger_name
        self.workdir = workdir
        self.local_path = workdir / ledger_name
        self.remote: DriveFile | None = None
        self.slice_records: list[SliceRecord] = []
        self.mix_records: list[MixRecord] = []
        self._md5_index: set[str] = set()

    # ---------- 加载 / 保存 ----------

    def load(self) -> "Ledger":
        """从网盘下载台账并解析；不存在则用空表初始化。"""
        hit = self.client.find_child(self.project_root_id, self.ledger_name)
        if hit is None:
            self.remote = None
            return self
        self.remote = hit
        self.client.download(hit, self.local_path)
        self._parse(self.local_path.read_text(encoding="utf-8"))
        return self

    def save(self) -> None:
        """写回网盘（覆盖更新，保留文件 ID）。"""
        self._render()
        if self.remote is None:
            self.remote = self.client.upload(
                self.local_path, self.project_root_id, self.ledger_name)
        else:
            self.client.update_content(self.remote.id, self.local_path)

    # ---------- 查询 ----------

    def is_processed(self, md5: str) -> bool:
        # 只有成功/跳过才算处理过；failed 允许重试
        if not md5 or md5 not in self._md5_index:
            return False
        return any(r.md5 == md5 and r.status != "failed"
                   for r in self.slice_records)

    # ---------- 追加 ----------

    def add_slice(self, rec: SliceRecord) -> None:
        if not rec.time:
            rec.time = now_str()
        self.slice_records.append(rec)
        if rec.md5:
            self._md5_index.add(rec.md5)

    def add_mix(self, rec: MixRecord) -> None:
        if not rec.time:
            rec.time = now_str()
        self.mix_records.append(rec)

    # ---------- 解析 / 渲染 ----------

    @staticmethod
    def _parse_table(text: str, anchor: str) -> list[list[str]]:
        lines = text.splitlines()
        rows: list[list[str]] = []
        in_table = False
        for i, ln in enumerate(lines):
            if ln.strip() == anchor:
                in_table = True
                continue
            if not in_table:
                continue
            if ln.startswith("## "):      # 下一个章节，结束
                break
            s = ln.strip()
            if not s.startswith("|"):
                continue
            cells = [c.strip() for c in s.strip("|").split("|")]
            if not cells or cells[0] == "#" or set(cells[0]) <= set("-:"):
                continue
            if cells[0] == "" and all(c == "" for c in cells):
                continue
            rows.append(cells)
        return rows

    def _parse(self, text: str) -> None:
        for cells in self._parse_table(text, self.SLICE_ANCHOR):
            if len(cells) < 9:
                continue
            try:
                count = int(cells[5]) if cells[5].isdigit() else 0
            except ValueError:
                count = 0
            rec = SliceRecord(source_path=cells[1], filename=cells[2],
                              md5=cells[3], size=cells[4], slice_count=count,
                              output_dir=cells[6], status=cells[7],
                              note=cells[8] if len(cells) >= 10 else "",
                              time=cells[9] if len(cells) >= 10 else cells[8])
            self.slice_records.append(rec)
            if rec.md5:
                self._md5_index.add(rec.md5)
        for cells in self._parse_table(text, self.MIX_ANCHOR):
            if len(cells) < 7:
                continue
            try:
                count = int(cells[3]) if cells[3].isdigit() else 0
            except ValueError:
                count = 0
            self.mix_records.append(MixRecord(
                source=cells[1], script=cells[2], count=count,
                output_dir=cells[4], status=cells[5], time=cells[6],
                seed=cells[7] if len(cells) > 7 else "",
                batch=cells[8] if len(cells) > 8 else ""))

    def _render(self) -> None:
        L: list[str] = []
        L.append("# EasyCut 视频处理进度记录")
        L.append("")
        L.append("> 由 Arlo 在云电脑上维护。每次处理完一批后更新。")
        L.append("")
        L.append(self.SLICE_ANCHOR)
        L.append("")
        L.append("| # | 源视频网盘路径 | 文件名 | MD5 | 大小 | 切片数 | 切片输出目录 | 状态 | 备注 | 处理时间 |")
        L.append("|---|---|---|---|---|---|---|---|---|---|")
        for i, r in enumerate(self.slice_records, 1):
            L.append(r.to_row(i))
        L.append("")
        L.append(self.MIX_ANCHOR)
        L.append("")
        L.append("| # | 使用切片目录 | 话术行号/版本 | 成片数 | 输出目录 | 状态 | 处理时间 | seed | 批次 |")
        L.append("|---|---|---|---|---|---|---|---|---|")
        for i, r in enumerate(self.mix_records, 1):
            L.append(r.to_row(i))
        L.append("")
        L.append("## 待处理")
        L.append("")
        L.append("- （无）")
        L.append("")
        L.append("## 备注")
        L.append("")
        L.append("- 每批下载约 1G 处理，处理完上传后删除本地副本再下下一批。")
        L.append("- MD5 以网盘源文件为准，用于断点续传时跳过已处理文件。")
        L.append("")
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.local_path.write_text("\n".join(L), encoding="utf-8")
