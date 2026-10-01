"""话术来源：爆款视频音频转录。

话术优先级（用户 2026-10-01 确认）：
1. 爆款视频音频转录：用爆款视频的原音频生成文案（爆款验证过的说法）。
2. 用户直接提供文案。

转录方式：
- 现阶段：Arlo 人工转录。程序按固定间隔抽字幕帧（爆款视频多为烧录字幕），
  Arlo 读帧整理成文，再经 product.check_script() 事实核对。
- 预留：WhisperTranscriber（本地 Whisper 模型，待用户确认后再接入）。

输出：话术文件（UTF-8，每行一条独立话术），存网盘与 product.yaml 同级。
"""
from __future__ import annotations

from pathlib import Path

from .media import MediaInfo, extract_frames_batch, probe


def subtitle_frame_times(duration: float, every: float = 5.0) -> list[float]:
    """按固定间隔生成抽帧时间点（用于读烧录字幕）。"""
    ts: list[float] = []
    t = 1.0
    while t < duration - 0.5:
        ts.append(round(t, 1))
        t += every
    return ts


def extract_subtitle_frames(src: Path, out_dir: Path,
                            every: float = 5.0, width: int = 720) -> list[Path]:
    """抽字幕帧，供 Arlo 读字幕整理话术。返回帧路径列表。"""
    info: MediaInfo = probe(src)
    out_dir.mkdir(parents=True, exist_ok=True)
    jobs = [(t, out_dir / f"sub_{t:07.1f}.jpg")
            for t in subtitle_frame_times(info.duration, every)]
    extract_frames_batch(src, jobs, width=width)
    return [p for _, p in jobs if p.exists()]


class WhisperTranscriber:
    """预留：本地 Whisper 转录。接入需要下载模型（约 500MB~1GB），
    待用户确认磁盘与时间成本后再实现。"""

    def __init__(self, model: str = "small"):
        self.model = model

    def transcribe(self, audio_path: Path) -> str:
        raise NotImplementedError("Whisper 未接入：需用户确认后再下载模型实现")
