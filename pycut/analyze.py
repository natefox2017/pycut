"""AI 分析阶段：程序准备证据，AI 做决策。

定位（对应 easycut「AI 只提案，Host 执行」）：
- 程序：场景初分段 -> 每段抽代表帧（头/中/尾）-> 音频信息 -> evidence.json
- AI（现阶段为 Arlo 人工分析，预留 vision 模型 API）：看帧决定
  最终切点（可合并/拆分/调整初分段）与每段的分类（文件夹）。
- 程序：校验 decisions.json -> 按决策导出切片。

证据包目录结构：
    review_pack/<md5_8>_<文件名>/
        evidence.json      # 分段证据
        frames/seg_000_f0.jpg ...
        decisions.json     # AI 填写的决策（模板由程序生成）
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .categorize import REVIEW_CATEGORY
from .config import CATEGORIES, SliceRules
from .media import (MediaInfo, detect_scenes, extract_frames_batch,
                    mean_volume_db, probe)
from .slicer import plan_cuts, snap_cuts_to_speech


@dataclass
class SegmentEvidence:
    index: int
    start: float
    end: float
    duration: float
    frames: list[str] = field(default_factory=list)  # 相对路径
    has_audio: bool = False
    volume_db: float | None = None
    speech: str = ""  # 本段内的转录话术（句子对齐后应为完整句子）


@dataclass
class CutDecision:
    index: int
    start: float
    end: float
    category: str
    reason: str = ""


def _safe_name(name: str) -> str:
    keep = []
    for ch in name:
        if ch.isalnum() or ch in ("-", "_", "."):
            keep.append(ch)
        elif ch == " ":
            keep.append("_")
    return "".join(keep)[:40]


class EvidenceBuilder:
    """为单个源视频构建 AI 分析证据包。"""

    def __init__(self, rules: SliceRules):
        self.rules = rules

    def build(self, src: Path, md5: str, pack_root: Path) -> Path:
        info = probe(src)
        scenes = detect_scenes(src, self.rules.scene_threshold)
        # 初分段：场景规划
        cuts = plan_cuts(info.duration, scenes, self.rules)
        # 句子对齐：转录话术，把切点吸附到句子边界（有话术必须说完一句）
        sentences: list[tuple[float, float, str]] = []
        if info.has_audio:
            try:
                from .speech import available as stt_available, transcribe
                if stt_available():
                    print("  🎙 转录话术取句子时间戳...")
                    segs = transcribe(src)
                    sentences = [(s.start, s.end, s.text) for s in segs]
                    cuts = snap_cuts_to_speech(
                        cuts, [(s, e) for s, e, _ in sentences], self.rules)
                    print(f"  句子对齐后 {len(cuts)} 段，共 {len(sentences)} 句")
                else:
                    print("  ⚠ faster-whisper 不可用，跳过句子对齐")
            except Exception as ex:
                print(f"  ⚠ 转录失败({str(ex)[:80]})，用纯场景切点")
        pack = pack_root / f"{md5[:8]}_{_safe_name(src.stem)}"
        frames_dir = pack / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)

        segments: list[SegmentEvidence] = []
        frame_jobs: list[tuple[float, Path]] = []
        for i, (s, e) in enumerate(cuts):
            fps: list[str] = []
            for j, t in enumerate((s + 0.3, (s + e) / 2, max(s + 0.3, e - 0.3))):
                fp = frames_dir / f"seg_{i:03d}_f{j}.jpg"
                fps.append(f"frames/{fp.name}")
                frame_jobs.append((min(t, e - 0.1), fp))
            # 本段内的话术（句子对齐后应为完整句子）
            speech = " ".join(t for ss, ee, t in sentences
                              if ss >= s - 0.3 and ee <= e + 0.3)
            segments.append(SegmentEvidence(
                index=i, start=round(s, 2), end=round(e, 2),
                duration=round(e - s, 2), frames=fps,
                has_audio=info.has_audio, speech=speech))
        # 单次解码批量抽帧（更快更稳）
        extract_frames_batch(src, frame_jobs, width=480)

        # 音频信息（整条一次，避免逐段重复计算）
        vol = mean_volume_db(src) if info.has_audio else None
        for sg in segments:
            sg.volume_db = vol

        evidence = {
            "src_name": src.name,
            "md5": md5,
            "duration": round(info.duration, 2),
            "size": f"{info.width}x{info.height}",
            "categories": CATEGORIES,
            "rules": {
                "min_seconds": self.rules.min_slice_seconds,
                "max_seconds": self.rules.max_slice_seconds,
                "note": "AI 可合并/拆分/微调初分段；时长尽量落在 [min, max] 内",
            },
            "segments": [asdict(s) for s in segments],
        }
        (pack / "evidence.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        # 决策模板
        template = [
            {"index": s.index, "start": s.start, "end": s.end,
             "category": REVIEW_CATEGORY, "reason": ""}
            for s in segments
        ]
        (pack / "decisions.json").write_text(
            json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8")
        return pack


def load_decisions(decisions_file: Path) -> list[CutDecision]:
    """读取并校验 AI 决策。传入 decisions.json 文件路径。"""
    data = json.loads(decisions_file.read_text(encoding="utf-8"))
    decisions = [CutDecision(**d) for d in data]
    for d in decisions:
        if d.category not in CATEGORIES:
            raise ValueError(f"非法分类: {d.category}（段 {d.index}）")
        if d.end <= d.start:
            raise ValueError(f"非法区间: {d.start}-{d.end}（段 {d.index}）")
    # 按时间排序并检查重叠
    decisions.sort(key=lambda d: d.start)
    for a, b in zip(decisions, decisions[1:]):
        if b.start < a.end - 0.01:
            raise ValueError(f"区间重叠: 段{a.index} 与段{b.index}")
    return decisions


def decisions_to_cuts(decisions: list[CutDecision],
                      ) -> list[tuple[tuple[float, float], str, str]]:
    """决策 -> [((start, end), category, reason)]，供 pipeline 导出使用。"""
    return [((d.start, d.end), d.category, d.reason) for d in decisions]
