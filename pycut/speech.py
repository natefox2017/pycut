"""语音转录：句子级时间戳。

云电脑说明（必读）：
- faster-whisper 装在独立 venv（pycut/.venv），不污染系统 Python。
  系统 pip 因 PEP 668 + debian 包冲突装不上，这是用 venv 的原因。
- 通过子进程调用 venv 里的 Python，主 pipeline 保持零第三方依赖：
  venv 坏了只影响转录（降级为纯场景切点），不影响切片主流程。
- 模型：small（int8 CPU），首次运行自动从 HuggingFace 下载（约 1GB，
  缓存在 ~/.cache/huggingface）。
- 网络注意：本机 httpx 解析 no_proxy 里的 IPv6 项（[::1]）会崩，
  所以子进程环境里删掉了 no_proxy/NO_PROXY（见 transcribe()）。
  如换云电脑后转录下载失败，优先检查这点。
- 用途：
  1. 切点对齐到句子边界：切片里有话术，必须把一句话说完（用户要求）。
  2. 话术转录（voice.py 的 WhisperTranscriber 后端）。
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

VENV_PYTHON = Path(__file__).resolve().parent.parent / ".venv" / "bin" / "python"
MODEL = "small"


@dataclass
class SpeechSegment:
    start: float
    end: float
    text: str


def available() -> bool:
    if not VENV_PYTHON.exists():
        return False
    r = subprocess.run([str(VENV_PYTHON), "-c", "import faster_whisper"],
                       capture_output=True, timeout=30)
    return r.returncode == 0


_TRANSCRIBE_SCRIPT = r"""
import json, sys, wave
import numpy as np
from faster_whisper import WhisperModel
src, model_name = sys.argv[1], sys.argv[2]
model = WhisperModel(model_name, device="cpu", compute_type="int8")
# 不走 faster-whisper 内置的 av 解码（PyAV 19 移除了 metadata_errors 参数，
# 版本不兼容）：wav 已是 16k 单声道，直接用标准库解码成 float32 数组
with wave.open(src, "rb") as w:
    assert w.getnchannels() == 1 and w.getsampwidth() == 2 and w.getframerate() == 16000
    audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
segments, _info = model.transcribe(audio, language="zh", vad_filter=True,
    vad_parameters=dict(min_silence_duration_ms=400))
segs = [{"start": round(s.start, 2), "end": round(s.end, 2),
         "text": s.text.strip()} for s in segments if s.text.strip()]
print(json.dumps(segs, ensure_ascii=False))
"""


def refine_sentences(segs: list[SpeechSegment],
                     max_dur: float = 9.0) -> list[SpeechSegment]:
    """过长句子按标点拆分。

    STT 有时会把多句并成一段很长的"句子"；超过 max_dur 的按标点
    （，。？！；：）拆分，拆分时间按字数比例估算。保证切点规划有足够的
    句子边界可用。
    """
    import re
    out: list[SpeechSegment] = []
    for s in segs:
        dur = s.end - s.start
        if dur <= max_dur:
            out.append(s)
            continue
        # 找标点拆分点
        parts = re.split(r"([，。？！；：])", s.text)
        # 重组为带标点的片段
        chunks: list[str] = []
        buf = ""
        for p in parts:
            buf += p
            if p in "，。？！；：":
                chunks.append(buf.strip())
                buf = ""
        if buf.strip():
            chunks.append(buf.strip())
        chunks = [c for c in chunks if c]
        if len(chunks) < 2:
            out.append(s)  # 无标点可拆，原样保留
            continue
        total = sum(len(c) for c in chunks)
        t = s.start
        for i, c in enumerate(chunks):
            frac = len(c) / total
            e = s.end if i == len(chunks) - 1 else t + dur * frac
            out.append(SpeechSegment(round(t, 2), round(e, 2), c))
            t = e
    return out


def transcribe(src: Path, model: str = MODEL,
               timeout: int = 1800) -> list[SpeechSegment]:
    """转录音频，返回句子级片段（含标点，时间戳到句子边界）。"""
    if not available():
        raise RuntimeError("faster-whisper venv 不可用")
    # 先抽单声道 16k wav，转录更快更稳
    wav = src.parent / (src.stem + "_16k.wav")
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", str(wav)],
        capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"音频抽取失败: {r.stderr[:200]}")
    try:
        import os
        env = {k: v for k, v in os.environ.items()
               if k not in ("no_proxy", "NO_PROXY")}  # httpx 解析 [::1] 会崩
        pr = subprocess.run(
            [str(VENV_PYTHON), "-c", _TRANSCRIBE_SCRIPT, str(wav), model],
            capture_output=True, text=True, timeout=timeout, env=env)
        if pr.returncode != 0:
            raise RuntimeError(f"转录失败: {pr.stderr[-500:]}")
        data = json.loads(pr.stdout)
        return [SpeechSegment(s["start"], s["end"], s["text"]) for s in data]
    finally:
        wav.unlink(missing_ok=True)
