"""分类：启发式预分类 + 待复核兜底 + Arlo 人工复核入口。"""
from __future__ import annotations

from pathlib import Path

from .config import CATEGORIES, REVIEW_CATEGORY
from .media import MediaInfo, extract_frame, mean_volume_db


def heuristic_category(info: MediaInfo, src_name: str,
                       keyword_map: dict[str, str] | None = None) -> str:
    """启发式分类。keyword_map: {关键词: 分类}，按源文件名匹配。

    规则按置信度从高到低：
    1. 用户关键词映射（最准，人工指定）
    2. 兜底 -> 待复核（由 Arlo 看帧后人工定类，见 decide_category）
    """
    keyword_map = keyword_map or {}
    for kw, cat in keyword_map.items():
        if kw and kw in src_name and cat in CATEGORIES:
            return cat
    return REVIEW_CATEGORY


def audio_hint_category(media_path: Path) -> str | None:
    """基于音频的辅助判断：有持续人声倾向 -> 人物口播。

    返回分类建议或 None（无法判断）。阈值保守，宁可返回 None
    让切片进待复核，也不误判。
    """
    vol = mean_volume_db(media_path)
    if vol is None:
        return None
    # 平均音量高于 -30dB 且有音频轨：大概率含持续人声/口播
    if vol > -30:
        return "人物口播"
    return None


def review_frames(src: Path, cuts: list[tuple[float, float]],
                  out_dir: Path) -> list[Path]:
    """为每条切片抽 3 帧（头/中/尾），供 Arlo 人工复核分类。"""
    paths: list[Path] = []
    for i, (s, e) in enumerate(cuts):
        mid = (s + e) / 2
        for j, t in enumerate((s + 0.3, mid, max(s + 0.3, e - 0.3))):
            fp = out_dir / f"cut_{i:03d}_f{j}.jpg"
            extract_frame(src, min(t, e - 0.1), fp)
            paths.append(fp)
    return paths


def decide_category(info: MediaInfo, src_name: str, media_path: Path,
                    keyword_map: dict[str, str] | None = None) -> tuple[str, str]:
    """综合判定分类，返回 (分类, 依据说明)。"""
    kw_cat = heuristic_category(info, src_name, keyword_map)
    if kw_cat != REVIEW_CATEGORY:
        return kw_cat, f"关键词命中: {src_name}"
    hint = audio_hint_category(media_path)
    if hint:
        return hint, "音频启发式: 持续人声"
    return REVIEW_CATEGORY, "无可靠依据，转人工复核"
