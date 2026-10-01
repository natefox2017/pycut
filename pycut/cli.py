"""PyCut 命令行入口。

云电脑说明（必读）：
- 运行环境 = Hatch 云电脑（Linux）。直接 `python -m pycut ...` 即可，
  无需虚拟环境（主流程零第三方依赖）。
- WORKDIR（~/workspace/pycut/run）是本地中转区：downloads（下载中转）、
  outputs（切片产出）、review_pack（AI 证据包）、decisions（AI 决策）、
  transcribe（字幕帧）、hook_pool（开头池）。处理完一批后本地副本删除，
  只留证据包和决策（可追溯）。
- 语音转录是唯一的例外：走 pycut/.venv 里的 faster-whisper（见 speech.py）。
- 网盘认证由运行时托管，开箱即用（见 drive.py）。

用法示例：
    python -m pycut scan --path 颗粒/爆款素材/抖音
    python -m pycut slice --path 颗粒/爆款素材/抖音 --decisions run/decisions
    python -m pycut import-shorts --path 颗粒/片段/片段手机录制
    python -m pycut organize --path 颗粒 --dry-run
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .analyze import EvidenceBuilder
from .clean import CleanReport, organize as organize_folder
from .config import DriveLayout, SliceRules
from .drive import DriveClient, DriveFile
from .hooks import extract_hook
from .ledger import Ledger
from .media import probe
from .pipeline import SlicePipeline
from .product import brief_text, load_product
from .scriptgen import (build_prepare_brief, estimate_duration,
                        extract_opening_audio, target_chars,
                        write_scripts_to_taskform)
from .taskform import generate_taskform, read_taskform, summarize as taskform_summary
from .voice import extract_subtitle_frames

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


def cmd_analyze(args) -> int:
    """AI 分析：下载 -> 建证据包（分段+代表帧）-> 输出待 AI 决策。"""
    client, layout, rules = _client(), DriveLayout(), SliceRules()
    pack_root = WORKDIR / "review_pack"
    pack_root.mkdir(parents=True, exist_ok=True)
    builder = EvidenceBuilder(rules)
    ledger = Ledger(client, layout.project_root_id,
                    layout.ledger_name, WORKDIR).load()
    n = 0
    for d in _resolve_paths(client, layout, args.path):
        for v in sorted([f for f in client.list_folder(d.id) if f.is_video],
                        key=lambda f: f.size):
            if ledger.is_processed(v.md5 or ""):
                continue
            local = WORKDIR / "downloads" / v.name
            client.download(v, local)
            md5 = v.md5 or DriveClient.md5_of(local)
            pack = builder.build(local, md5, pack_root)
            local.unlink(missing_ok=True)
            print(f"📦 {v.name} -> {pack}")
            n += 1
            if n >= args.limit:
                break
        if n >= args.limit:
            break
    print(f"\n共生成 {n} 个证据包，AI 看帧后填写各包内 decisions.json，"
          f"改名为 <md5>.json 放入 --decisions 目录再跑 slice")
    return 0


def _run_mode(args, mode: str) -> int:
    client, layout, rules = _client(), DriveLayout(), SliceRules()
    pipe = SlicePipeline(client, layout, rules, WORKDIR,
                         dry_run=args.dry_run,
                         decisions_dir=Path(args.decisions)
                         if getattr(args, "decisions", None) else None)
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


def cmd_organize(args) -> int:
    """整理视频：清理重复文件与系统垃圾（进回收站，可撤销）。"""
    client, layout = _client(), DriveLayout()
    for p in args.path:
        parts = [x for x in p.strip("/").split("/") if x]
        folder = client.resolve_path(layout.project_root_id, *parts)
        report = organize_folder(client, folder.id, root_name=p,
                                 dry_run=args.dry_run)
        print(f"\n📁 {p}")
        print(report.summary())
        if report.junk:
            print("  垃圾文件:")
            for f in report.junk[:20]:
                print(f"    - {f.name}")
        if report.duplicates:
            print("  重复冗余（已保留一份）:")
            for f in report.duplicates[:20]:
                print(f"    - {f.name} ({f.size/1e6:.0f}MB)")
        if report.docs:
            print("  用户文档（未删除，请确认）:")
            for f in report.docs:
                print(f"    - {f.name}")
        if args.dry_run:
            print("  [dry-run] 未实际删除")
    return 0


def cmd_slice(args) -> int:
    return _run_mode(args, "slice")


def cmd_import_shorts(args) -> int:
    return _run_mode(args, "shorts")


def cmd_product(args) -> int:
    """显示产品信息表（话术的唯一事实来源）。"""
    from pathlib import Path
    info = load_product(Path(__file__).resolve().parent.parent / "product.yaml")
    print(brief_text(info))
    return 0


def cmd_transcribe(args) -> int:
    """话术转录准备：按固定间隔抽字幕帧，供 Arlo 读字幕整理话术。"""
    from pathlib import Path
    client, layout = _client(), DriveLayout()
    out_root = WORKDIR / "transcribe"
    out_root.mkdir(parents=True, exist_ok=True)
    for p in args.path:
        parts = [x for x in p.strip("/").split("/") if x]
        folder = client.resolve_path(layout.project_root_id, *parts)
        vids = sorted([f for f in client.list_folder(folder.id) if f.is_video],
                      key=lambda f: f.size)[:args.limit]
        for v in vids:
            local = WORKDIR / "downloads" / v.name
            client.download(v, local)
            out_dir = out_root / Path(v.name).stem
            frames = extract_subtitle_frames(local, out_dir, every=args.every)
            local.unlink(missing_ok=True)
            print(f"📝 {v.name}: {len(frames)} 张字幕帧 -> {out_dir}")
    print("\nArlo 读帧整理话术后，写入话术文件（UTF-8，每行一条），"
          "再经 product.check_script() 事实核对")
    return 0


def cmd_hooks(args) -> int:
    """爆款开头池：取爆款视频前 N 秒，标准化后存入 hook_pool/。"""
    from pathlib import Path
    client, layout, rules = _client(), DriveLayout(), SliceRules()
    pool = WORKDIR / "hook_pool"
    pool.mkdir(parents=True, exist_ok=True)
    n = 0
    for p in args.path:
        parts = [x for x in p.strip("/").split("/") if x]
        folder = client.resolve_path(layout.project_root_id, *parts)
        vids = sorted([f for f in client.list_folder(folder.id) if f.is_video],
                      key=lambda f: f.size)[:args.limit]
        for v in vids:
            local = WORKDIR / "downloads" / v.name
            client.download(v, local)
            md5 = v.md5 or DriveClient.md5_of(local)
            dst = pool / f"hook_{md5[:8]}.mp4"
            extract_hook(local, dst, seconds=args.seconds, rules=rules)
            local.unlink(missing_ok=True)
            info = probe(dst)
            print(f"🎬 {v.name} -> {dst.name} ({info.duration:.1f}s)")
            n += 1
    print(f"\n开头池共 {n} 条，混剪（二期）时轮换使用 + 轻微去重变换")
    return 0


def cmd_scriptgen(args) -> int:
    """话术生成（独立执行过程，产出进任务单"话术"表）。
    prepare: 方式一，输出 Arlo 写话术用的输入包（产品+参考话术+目标）。
    write: 把话术文件校验后写入任务单"话术"表并回传网盘。
    extract-opening: 方式二，提取爆款视频前 N 秒原音频存入 颗粒/音频库/。
    """
    from pathlib import Path
    client, layout = _client(), DriveLayout()
    parts = [x for x in args.path.strip("/").split("/") if x]
    folder = client.resolve_path(layout.project_root_id, *parts)
    product = load_product(Path(__file__).resolve().parent.parent / "product.yaml")

    if args.action == "prepare":
        ref: list[str] = []
        ref_file = client.find_child(folder.id, "参考话术.txt")
        if ref_file:
            tmp = WORKDIR / "参考话术.txt"
            client.download(ref_file, tmp)
            ref = [x.strip() for x in tmp.read_text(encoding="utf-8").splitlines()
                   if x.strip()]
            tmp.unlink(missing_ok=True)
        print(build_prepare_brief(product, ref, args.seconds, args.count))
        print(f"\nArlo 写完话术后跑: pycut scriptgen write --path {args.path} "
              f"--name {args.name} --scripts-file 话术.txt")
        return 0

    if args.action == "write":
        name = args.name
        f = client.find_child(folder.id, name)
        if not f:
            print(f"找不到任务单: {name}")
            return 1
        local = WORKDIR / name
        client.download(f, local)
        scripts = [x.strip() for x in
                   Path(args.scripts_file).read_text(encoding="utf-8").splitlines()
                   if x.strip()]
        blocked = write_scripts_to_taskform(local, scripts, product)
        if blocked:
            print("❌ 以下话术未通过事实核对，未写入：")
            for b in blocked:
                print("  -", b)
            local.unlink(missing_ok=True)
            return 2
        client.update_content(f.id, local)
        local.unlink(missing_ok=True)
        print(f"✅ {len(scripts)} 条话术已写入任务单" )
        for s in scripts:
            print(f"  约{estimate_duration(s)}s | {s[:40]}")
        return 0

    # extract-opening
    vparts = [x for x in args.video.strip("/").split("/") if x]
    vfolder = client.resolve_path(layout.project_root_id, *vparts[:-1])
    vf = client.find_child(vfolder.id, vparts[-1])
    if not vf:
        print(f"找不到视频: {args.video}")
        return 1
    local_v = WORKDIR / "downloads" / vf.name
    client.download(vf, local_v)
    audio_dir = client.ensure_folder(
        client.resolve_path(layout.project_root_id, "颗粒").id, "音频库")
    dst = WORKDIR / f"opening_{vf.md5[:8] if vf.md5 else 'x'}_{args.seconds:.0f}s.m4a"
    extract_opening_audio(local_v, args.seconds, dst)
    up = client.upload(dst, audio_dir.id)
    local_v.unlink(missing_ok=True)
    dst.unlink(missing_ok=True)
    print(f"🎙 开头原音频已存: 颗粒/音频库/{up.name} (id={up.id})")
    return 0


def cmd_taskform(args) -> int:
    """任务单：new=按规划生成并上传到网盘；read=下载并校验（只认表内事项）。"""
    from datetime import date
    from pathlib import Path
    client, layout = _client(), DriveLayout()
    parts = [x for x in args.path.strip("/").split("/") if x]
    folder = client.resolve_path(layout.project_root_id, *parts)
    name = args.name or f"任务单-{date.today():%Y-%m-%d}.xlsx"
    local = WORKDIR / name
    if args.action == "new":
        product = load_product(Path(__file__).resolve().parent.parent / "product.yaml")
        generate_taskform(local, product, batch_name=name.replace(".xlsx", ""))
        up = client.upload(local, folder.id)
        local.unlink(missing_ok=True)
        print(f"📋 任务单已上传: {args.path}/{name} (id={up.id})")
        print("用户在手机上填写完成后，把'状态'改为'已填写'并在聊天里告诉我")
    else:
        f = client.find_child(folder.id, name)
        if not f:
            print(f"找不到任务单: {name}")
            return 1
        client.download(f, local)
        form = read_taskform(local)
        local.unlink(missing_ok=True)
        print(taskform_summary(form))
        if not form.ready:
            print("\n⏸ 任务单未填写完成，不执行任何处理")
            return 2
    return 0


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
    p.add_argument("--decisions", default=None,
                   help="AI 决策目录（<md5>.json），有则用 AI 切点+分类代替机械规划")
    p.set_defaults(fn=cmd_slice)

    p = sub.add_parser("analyze", help="AI 分析：生成证据包供 AI 看帧决策")
    p.add_argument("--path", action="append", required=True,
                   help="网盘相对路径，如 颗粒/爆款素材/抖音（可多次）")
    p.add_argument("--limit", type=int, default=1, help="最多处理几个视频")
    p.set_defaults(fn=cmd_analyze)

    p = sub.add_parser("organize", help="整理视频：清理重复文件与系统垃圾")
    p.add_argument("--path", action="append", required=True,
                   help="网盘相对路径，如 颗粒（可多次）")
    p.add_argument("--dry-run", action="store_true", help="只报告，不删除")
    p.set_defaults(fn=cmd_organize)

    p = sub.add_parser("import-shorts", help="功能一：短视频直接入库（不切割）")
    p.add_argument("--path", action="append", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_import_shorts)

    p = sub.add_parser("product", help="显示产品信息表（话术的唯一事实来源）")
    p.set_defaults(fn=cmd_product)

    p = sub.add_parser("transcribe", help="话术转录准备：抽字幕帧供 Arlo 读字幕整理话术")
    p.add_argument("--path", action="append", required=True,
                   help="网盘相对路径，如 颗粒/爆款素材/抖音（可多次）")
    p.add_argument("--limit", type=int, default=2, help="最多处理几个视频")
    p.add_argument("--every", type=float, default=5.0, help="抽帧间隔（秒）")
    p.set_defaults(fn=cmd_transcribe)

    p = sub.add_parser("hooks", help="爆款开头池：取爆款视频前 N 秒建开头池")
    p.add_argument("--path", action="append", required=True,
                   help="网盘相对路径，如 颗粒/爆款素材/抖音（可多次）")
    p.add_argument("--limit", type=int, default=3, help="最多处理几个视频")
    p.add_argument("--seconds", type=float, default=4.0, help="开头取几秒")
    p.set_defaults(fn=cmd_hooks)

    p = sub.add_parser("taskform", help="任务单：生成/读取本批处理任务单")
    p.add_argument("action", choices=["new", "read"], help="new=生成并上传，read=下载并校验")
    p.add_argument("--path", required=True, help="网盘目录，如 颗粒")
    p.add_argument("--name", default=None, help="任务单文件名（默认按日期）")
    p.set_defaults(fn=cmd_taskform)

    p = sub.add_parser("scriptgen", help="话术生成：独立执行，产出进任务单话术表")
    p.add_argument("action", choices=["prepare", "write", "extract-opening"])
    p.add_argument("--path", required=True, help="网盘目录，如 颗粒")
    p.add_argument("--name", default=None, help="任务单文件名（write 用）")
    p.add_argument("--seconds", type=float, default=30.0, help="每条目标秒数（prepare 用）")
    p.add_argument("--count", type=int, default=5, help="生成条数（prepare 用）")
    p.add_argument("--scripts-file", default=None, help="话术文本文件（write 用，一行一条）")
    p.add_argument("--video", default=None, help="爆款视频网盘路径（extract-opening 用）")
    p.set_defaults(fn=cmd_scriptgen)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
