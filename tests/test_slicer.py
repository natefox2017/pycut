"""plan_cuts 纯逻辑测试（不依赖 ffmpeg）。"""
from pycut.config import SliceRules
from pycut.slicer import plan_cuts

R = SliceRules()  # skip<10s, min 2s, max 8s


def test_short_skipped():
    assert plan_cuts(9.9, [], R) == []


def test_whole_kept():
    cuts = plan_cuts(15.0, [], R)
    assert len(cuts) == 2
    for s, e in cuts:
        assert 2.0 - 0.3 <= e - s <= 8.0 + 0.3


def test_tail_redistribution():
    # 9 秒无场景：应分成 2 段（如 4.5+4.5），而不是 8+1
    cuts = plan_cuts(20.0, [], R)
    for s, e in cuts:
        assert e - s >= 2.0 - 0.3, f"出现过小尾段: {cuts}"


def test_scene_bounds_respected():
    # 场景边界 5s：20s 视频应在 5s 处切分
    cuts = plan_cuts(20.0, [5.0, 12.0], R)
    bounds = {round(s, 1) for s, e in cuts for s in (s,)} | \
             {round(e, 1) for s, e in cuts for e in (e,)}
    assert 5.0 in bounds


def test_small_merged():
    # 密集小场景应合并，不产生 <2s 碎片
    cuts = plan_cuts(30.0, [1.0, 2.0, 3.0, 15.0], R)
    for s, e in cuts:
        assert e - s >= 2.0 - 0.3, f"碎片: {cuts}"


def test_coverage_no_gaps():
    cuts = plan_cuts(25.0, [7.0, 13.0, 21.0], R)
    assert abs(cuts[0][0] - 0.0) < 0.01
    assert abs(cuts[-1][1] - 25.0) < 0.01
    for (s1, e1), (s2, e2) in zip(cuts, cuts[1:]):
        assert abs(e1 - s2) < 0.01, "切段之间有缝隙"
