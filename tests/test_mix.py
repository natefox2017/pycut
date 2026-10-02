"""混剪（二期）单元测试：规划去重、时长对齐、字幕生成。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pycut.mix import (ClipInfo, MixPlanner, build_filtergraph,
                       parse_slice_index, segment_filter)
from pycut.subtitles import distribute_times, split_lines, write_ass


def _clip(name, cat="痛点描述", dur=6.0, fid=None):
    return ClipInfo(file_id=fid or f"id_{name}", name=name,
                    category=cat, duration=dur)


def _library():
    lib = {}
    for i, cat in enumerate(["开头钩子", "痛点描述", "产品介绍",
                             "使用场景", "结尾转化"]):
        lib[cat] = [_clip(f"src{i:02d}_{j:02d}_x.mp4", cat, 5.0 + j,
                           fid=f"fid_{i}_{j}")
                    for j in range(6)]
    return lib


def test_plan_seed_reproducible():
    lib, hooks = _library(), _library()["开头钩子"]
    p1 = MixPlanner(seed=42).plan(lib, hooks, 30.0)
    p2 = MixPlanner(seed=42).plan(lib, hooks, 30.0)
    assert [s.clip.name for s in p1.segments] == \
           [s.clip.name for s in p2.segments]
    assert p1.gop == p2.gop and p1.bitrate == p2.bitrate


def test_plan_no_overlap_source():
    lib, hooks = _library(), _library()["开头钩子"]
    plan = MixPlanner(seed=7).plan(lib, hooks, 30.0)
    prefixes = [s.clip.source_prefix for s in plan.segments]
    assert len(prefixes) == len(set(prefixes)), "同条成片出现同源切片"


def test_plan_hook_first_and_untouched():
    lib, hooks = _library(), _library()["开头钩子"]
    plan = MixPlanner(seed=7).plan(lib, hooks, 30.0)
    hook = plan.segments[0]
    assert hook.is_hook
    assert hook.clip.category == "开头钩子"
    assert hook.speed == 1.0 and not hook.mirror


def test_usage_balancing():
    lib, hooks = _library(), _library()["开头钩子"]
    planner = MixPlanner(seed=1)
    # 把某个切片 usage 调高，它不应再被选中
    planner.usage["fid_0_0"] = 100
    plan = planner.plan(lib, hooks, 30.0)
    names = [s.clip.file_id for s in plan.segments]
    assert "fid_0_0" not in names


def test_align_durations_total():
    durs = [4.0, 6.0, 5.0, 7.0]
    speeds = [1.0, 1.0, 1.0, 1.0]
    # 钩子 4s 恒 1.0x，其余最多 1.1x -> 最小 20.36s，取可达目标
    out, new_speeds = MixPlanner._align_durations(durs, speeds, 21.0)
    assert abs(sum(out) - 21.0) < 0.15
    assert all(0.9 <= s <= 1.1 for s in new_speeds)
    assert new_speeds[0] == 1.0  # 钩子不动
    # 反向：目标比总和大
    out2, sp2 = MixPlanner._align_durations(durs, speeds, 24.0)
    assert abs(sum(out2) - 24.0) < 0.15
    # 不可达目标时取最接近（残余由渲染 -t 兜底）
    out3, _ = MixPlanner._align_durations(durs, speeds, 20.0)
    assert abs(sum(out3) - 20.36) < 0.15


def test_segment_filter_has_noise():
    seg_plans = MixPlanner(seed=3)._plan_segment(
        _clip("a.mp4"), False, 6.0, 1.0)
    f = segment_filter(seg_plans, 0)
    assert "noise=alls=" in f
    assert "scale=1080:1920" in f


def test_segment_filter_syntax():
    """输入/输出标签与滤镜之间不能有多余逗号。"""
    lib = {"开头钩子": [_clip("a.mp4", "开头钩子", 7.0)],
           "痛点描述": [_clip("b.mp4", "痛点描述", 6.0)]}
    plan = MixPlanner(seed=42).plan(lib, lib["开头钩子"], 10.0)
    fg, _ = build_filtergraph(plan)
    assert "[0:v]," not in fg and ",[v0]" not in fg
    assert "[1:v]," not in fg and ",[v1]" not in fg
    # 每个片段链以 [N:v] 开头、以 [vN] 结尾
    for i in range(len(plan.segments)):
        assert f"[{i}:v]" in fg and f"[v{i}]" in fg


def test_filtergraph_concat():
    lib, hooks = _library(), _library()["开头钩子"]
    plan = MixPlanner(seed=9).plan(lib, hooks, 25.0)
    fg, _ = build_filtergraph(plan)
    assert f"concat=n={len(plan.segments)}" in fg
    assert fg.count("[v") >= len(plan.segments)


def test_zoom_103_in_filter():
    seg = MixPlanner(seed=5)._plan_segment(_clip("a.mp4"), False, 6.0, 1.0)
    seg.zoom_103 = True
    seg.crop_shift = None
    f = segment_filter(seg, 0)
    assert "scale=iw*1.03" in f


def test_zoom_103_safe_for_small_input():
    """小分辨率源：zoom_103 先缩放到目标尺寸再放大，不越界。"""
    seg = MixPlanner(seed=5)._plan_segment(_clip("a.mp4"), False, 6.0, 1.0)
    seg.zoom_103 = True
    seg.crop_shift = None
    f = segment_filter(seg, 0, w=1080, h=1920)
    # 应该先 scale 到 1080x1920
    assert f.index("scale=1080:1920") < f.index("scale=iw*1.03")


def test_drop_frames_in_filter():
    seg = MixPlanner(seed=5)._plan_segment(_clip("a.mp4"), False, 6.0, 1.0)
    seg.drop_frames = True
    f = segment_filter(seg, 0)
    assert "select=" in f


def test_watermark_in_plan():
    lib, hooks = _library(), _library()["开头钩子"]
    # 中强度应有水印，轻强度没有
    p_mid = MixPlanner(seed=1, intensity="中").plan(lib, hooks, 20.0)
    assert p_mid.watermark_text != ""
    p_light = MixPlanner(seed=1, intensity="轻").plan(lib, hooks, 20.0)
    assert p_light.watermark_text == ""


def test_blur_bg_filter():
    seg = MixPlanner(seed=5)._plan_segment(_clip("a.mp4"), False, 6.0, 1.0)
    seg.blur_bg = True
    f = segment_filter(seg, 0)
    assert "gblur=sigma=40" in f and "overlay=" in f


def test_vignette_in_filter():
    seg = MixPlanner(seed=5)._plan_segment(_clip("a.mp4"), False, 6.0, 1.0)
    seg.vignette = True
    f = segment_filter(seg, 0)
    assert "vignette=" in f


def test_plan_with_script():
    """有话术时按句子分段选片，且保持句子顺序不打乱。"""
    lib, hooks = _library(), _library()["开头钩子"]
    script = "你敢相信吗。用啤酒消灭老鼠。又快又猛。"
    plan = MixPlanner(seed=42).plan(lib, hooks, 20.0, script=script)
    # 3 句 + 1 钩子 = 4 段
    assert len(plan.segments) == 4
    assert plan.segments[0].is_hook
    # 句子顺序保持：多次规划同一 seed 结果一致（不 shuffle）
    plan2 = MixPlanner(seed=42).plan(lib, hooks, 20.0, script=script)
    names1 = [s.clip.name for s in plan.segments]
    names2 = [s.clip.name for s in plan2.segments]
    assert names1 == names2


def test_plan_without_script_shuffles():
    """无话术时中间段打散（seed 可复现）。"""
    lib, hooks = _library(), _library()["开头钩子"]
    plan1 = MixPlanner(seed=42).plan(lib, hooks, 20.0)
    plan2 = MixPlanner(seed=42).plan(lib, hooks, 20.0)
    names1 = [s.clip.name for s in plan1.segments]
    names2 = [s.clip.name for s in plan2.segments]
    assert names1 == names2  # 同 seed 可复现


def test_plan_custom_size():
    lib, hooks = _library(), _library()["开头钩子"]
    plan = MixPlanner(seed=42).plan(lib, hooks, 20.0)
    plan.width, plan.height = 720, 1280
    fg, _ = build_filtergraph(plan)
    assert "scale=720:1280" in fg


def test_parse_slice_index():
    text = ("- abc_001_开头钩子.mp4 | 6.0s | 开头 | 你敢相信吗\n"
            "- notavideo.txt | 3s | x | y\n"
            "- def_002_痛点.mp4 | 5.5s | 痛点 | 老鼠太多了\n")
    out = parse_slice_index(text)
    assert len(out) == 2
    assert out[0]["name"] == "abc_001_开头钩子.mp4"
    assert out[0]["duration"] == 6.0
    assert out[1]["speech"] == "老鼠太多了"


def test_split_lines():
    lines = split_lines("你敢相信吗，用啤酒来消灭老鼠，那是又快又猛又狠。")
    assert all(len(x) <= 13 for x in lines)
    assert "".join(lines) == "你敢相信吗，用啤酒来消灭老鼠，那是又快又猛又狠。"


def test_distribute_times():
    ts = distribute_times(["ab", "abcd"], 10.0)
    assert ts[0][0] == 0.0 and ts[-1][1] == 10.0
    # 字数 2:4 -> 时长 1:2
    assert abs((ts[0][1] - ts[0][0]) * 2 - (ts[1][1] - ts[1][0])) < 0.01


def test_write_ass(tmp_path):
    fonts = {"Noto Sans SC Bold": "Noto Sans SC Bold",
             "ZhanKu KuaiLe": "ZhanKu KuaiLe"}
    for style in (0, 1, 2):
        p = tmp_path / f"t{style}.ass"
        write_ass("灭鼠颗粒19元11包，安全灭鼠。", 10.0, style, fonts, p)
        txt = p.read_text(encoding="utf-8")
        assert "[Events]" in txt and "Dialogue:" in txt
    # 样式1 价格标红
    p1 = tmp_path / "t1.ass"
    write_ass("19元11包", 5.0, 1, fonts, p1)
    assert r"{\c&H0000FF&}" in p1.read_text(encoding="utf-8")
