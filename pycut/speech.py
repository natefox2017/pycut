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
    words: list = None  # 词级时间戳 [{w, s, e}]，with_words=True 时填充

    def __post_init__(self):
        if self.words is None:
            self.words = []


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
want_words = len(sys.argv) > 3 and sys.argv[3] == "words"
model = WhisperModel(model_name, device="cpu", compute_type="int8")
# 不走 faster-whisper 内置的 av 解码（PyAV 19 移除了 metadata_errors 参数，
# 版本不兼容）：wav 已是 16k 单声道，直接用标准库解码成 float32 数组
with wave.open(src, "rb") as w:
    assert w.getnchannels() == 1 and w.getsampwidth() == 2 and w.getframerate() == 16000
    audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
segments, _info = model.transcribe(audio, language="zh", vad_filter=True,
    vad_parameters=dict(min_silence_duration_ms=400),
    word_timestamps=want_words)
out = []
for s in segments:
    if not s.text.strip():
        continue
    d = {"start": round(s.start, 2), "end": round(s.end, 2),
         "text": s.text.strip()}
    if want_words:
        d["words"] = [{"w": wd.word, "s": round(wd.start, 2),
                       "e": round(wd.end, 2)} for wd in (s.words or [])]
    out.append(d)
print(json.dumps(out, ensure_ascii=False))
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
               timeout: int = 1800,
               with_words: bool = False) -> list[SpeechSegment]:
    """转录音频，返回句子级片段（含标点，时间戳到句子边界）。

    with_words=True 时同时返回词级时间戳（存于 SpeechSegment.words，
    用于完整话语边界精确定位）。
    """
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
        args = [str(VENV_PYTHON), "-c", _TRANSCRIBE_SCRIPT, str(wav), model]
        if with_words:
            args.append("words")
        pr = subprocess.run(args, capture_output=True, text=True,
                            timeout=timeout, env=env)
        if pr.returncode != 0:
            raise RuntimeError(f"转录失败: {pr.stderr[-500:]}")
        data = json.loads(pr.stdout)
        segs = []
        for s in data:
            seg = SpeechSegment(s["start"], s["end"], s["text"])
            if with_words:
                seg.words = s.get("words", [])
            segs.append(seg)
        return segs
    finally:
        wav.unlink(missing_ok=True)


# 话语未完标记：以这些结尾的片段，话没说完，不能在此切断。
# （2026-10-01 pilot 教训：STT 无标点，停顿≠说完；"原因呢|是…"、
# "下水道的缝隙|窗户的缝隙"都是在此切断导致用户投诉）
_INCOMPLETE_END = (
    "呢", "吗", "吧", "呀", "啊", "么",  # 提问/设问，等待回答
    "因为", "所以", "如果", "要是", "的话",  # 条件/因果前半
    "但是", "不过", "而且", "然后", "接着", "首先",  # 转折/承接
    "第一", "第二", "第三",  # 枚举未完
    "、",  # 顿号枚举未完
    "的缝隙", "的地方", "的时候", "的里面", "的里边",  # 的+名词，介词短语未完
    "顺着", "通过", "经过", "按照",  # 介词开头，主句动词还没出现
)

_INCOMPLETE_START = (
    "是", "就是", "比如", "例如",  # 回答/举例承接上文提问
    "它", "他", "她", "这", "那",  # 代词开头大概率承接上句
    "窗户", "门", "墙",  # 并列名词承接上文枚举（如下水道缝隙→窗户缝隙）
)


def _ends_incomplete(text: str) -> bool:
    t = text.strip()
    return t.endswith(_INCOMPLETE_END)


def _starts_continuation(text: str) -> bool:
    t = text.strip()
    return t.startswith(_INCOMPLETE_START)


def merge_incomplete(segs: list[SpeechSegment],
                     max_dur: float = 20.0) -> list[SpeechSegment]:
    """合并话语未完的片段，保证每段都是一句完整的话。

    规则：
    1. 片段以未完标记结尾（如"原因呢"）→ 与下一段合并。
    2. 下一段以承接词开头（如"是…"）→ 与上一段合并。
    3. 完整表达优先：合并后超过 max_dur 也保留，不截断。
    4. 合并时时间戳取首尾，文本用空格连接。
    """
    if not segs:
        return []
    out: list[SpeechSegment] = []
    buf: SpeechSegment | None = None
    for s in segs:
        if buf is None:
            buf = s
            continue
        # 上一段没说完，或这一段是承接 → 合并
        if _ends_incomplete(buf.text) or _starts_continuation(s.text):
            if buf.end - buf.start < max_dur:
                buf = SpeechSegment(
                    buf.start, s.end,
                    (buf.text + " " + s.text).strip())
                if hasattr(s, "words"):
                    buf.words = getattr(buf, "words", []) + s.words
                continue
        out.append(buf)
        buf = s
    if buf is not None:
        out.append(buf)
    return out
