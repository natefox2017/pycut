"""PyCut 命令行入口。

云电脑说明（必读）：
- 运行环境 = Hatch 云电脑（Linux，由 Muse 提供给用户的云端 Linux 虚拟机，
  Arlo 即运行在此环境中）。直接 `python -m pycut ...` 即可，
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
import subprocess
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
    """话术转录准备：按固定间隔抽字幕帧，供 Arlo 读字幕整理话术。
    Arlo = 用户的 AI 助手（Muse）。"""
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
    # Arlo = 用户的 AI 助手（Muse）：读帧整理话术后写入话术文件
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
    Arlo = 用户的 AI 助手（Muse）。
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
        # Arlo = 用户的 AI 助手（Muse）
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


def cmd_mix(args) -> int:
    """功能二：混剪成片。用切片库批量混剪，核心目标是去重。

    流程：读任务单（混剪参数+话术）-> 加载切片库 -> 开头池 ->
    seed 规划 N 条 -> 下载切片 -> 生成字幕 -> 渲染 -> 上传成片/ -> 台账。
    """
    from datetime import datetime
    from .mix import ClipInfo, MixPlanner, load_library, render
    from .subtitles import assets_fonts_dir, ensure_fonts, write_ass

    client, layout = _client(), DriveLayout()
    # 1. 读任务单
    parts = [x for x in args.taskform.strip("/").split("/") if x]
    folder = client.resolve_path(layout.project_root_id, *parts[:-1]) \
        if len(parts) > 1 else client.resolve_path(layout.project_root_id)
    tf_file = client.find_child(folder.id, parts[-1])
    if not tf_file:
        print(f"找不到任务单: {args.taskform}")
        return 1
    local_tf = WORKDIR / parts[-1]
    client.download(tf_file, local_tf)
    form = read_taskform(local_tf)
    local_tf.unlink(missing_ok=True)
    if not form.ready:
        print("⏸ 任务单状态不是'已填写'，不执行混剪")
        return 2

    count = args.count or form.mix_count
    intensity = args.intensity or form.mix_intensity
    # 分辨率：命令行 > 任务单 > 默认 1080x1920
    size_str = args.size or form.mix_size or "1080x1920"
    try:
        sw, sh = size_str.lower().split("x")
        out_w, out_h = int(sw), int(sh)
    except ValueError:
        out_w, out_h = 1080, 1920
    # 话术：优先命令行，其次任务单话术表序号
    scripts: list[str] = []
    if args.script:
        sp = Path(args.script)
        scripts = [sp.read_text(encoding="utf-8").strip()] if sp.exists() \
            else [args.script]
    elif args.scripts_file:
        scripts = [l.strip() for l in
                   Path(args.scripts_file).read_text(encoding="utf-8")
                   .splitlines() if l.strip()]
    else:
        rows = form.mix_script_rows or list(range(1, len(form.scripts) + 1))
        scripts = [form.scripts[i - 1] for i in rows
                   if 0 < i <= len(form.scripts)]
    if not scripts:
        print("没有话术：用 --script / --scripts-file，或在任务单话术表填写")
        return 1
    print(f"📝 话术 {len(scripts)} 条，混剪 {count} 条，强度 {intensity}")

    # 2. 加载切片库 + 开头池
    library, name_to_id = load_library(client, layout)
    total_clips = sum(len(v) for v in library.values())
    print(f"📚 切片库 {total_clips} 条")
    if total_clips == 0:
        print("切片库为空，先跑功能一")
        return 1
    hook_pool_dir = WORKDIR / "hook_pool"
    hook_files = sorted(hook_pool_dir.glob("hook_*.mp4")) if hook_pool_dir.exists() else []
    # 开头池也可用切片库"开头钩子"分类兜底
    hook_pool: list[ClipInfo] = []
    for hf in hook_files:
        info = probe(hf)
        hook_pool.append(ClipInfo(file_id="", name=hf.name,
                                  category="开头钩子",
                                  duration=info.duration,
                                  local_path=hf))
    if not hook_pool:
        for c in library.get("开头钩子", [])[:20]:
            hook_pool.append(c)
    if not hook_pool:
        print("开头池为空：先跑 `pycut hooks` 或等切片库有开头钩子分类")
        return 1
    print(f"🎬 开头池 {len(hook_pool)} 条")

    # 3. 话术音频（可选）：有则按音频时长对齐，否则按文本估算
    #    方式二（§8）：--opening-audio 指定开头爆款原声时，
    #    最终音频 = 开头原声 + 话术配音拼接，视频总时长向拼接后音频看齐
    audio_path: Path | None = None
    audio_dur: float | None = None
    opening_audio_path: Path | None = None
    opening_dur: float = 0.0
    if args.opening_audio:
        op = Path(args.opening_audio)
        if op.exists():
            opening_audio_path = op
            opening_dur = probe(op).duration
            print(f"🎙 开头原音频 {opening_dur:.1f}s（方式二）")
    if args.audio:
        audio_path = Path(args.audio)
        if audio_path.exists():
            main_dur = probe(audio_path).duration
            print(f"🔊 话术音频 {main_dur:.1f}s")
            audio_dur = opening_dur + main_dur if opening_dur > 0 else main_dur
            if opening_dur > 0:
                print(f"🔊 拼接后总时长 {audio_dur:.1f}s")
    if audio_dur is None:
        from .scriptgen import estimate_duration
        audio_dur = estimate_duration(scripts[0])
        print(f"🔊 无音频，按文本估算 {audio_dur:.1f}s")

    # 4. 输出目录
    batch = datetime.now().strftime("%Y%m%d-%H%M")
    granule = client.resolve_path(layout.project_root_id,
                                   layout.source_root_name)
    out_root = client.ensure_folder(granule.id, layout.outputs_dir_name)
    batch_folder = client.ensure_folder(out_root.id, f"批次-{batch}")
    workdir = WORKDIR / "mix" / batch
    dl_dir = workdir / "downloads"
    dl_dir.mkdir(parents=True, exist_ok=True)

    # 3b. 方式二音频拼接：开头原声 + 话术配音（在 workdir 定义后执行）
    #     先 loudnorm 统一两段音量（-16 LUFS），再拼接，避免配音听不清
    if opening_audio_path and audio_path and opening_dur > 0:
        concat_path = workdir / "concat_audio.m4a"
        if not concat_path.exists():
            import subprocess as _sp
            op_norm = workdir / "opening_norm.m4a"
            na_norm = workdir / "narration_norm.m4a"
            for _src, _dst in [(opening_audio_path, op_norm),
                               (audio_path, na_norm)]:
                _sp.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                         "-i", str(_src), "-af",
                         "loudnorm=I=-16:TP=-1.5:LRA=11",
                         "-c:a", "aac", str(_dst)], check=True)
            _sp.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                     "-i", str(op_norm), "-i", str(na_norm),
                     "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1",
                     "-c:a", "aac", str(concat_path)], check=True)
            print(f"🎙 音频已拼接（音量已统一）: {concat_path.name}")
        audio_path = concat_path  # 后续渲染用拼接后的音频

    # 5. 逐条规划渲染
    fonts = ensure_fonts()
    fonts_dir = assets_fonts_dir()
    usage_path = WORKDIR / "mix" / "usage.json"
    seed_base = args.seed or 20261002
    ok = 0
    for i in range(count):
        seed = seed_base + i
        script = scripts[i % len(scripts)]
        planner = MixPlanner(seed=seed, intensity=intensity)
        planner.load_usage(usage_path)
        try:
            # 方式二：hook 片段时间 = 开头原声音频时长
            hook_secs = opening_dur if opening_dur > 0 else 4.0
            plan = planner.plan(library, hook_pool, audio_dur, script=script,
                                hook_seconds=hook_secs)
            plan.width, plan.height = out_w, out_h
        except ValueError as e:
            print(f"  第 {i+1} 条规划失败: {e}")
            continue
        # 下载本条用到的切片
        from .mix import detect_blur_bg, has_text_overlay
        for seg in plan.segments:
            clip = seg.clip
            if clip.local_path and clip.local_path.exists():
                detect_blur_bg(seg)
                # §3.2 禁忌：有烧录文字的不镜像
                if seg.mirror and has_text_overlay(seg):
                    seg.mirror = False
                continue
            fid = clip.file_id or name_to_id.get(clip.name)
            if not fid:
                # 开头池本地文件
                continue
            dst = dl_dir / clip.name
            if not dst.exists():
                client.download(DriveFile(id=fid, name=clip.name,
                                          mime_type="video/mp4",
                                          size=0, md5=""), dst)
            clip.local_path = dst
            detect_blur_bg(seg)
            if seg.mirror and has_text_overlay(seg):
                seg.mirror = False
        # 字幕
        ass_path = workdir / f"mix_{seed}.ass"
        write_ass(script, plan.target_duration, plan.subtitle_style,
                  fonts, ass_path, width=plan.width, height=plan.height)
        # 渲染
        out_path = workdir / f"成片_{batch}_{i+1:02d}.mp4"
        # 话术音频：有则用，无则生成静音轨（按 Ta）
        seg_audio = audio_path
        if seg_audio is None:
            seg_audio = workdir / f"silence_{seed}.m4a"
            if not seg_audio.exists():
                subprocess.run(
                    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                     "-f", "lavfi", "-i",
                     f"anullsrc=r=44100:cl=stereo:d={plan.target_duration:.2f}",
                     "-c:a", "aac", str(seg_audio)], check=True)
        try:
            render(plan, seg_audio, out_path,
                   ass_path=ass_path, fonts_dir=fonts_dir,
                   pad_bytes=args.pad_bytes)
        except RuntimeError as e:
            print(f"  第 {i+1} 条渲染失败: {e}")
            continue
        # 上传
        client.upload(out_path, batch_folder.id,
                      name=f"成片_{batch}_{i+1:02d}.mp4")
        # 封面：从成片随机抽一帧（2026-10-02 用户确认），上传到同批次目录
        try:
            from .mix import extract_cover_frame
            cover_path = workdir / f"封面_{batch}_{i+1:02d}.jpg"
            extract_cover_frame(out_path, cover_path, seed=seed)
            client.upload(cover_path, batch_folder.id,
                          name=f"封面_{batch}_{i+1:02d}.jpg")
            cover_path.unlink(missing_ok=True)
        except Exception as e:
            print(f"  ⚠️ 封面提取失败: {e}")
        planner.save_usage(usage_path)
        # 台账记录
        _record_mix_ledger(client, layout, batch, i + 1, plan, script)
        # 清理本条本地文件（保留 usage）
        for seg in plan.segments:
            p = seg.clip.local_path
            if p and hook_pool_dir not in p.parents and p.exists():
                p.unlink(missing_ok=True)
        out_path.unlink(missing_ok=True)
        ass_path.unlink(missing_ok=True)
        ok += 1
        print(f"  ✅ 第 {i+1}/{count} 条成片已上传 (seed={seed})")
    print(f"\n🎉 混剪完成: {ok}/{count} 条 -> 网盘 {layout.outputs_dir_name}/批次-{batch}/")
    return 0 if ok else 1


def _record_mix_ledger(client, layout, batch: str, idx: int,
                       plan, script: str) -> None:
    """混剪台账：seed/用料/变换参数/话术版本，可复现。"""
    from datetime import datetime
    granule = client.resolve_path(layout.project_root_id,
                                   layout.source_root_name)
    out_root = client.find_child(granule.id, layout.outputs_dir_name)
    batch_folder = client.find_child(out_root.id, f"批次-{batch}")
    ledger_name = "混剪台账.md"
    existing = client.find_child(batch_folder.id, ledger_name)
    lines: list[str] = []
    if existing:
        tmp = Path(f"/tmp/mix_ledger_{batch}.md")
        client.download(existing, tmp)
        lines = tmp.read_text(encoding="utf-8").splitlines()
        tmp.unlink(missing_ok=True)
    else:
        lines = ["# 混剪台账", "",
                 "| # | seed | 用料 | 变换 | 时长 | 话术摘要 | 时间 |",
                 "|---|---|---|---|---|---|---|"]
    used = "; ".join(
        f"{s.clip.name}[{s.clip.category}]"
        f"{'@' + str(round(s.speed, 2)) + 'x' if abs(s.speed - 1.0) > 1e-6 else ''}"
        f"{'镜' if s.mirror else ''}n{s.noise_n}"
        f"{'z103' if s.zoom_103 else ''}"
        f"{'抽帧' if s.drop_frames else ''}"
        f"{'模糊bg' if s.blur_bg else ''}"
        f"{'字幕模糊' if s.blur_sub_band else ''}"
        f"{'暗角' if s.vignette else ''}"
        for s in plan.segments)
    trans = ",".join({s.transition for s in plan.segments})
    wm = f"/水印:{plan.watermark_text}" if plan.watermark_text else ""
    lines.append(
        f"| {idx} | {plan.seed} | {used[:200]} | {trans} "
        f"gop{plan.gop}/{plan.bitrate}/字幕{plan.subtitle_style}{wm} | "
        f"{plan.target_duration:.1f}s | {script[:30]} | "
        f"{datetime.now():%Y-%m-%d %H:%M} |")
    tmp = Path(f"/tmp/mix_ledger_{batch}.md")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if existing:
        client.update_content(existing.id, tmp)
    else:
        client.upload(tmp, batch_folder.id, name=ledger_name)
    tmp.unlink(missing_ok=True)


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

    p = sub.add_parser("mix", help="功能二：混剪成片（去重）")
    p.add_argument("--taskform", required=True,
                   help="任务单网盘路径，如 颗粒/任务单-2026-10-01.xlsx")
    p.add_argument("--count", type=int, default=None, help="成片数（默认读任务单）")
    p.add_argument("--script", default=None, help="话术文本或文本文件路径")
    p.add_argument("--scripts-file", default=None, help="话术文件，一行一条")
    p.add_argument("--audio", default=None, help="话术音频本地路径（可选）")
    p.add_argument("--opening-audio", default=None,
                   help="开头爆款原音频本地路径（方式二：前几秒用爆款原声+原画面，后面接话术配音）")
    p.add_argument("--intensity", choices=["轻", "中", "强"], default=None,
                   help="去重强度（默认读任务单）")
    p.add_argument("--seed", type=int, default=None, help="起始 seed（默认 20261002）")
    p.add_argument("--size", default=None,
                   help="输出分辨率，如 1080x1920（默认）或 720x1280（吃力时降级）")
    p.add_argument("--pad-bytes", type=int, default=0,
                   help="文件尾追加随机字节数（破文件哈希，可选）")
    p.set_defaults(fn=cmd_mix)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
