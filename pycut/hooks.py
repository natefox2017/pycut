"""爆款开头池：混剪的开头策略（用户 2026-10-01 确认）。

策略：爆款视频的开头都是同款，用同样的前几秒做开头更容易爆。
混剪时统一用爆款同款开头，只做轻微去重变换（裁剪位移/色调二选一），
不做镜像/变速等会损伤钩子效果的变换。

开头池 = 从爆款视频取前 N 秒（标准化后）存入 hook_pool/，
混剪（二期）时轮换使用。
"""
from __future__ import annotations

from pathlib import Path

from .config import SliceRules
from .media import probe
from .slicer import normalize_full


def extract_hook(src: Path, dst: Path, seconds: float = 4.0,
                 rules: SliceRules | None = None) -> Path:
    """取视频前 N 秒并标准化，存入开头池。"""
    import subprocess

    rules = rules or SliceRules()
    info = probe(src)
    dur = min(seconds, info.duration)
    tmp = dst.parent / (dst.stem + "_raw.mp4")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(src), "-t", f"{dur:.1f}",
         "-c", "copy", str(tmp)],
        capture_output=True, text=True, timeout=300,
    )
    if r.returncode != 0 or not tmp.exists():
        raise RuntimeError(f"开头截取失败 {src.name}")
    try:
        return normalize_full(tmp, dst, rules)
    finally:
        tmp.unlink(missing_ok=True)
