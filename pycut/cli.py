"""PyCut 命令行入口。

用法示例：
    python -m pycut scan --path 爆款素材/抖音
    python -m pycut slice --path 爆款素材/抖音 --path 爆款素材/快手
    python -m pycut import-shorts --path 片段/片段手机录制 --path 片段/死老鼠
    python -m pycut slice --path 爆款素材/抖音 --dry-run
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import DriveLayout, SliceRules
from .drive import DriveClient, DriveFile
from .pipeline import SlicePipeline

WORKDIR = Path.home() / "workspace" / "pycut" / "run"


def _client() -> DriveClient:
    return DriveClient()


def _resolve_paths(client: DriveClient, layout: DriveLayout,
                   paths: list[str]) -> list[DriveFile]:
    out = []
    for p in paths:
        parts = [x for x in p.strip("/").split("/") if x]
        out.append(client.resolve_path(layout.project_root_id, *parts))
    return out


def cmd_scan(args) -> int:
    client, layout = _client(), DriveLayout()
    pipe = SlicePipeline(client, layout, SliceRules(), WORKDIR, dry_run=True)
    for d in _resolve_paths(client, layout, args.path):
        vids = pipe.scan(d.id)
        total = sum(v.size for v in vids)
        print(f"📁 {d.name}: {len(vids)}个视频, 共{total/1e9:.2f}GB")
        for v in vids[:20]:
            print(f"   {v.name}  {v.size/1e6:.0f}MB  md5:{v.md5[:8] if v.md5 else '-'}")
        if len(vids) > 20:
            print(f"   ... 还有 {len(vids)-20} 个")
    return 0


def _run_mode(args, mode: str) -> int:
    client, layout, rules = _client(), DriveLayout(), SliceRules()
    pipe = SlicePipeline(client, layout, rules, WORKDIR,
                         dry_run=args.dry_run)
    sources: list[DriveFile] = []
    for d in _resolve_paths(client, layout, args.path):
        vids = pipe.scan(d.id)
        print(f"📁 {d.name}: {len(vids)}个视频待处理")
        sources.extend(vids)
    results = pipe.run_sources(sources, mode=mode)
    done = sum(1 for r in results if r.status == "done")
    skip = sum(1 for r in results if r.status.startswith("skipped"))
    fail = sum(1 for r in results if r.status == "failed")
    print(f"\n完成: {done}  跳过: {skip}  失败: {fail}")
    for r in results:
        if r.status == "failed":
            print(f"  ✗ {r.source.name}: {r.note}")
    return 0 if fail == 0 else 1


def cmd_slice(args) -> int:
    return _run_mode(args, "slice")


def cmd_import_shorts(args) -> int:
    return _run_mode(args, "shorts")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="pycut", description="PyCut 云端视频处理管线")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scan", help="扫描目录（只读，列出视频）")
    p.add_argument("--path", action="append", required=True,
                   help="网盘相对路径，如 爆款素材/抖音（可多次）")
    p.set_defaults(fn=cmd_scan)

    p = sub.add_parser("slice", help="功能一：长视频切片整理")
    p.add_argument("--path", action="append", required=True)
    p.add_argument("--dry-run", action="store_true", help="只下载处理，不上传/不写台账")
    p.set_defaults(fn=cmd_slice)

    p = sub.add_parser("import-shorts", help="功能一：短视频直接入库（不切割）")
    p.add_argument("--path", action="append", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_import_shorts)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
