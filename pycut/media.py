"""媒体分析：ffprobe 元数据、场景检测、抽帧。"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class MediaInfo:
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    has_audio: bool = False
    vcodec: str = ""

    @property
    def is_portrait(self) -> bool:
        return self.height >= self.width and self.width > 0


def probe(path: Path) -> MediaInfo:
    """读取视频元数据。失败抛 RuntimeError。"""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format",
         "-of", "json", str(path)],
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe 失败 {path.name}: {r.stderr.strip()[:200]}")
    data = json.loads(r.stdout)
    info = MediaInfo()
    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and info.width == 0:
            info.width = int(s.get("width") or 0)
            info.height = int(s.get("height") or 0)
            info.vcodec = s.get("codec_name", "")
            fps_s = s.get("avg_frame_rate") or s.get("r_frame_rate") or "0/1"
            try:
                n, d = fps_s.split("/")
                info.fps = float(n) / float(d) if float(d) else 0.0
            except (ValueError, ZeroDivisionError):
                info.fps = 0.0
        elif s.get("codec_type") == "audio":
            info.has_audio = True
    fmt = data.get("format", {})
    try:
        info.duration = float(fmt.get("duration") or 0.0)
    except ValueError:
        info.duration = 0.0
    return info


def detect_scenes(path: Path, threshold: float = 0.35) -> list[float]:
    """场景切换时间点（秒）。失败返回空列表（不阻断流程）。"""
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path),
         "-vf", f"select='gt(scene,{threshold})',showinfo",
         "-vsync", "0", "-f", "null", "-"],
        capture_output=True, text=True, timeout=600,
    )
    times: list[float] = []
    for m in re.finditer(r"pts_time:([0-9.]+)", r.stderr):
        try:
            times.append(float(m.group(1)))
        except ValueError:
            pass
    # 去重 + 排序（showinfo 可能重复输出）
    uniq: list[float] = []
    for t in sorted(times):
        if not uniq or t - uniq[-1] > 0.2:
            uniq.append(t)
    return uniq


def extract_frame(path: Path, t: float, out: Path,
                  width: int = 320) -> Path:
    """抽一帧（供 Arlo 人工复核分类用）。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-ss", f"{t:.2f}", "-i", str(path),
         "-frames:v", "1", "-vf", f"scale={width}:-1", str(out)],
        check=False, timeout=120,
    )
    return out


def mean_volume_db(path: Path) -> float | None:
    """平均音量（dB），用于判断是否有持续人声；无音频返回 None。"""
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path),
         "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, timeout=300,
    )
    m = re.search(r"mean_volume:\s*(-?[0-9.]+)\s*dB", r.stderr)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    if "n/a" in r.stderr and "mean_volume" in r.stderr:
        return None
    return None
