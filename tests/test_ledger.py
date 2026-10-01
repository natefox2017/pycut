"""台账解析/渲染测试（纯本地，不碰网盘）。"""
from pycut.ledger import Ledger, SliceRecord

SAMPLE = """# EasyCut 视频处理进度记录

## 功能一：整理切片

| # | 源视频网盘路径 | 文件名 | MD5 | 大小 | 切片数 | 切片输出目录 | 状态 | 处理时间 |
|---|---|---|---|---|---|---|---|---|
| 1 | 爆款素材/抖音 | a.mp4 | abc123 | 100MB | 5 | 切片库/<分类> | done | 2026-10-01 10:00 |
| 2 | 爆款素材/抖音 | b.mp4 | def456 | 5MB | 0 |  | skipped_short | 2026-10-01 10:05 |

## 功能二：混剪成片

| # | 使用切片目录 | 话术行号/版本 | 成片数 | 输出目录 | 状态 | 处理时间 |
|---|---|---|---|---|---|---|

## 待处理

- （无）
"""


def test_parse():
    lg = Ledger.__new__(Ledger)
    lg.slice_records, lg.mix_records, lg._md5_index = [], [], set()
    lg._parse(SAMPLE)
    assert len(lg.slice_records) == 2
    assert lg.slice_records[0].md5 == "abc123"
    assert lg.slice_records[1].status == "skipped_short"
    assert lg.is_processed("abc123")
    assert not lg.is_processed("zzz")


def test_render_roundtrip(tmp_path):
    lg = Ledger.__new__(Ledger)
    lg.slice_records, lg.mix_records, lg._md5_index = [], [], set()
    lg.workdir = tmp_path
    lg.local_path = tmp_path / "t.md"
    lg._parse(SAMPLE)
    lg.add_slice(SliceRecord(source_path="p", filename="c.mp4", md5="fff",
                             size="9MB", slice_count=2,
                             output_dir="切片库/<分类>", status="done"))
    lg._render()
    text = (tmp_path / "t.md").read_text(encoding="utf-8")
    lg2 = Ledger.__new__(Ledger)
    lg2.slice_records, lg2.mix_records, lg2._md5_index = [], [], set()
    lg2._parse(text)
    assert len(lg2.slice_records) == 3
    assert lg2.is_processed("fff")
