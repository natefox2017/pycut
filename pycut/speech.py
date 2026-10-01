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
import json, sys
from faster_whisper import WhisperModel
src, model_name = sys.argv[1], sys.argv[2]
model = WhisperModel(model_name, device="cpu", compute_type="int8")
segments, _info = model.transcribe(src, language="zh", vad_filter=True,
    vad_parameters=dict(min_silence_duration_ms=400))
segs = [{"start": round(s.start, 2), "end": round(s.end, 2),
         "text": s.text.strip()} for s in segments if s.text.strip()]
print(json.dumps(segs, ensure_ascii=False))
"""


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
