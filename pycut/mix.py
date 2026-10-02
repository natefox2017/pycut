"""混剪成片（二期）：用切片库批量混剪，核心目标是去重。

去重优先级（2026-10-02 调研结论，针对抖音/快手）：
1. 内容指纹层：抽帧感知哈希 + 音频指纹 + OCR 是查重主力。
   对策：1.03x 放大、抽帧、移动水印、字幕带模糊、打乱重排。
2. 特效层：时域噪点破坏像素级指纹，N 按 seed 在 10~25 变化，
   同一源切片在不同成片里噪点 pattern 完全不同。切片库本身永远干净。
3. 文件层：彻底重编码 —— 换 GOP 结构、换码率、换编码参数，
   输出二进制特征与源完全不同。（注：只改 MD5 基本无效）
4. 素材调度层：usage 均衡 + 同一条成片不重复高重叠 source range。
5. 单片段变换层（辅助）：裁剪位移 / 色调 / 变速；钩子只做裁剪位移+色调二选一，
   不做镜像/变速（保护钩子效果）。

注：暗水印无公开实证，不作为核心去重目标。
原则：音频是主轨道，视频向音频看齐（ffprobe 测话术音频时长 Ta）。
"""
from __future__ import annotations

import json
import random
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import CATEGORIES

#: 混剪默认规格（与切片库一致）
MIX_WIDTH = 1080
MIX_HEIGHT = 1920
MIX_FPS = 30

#: 去重强度档
INTENSITY_LEVELS = ("轻", "中", "强")

#: 噪点强度范围（按强度档）
NOISE_RANGE = {
    "轻": (8, 14),
    "中": (10, 25),
    "强": (18, 28),
}

#: 文件层：与切片（crf20/veryfast/默认GOP）不同的编码参数，按 seed 轮换
GOP_CHOICES = (48, 60, 72)
BITRATE_CHOICES = ("2500k", "3000k", "3500k")


@dataclass
class ClipInfo:
    """切片库中的一条候选切片。"""
    file_id: str
    name: str
    category: str
    duration: float
    speech: str = ""
    local_path: Path | None = None

    @property
    def source_prefix(self) -> str:
        """源视频标识：文件名 md5 前缀。同一源的不同切片前缀相同。"""
        return self.name.split("_")[0] if "_" in self.name else self.name


@dataclass
class SegmentPlan:
    """一个片段的规划：用哪条切片 + 变换 + 特效 + 转场。"""
    clip: ClipInfo
    is_hook: bool = False
    # 单片段变换
    crop_shift: tuple[float, float] | None = None  # (x_ratio, y_ratio) 裁剪偏移
    speed: float = 1.0                              # 变速（钩子恒为 1.0）
    tone: tuple[float, float, float] | None = None  # (亮度, 饱和度, 对比度) 偏移
    mirror: bool = False
    # 特效层
    noise_n: int = 15
    noise_variant: int = 0  # 0=时域噪点 1=平均时域噪点 2=时域+均匀噪点
    # 去重增强（2026-10-02 调研补齐）
    zoom_103: bool = False       # 1.03x 中心放大（裁掉边缘，破抽帧指纹）
    drop_frames: bool = False    # 抽帧：每 30 帧丢 1 帧
    blur_bg: bool = False        # 横屏素材：模糊背景填充转竖屏
    blur_sub_band: bool = False  # 底部字幕带模糊（盖掉源片烧录字幕）
    vignette: bool = False       # 暗角（§3.4 特效层）
    # P1/P2 开源调研补齐（2026-10-02）
    color_tweak: bool = False   # 色彩空间微调：hue + colorbalance
    frame_blend: bool = False   # 相邻帧混合：tblend，对抗抽帧哈希
    lens_distort: bool = False  # 边缘畸变：lenscorrection，对抗几何特征
    sharp_tweak: bool = False   # 锐度微调：unsharp，改变锐度指纹
    # 结构层
    transition: str = "hardcut"  # hardcut / fade / zoom
    # 时长对齐后
    out_duration: float = 0.0


@dataclass
class MixPlan:
    seed: int
    segments: list[SegmentPlan] = field(default_factory=list)
    audio_path: Path | None = None
    target_duration: float = 0.0   # Ta
    gop: int = 60
    bitrate: str = "3000k"
    subtitle_style: int = 0
    watermark_text: str = ""       # 移动水印文字（空=不加）
    sticker_path: str = ""         # 贴纸图片路径（空=不加）
    sticker_pos: str = "top-right" # 贴纸位置：top-left/top-right/bottom-left/bottom-right
    sticker_size: int = 160        # 贴纸尺寸（像素，配置 effects.sticker_size）
    width: int = MIX_WIDTH          # 输出分辨率（§9：吃力时降 720x1280）
    height: int = MIX_HEIGHT
    fps: int = MIX_FPS

    @property
    def video_duration(self) -> float:
        return round(sum(s.out_duration for s in self.segments), 2)


# ---------------------------------------------------------------------------
# 素材调度层
# ---------------------------------------------------------------------------

def pick_lowest_usage(candidates: list[ClipInfo],
                      usage: dict[str, int]) -> ClipInfo:
    """usage 均衡：优先选历史使用次数最少的切片（并列时随机顺序由调用方打乱）。"""
    return min(candidates, key=lambda c: usage.get(c.file_id or c.name, 0))


class MixPlanner:
    """seed 可复现的选片规划。"""

    #: 中段分类（按话术推进顺序的候选池；打乱重排时用）
    MIDDLE_CATEGORIES = [c for c in CATEGORIES
                         if c not in ("开头钩子", "结尾转化")]

    def __init__(self, seed: int, intensity: str = "中"):
        if intensity not in INTENSITY_LEVELS:
            raise ValueError(f"去重强度须为 {INTENSITY_LEVELS}")
        self.seed = seed
        self.intensity = intensity
        self.rng = random.Random(seed)
        self.usage: dict[str, int] = {}

    # -- usage 持久化 ------------------------------------------------------

    def load_usage(self, path: Path) -> None:
        if path.exists():
            try:
                self.usage = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.usage = {}

    def save_usage(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.usage, ensure_ascii=False, indent=1),
                        encoding="utf-8")

    def _bump_usage(self, clip: ClipInfo) -> None:
        key = clip.file_id or clip.name
        self.usage[key] = self.usage.get(key, 0) + 1

    # -- 选片 --------------------------------------------------------------

    def _choose_hook(self, hooks: list[ClipInfo]) -> ClipInfo:
        """开头池轮换：usage 最低的优先。"""
        pool = hooks[:]
        self.rng.shuffle(pool)
        return pick_lowest_usage(pool, self.usage)

    def _choose_by_sentences(self, library: dict[str, list[ClipInfo]],
                             used_prefixes: set[str], script: str,
                             audio_duration: float,
                             hook_seconds: float) -> list[ClipInfo]:
        """按句子分段时间轴选片（§4）：每句按字数估算时长，选对应时长的切片。

        中文约 4.5 字/秒。句子与切片一一对应，口播类不硬切。
        """
        import re
        # 按标点分句
        sentences = [s.strip() for s in re.split(r"[，。！？；]", script)
                     if s.strip()]
        if not sentences:
            return self._choose_middles(library, used_prefixes,
                                        audio_duration - hook_seconds + 2.0)
        # 每句估算时长
        total_chars = sum(len(s) for s in sentences)
        if total_chars == 0:
            return self._choose_middles(library, used_prefixes,
                                        audio_duration - hook_seconds + 2.0)
        avail = audio_duration - hook_seconds
        picked: list[ClipInfo] = []
        cats = self.MIDDLE_CATEGORIES[:]
        self.rng.shuffle(cats)
        ci = 0
        for sent in sentences:
            target = avail * len(sent) / total_chars
            # 找时长接近的切片（±30% 内优先）
            cands = []
            for cat in cats:
                for c in library.get(cat, []):
                    if c.source_prefix in used_prefixes:
                        continue
                    if c.file_id in {p.file_id for p in picked}:
                        continue
                    cands.append(c)
            if not cands:
                break
            # 按时长接近度 + usage 综合排序
            def _score(c: ClipInfo) -> tuple:
                dur_diff = abs(c.duration - target) / max(target, 0.1)
                use = self.usage.get(c.file_id or c.name, 0)
                return (dur_diff, use, self.rng.random())
            cands.sort(key=_score)
            # 取前 3 个中最优的（带一点随机性）
            clip = cands[0] if len(cands) == 1 else \
                self.rng.choice(cands[:min(3, len(cands))])
            picked.append(clip)
            used_prefixes.add(clip.source_prefix)
            # usage 在 plan() 里统一 bump，这里不重复
            ci += 1
        return picked

    def _choose_middles(self, library: dict[str, list[ClipInfo]],
                        used_prefixes: set[str], target: float) -> list[ClipInfo]:
        """按目标时长从中间分类池挑选；同一源前缀只用一次。"""
        picked: list[ClipInfo] = []
        acc = 0.0
        cats = self.MIDDLE_CATEGORIES[:]
        self.rng.shuffle(cats)  # 打乱分类顺序
        # 轮询各分类，保证多样性
        ci = 0
        guard = 0
        while acc < target and guard < 200:
            guard += 1
            cat = cats[ci % len(cats)]
            ci += 1
            cands = [c for c in library.get(cat, [])
                     if c.source_prefix not in used_prefixes
                     and c.file_id not in {p.file_id for p in picked}]
            if not cands:
                continue
            self.rng.shuffle(cands)
            clip = pick_lowest_usage(cands, self.usage)
            picked.append(clip)
            used_prefixes.add(clip.source_prefix)
            acc += clip.duration
        return picked

    def plan(self, library: dict[str, list[ClipInfo]], hook_pool: list[ClipInfo],
             audio_duration: float, hook_seconds: float = 4.0,
             script: str = "") -> MixPlan:
        """规划一条成片。library: 分类->切片；hook_pool: 开头候选。

        script: 话术文本，有则按句子分段时间轴选片（§4），无则按总时长。
        """
        if not hook_pool:
            raise ValueError("开头池为空，无法规划")
        plan = MixPlan(seed=self.seed, target_duration=round(audio_duration, 2))
        used_prefixes: set[str] = set()

        # 贴纸：按 seed 随机选（2026-10-02 用户要求混剪加贴纸）
        # 中/强强度 70% 概率加贴纸，轻强度不加
        # 配置 effects.sticker: 1=开 0=关；sticker_pos: 0=随机 1-4=四角
        from .settings import get as _get
        if int(_get("effects", "sticker", default=1) or 0):
            p_sticker = {1: 0.0, 2: 0.7, 3: 0.7}.get(
                {"轻": 1, "中": 2, "强": 3}.get(self.intensity, 2), 0.7)
            if self.rng.random() < p_sticker:
                import random as _r
                _rr = _r.Random(self.seed * 97 + 13)
                stickers_dir = Path(__file__).parent.parent / "assets" / "stickers"
                if stickers_dir.exists():
                    files = list(stickers_dir.glob("*.png"))
                    if files:
                        plan.sticker_path = str(_rr.choice(files))
                        pos_names = ["top-left", "top-right",
                                     "bottom-left", "bottom-right"]
                        cfg_pos = int(_get("effects", "sticker_pos", default=0) or 0)
                        if 1 <= cfg_pos <= 4:
                            plan.sticker_pos = pos_names[cfg_pos - 1]
                        else:
                            plan.sticker_pos = _rr.choice(pos_names)
                        plan.sticker_size = int(
                            _get("effects", "sticker_size", default=160) or 160)

        # 1. 开头：开头池轮换（爆款同款开头）
        hook = self._choose_hook(hook_pool)
        used_prefixes.add(hook.source_prefix)
        self._bump_usage(hook)

        # 2. 中间段：有话术按句子分段选片，无话术按总时长
        #    注意：按句子选片时顺序即对应话术时间轴，不能打乱（§4）
        use_sentence_order = bool(script.strip())
        if use_sentence_order:
            middles = self._choose_by_sentences(library, used_prefixes,
                                                 script, audio_duration,
                                                 hook_seconds)
        else:
            middles = self._choose_middles(library, used_prefixes,
                                           audio_duration - hook_seconds + 2.0)
        if not middles:
            raise ValueError("切片库素材不足，无法凑齐目标时长")
        for m in middles:
            self._bump_usage(m)

        # 3. 结构层：开头固定；无话术时中间打散重排，有话术时保持句子顺序
        if not use_sentence_order:
            self.rng.shuffle(middles)
        clips = [hook] + middles

        # 4. 先定每段变速（钩子恒 1.0，中间 0.95~1.05 随机），再做时长对齐。
        #    对齐在多个中间段之间分摊变速（0.9~1.1），使总和≈Ta。
        speeds = [1.0] + [round(self.rng.uniform(0.95, 1.05), 3)
                          for _ in middles]
        durations, speeds = self._align_durations(
            [c.duration for c in clips], speeds, audio_duration)

        # 5. 逐片段定变换/特效/转场
        for i, (clip, dur) in enumerate(zip(clips, durations)):
            is_hook = (i == 0)
            seg = self._plan_segment(clip, is_hook, dur, speeds[i])
            plan.segments.append(seg)

        # 6. 文件层 + 字幕样式按 seed 轮换
        plan.gop = self.rng.choice(list(GOP_CHOICES))
        plan.bitrate = self.rng.choice(list(BITRATE_CHOICES))
        plan.subtitle_style = self.seed % 3
        # 7. 移动水印（2026-10-02 调研：实战标配，破画面指纹）
        #    轻/中/强都有，文字按 seed 从候选池轮换
        wm_pool = ["看评论区", "点击头像", "进直播间", "限时优惠", "点赞关注"]
        if self.intensity == "轻":
            plan.watermark_text = ""  # 轻度不加，保干净
        else:
            plan.watermark_text = self.rng.choice(wm_pool)
        return plan

    # -- 时长对齐 ----------------------------------------------------------

    @staticmethod
    def _align_durations(durations: list[float], speeds: list[float],
                         target: float) -> tuple[list[float], list[float]]:
        """时长对齐：把各段变速（0.9~1.1，钩子恒 1.0）微调，使总和≈target。

        按时长从大到小轮流调整中间段，直到误差 <0.05s。
        残余误差由渲染阶段的 tpad/-t 兜底。
        返回 (每段对齐后时长, 每段最终变速)。
        """
        new_speeds = list(speeds)
        n = len(durations)
        if n < 2:
            ds = [d / s for d, s in zip(durations, new_speeds)]
            return [round(d, 2) for d in ds], new_speeds
        # 可调段：跳过钩子（下标0），按时长从大到小
        order = sorted(range(1, n), key=lambda i: durations[i], reverse=True)
        for _ in range(3):  # 最多 3 轮
            total = sum(durations[i] / new_speeds[i] for i in range(n))
            diff = total - target
            if abs(diff) < 0.05:
                break
            for idx in order:
                actual_i = durations[idx] / new_speeds[idx]
                if diff > 0:
                    # 加速：s -> 1.1，最多减少这么多
                    max_reduce = actual_i - durations[idx] / 1.1
                    reduce = min(diff, max_reduce)
                    if reduce > 0.01:
                        new_speeds[idx] = round(
                            durations[idx] / (actual_i - reduce), 3)
                        diff -= reduce
                else:
                    # 减速：s -> 0.9，最多增加这么多
                    max_add = durations[idx] / 0.9 - actual_i
                    add = min(-diff, max_add)
                    if add > 0.01:
                        new_speeds[idx] = round(
                            durations[idx] / (actual_i + add), 3)
                        diff += add
                if abs(diff) < 0.05:
                    break
        final = [round(durations[i] / new_speeds[i], 2) for i in range(n)]
        return final, new_speeds

    # -- 单片段变换 / 特效 / 转场 ------------------------------------------

    def _plan_segment(self, clip: ClipInfo, is_hook: bool,
                      out_duration: float, speed: float) -> SegmentPlan:
        seg = SegmentPlan(clip=clip, is_hook=is_hook,
                          out_duration=out_duration, speed=speed)
        r = self.rng
        if is_hook:
            # 钩子：只做裁剪位移/色调二选一，保护钩子效果
            if r.random() < 0.5:
                seg.crop_shift = (round(r.uniform(0.0, 1.0), 3),
                                  round(r.uniform(0.0, 1.0), 3))
            else:
                seg.tone = self._rand_tone()
            seg.mirror = False
        else:
            # 辅助变换：按 seed 随机组合
            if r.random() < 0.7:
                seg.crop_shift = (round(r.uniform(0.0, 1.0), 3),
                                  round(r.uniform(0.0, 1.0), 3))
            if r.random() < 0.6:
                seg.tone = self._rand_tone()
            # 镜像：仅强度"强"时 30% 概率（有烧录字幕风险，保守）
            seg.mirror = (self.intensity == "强" and r.random() < 0.3)
        # 特效层（最高优先级）：时域噪点为主力，按 seed 轮换变体
        lo, hi = NOISE_RANGE[self.intensity]
        seg.noise_n = r.randint(lo, hi)
        seg.noise_variant = r.randint(0, 2)
        # 去重增强（2026-10-02 调研：实战验证有效的手法）
        # 1.03x 放大：轻 50% / 中 80% / 强 100%
        p_zoom = {"轻": 0.5, "中": 0.8, "强": 1.0}[self.intensity]
        seg.zoom_103 = r.random() < p_zoom
        # 抽帧：中 50% / 强 80%，轻不做（保流畅）
        p_drop = {"轻": 0.0, "中": 0.5, "强": 0.8}[self.intensity]
        seg.drop_frames = (not is_hook) and r.random() < p_drop
        # 横屏模糊填充：按源宽高比自动判定（渲染时 probe）
        seg.blur_bg = False  # 在 segment_filter 里按实际尺寸决定
        # 底部字幕带模糊：强 60%（盖源片烧录字幕），钩子不做
        # 与 crop_shift/zoom_103 互斥（滤镜链简化）
        seg.blur_sub_band = (not is_hook) and self.intensity in ("中", "强") \
            and r.random() < 0.8
        if seg.blur_sub_band:
            seg.crop_shift = None
            seg.zoom_103 = False
        # 暗角（§3.4 特效层）：中 30% / 强 50%，轻微不影响观看
        p_vig = {"轻": 0.0, "中": 0.3, "强": 0.5}[self.intensity]
        seg.vignette = r.random() < p_vig
        # P1/P2 开源调研补齐（2026-10-02）：色彩微调/帧混合/畸变/锐度
        # 色彩微调：中 40% / 强 70%，轻不做
        p_color = {"轻": 0.0, "中": 0.4, "强": 0.7}[self.intensity]
        seg.color_tweak = (not is_hook) and r.random() < p_color
        # 相邻帧混合：中 30% / 强 50%（轻微拖影，钩子不做）
        p_blend = {"轻": 0.0, "中": 0.3, "强": 0.5}[self.intensity]
        seg.frame_blend = (not is_hook) and r.random() < p_blend
        # 边缘畸变+锐度：仅强 30%
        p_lens = {"轻": 0.0, "中": 0.0, "强": 0.3}[self.intensity]
        seg.lens_distort = (not is_hook) and r.random() < p_lens
        seg.sharp_tweak = (not is_hook) and r.random() < p_lens
        # 转场轮换
        seg.transition = r.choice(["hardcut", "hardcut", "fade", "zoom"])
        return seg

    def _rand_tone(self) -> tuple[float, float, float]:
        r = self.rng
        return (round(r.uniform(-0.05, 0.05), 3),   # 亮度
                round(r.uniform(-0.05, 0.05), 3),   # 饱和度
                round(r.uniform(-0.05, 0.05), 3))   # 对比度


# ---------------------------------------------------------------------------
# 滤镜链构建
# ---------------------------------------------------------------------------

def segment_filter(seg: SegmentPlan, idx: int,
                   w: int = MIX_WIDTH, h: int = MIX_HEIGHT,
                   fps: int = MIX_FPS) -> str:
    """单个片段的视频滤镜链（不含音频；素材原声默认关闭）。"""
    f: list[str] = []
    # 0. 横屏模糊填充（横屏源转竖屏）：模糊背景 + 居中原画面
    #    注：blur_bg 在调用前由 probe 判定，此处只拼滤镜
    if seg.blur_bg:
        # split 出两路：一路做模糊背景，一路做前景
        # （此分支需特殊处理，见 _segment_filter_blur_bg）
        return _segment_filter_blur_bg(seg, idx, w, h, fps)
    # 1. 裁剪位移 ±3~5%（先裁后放大回规格，改变像素指纹）
    if seg.crop_shift:
        xr, yr = seg.crop_shift
        cw, chh = 0.95, 0.95
        f.append(
            f"crop=w='iw*{cw}':h='ih*{chh}':"
            f"x='(iw-ow)*{xr:.3f}':y='(ih-oh)*{yr:.3f}'"
        )
    # 1b. 1.03x 中心放大（实战验证：破抽帧指纹，裁掉边缘水印/字幕带）
    # 先缩放到目标尺寸再放大裁剪，避免小分辨率源越界
    if seg.zoom_103:
        f.append(f"scale={w}:{h}:flags=lanczos,"
                 f"scale=iw*1.03:ih*1.03,"
                 f"crop={w}:{h}:(in_w-{w})/2:(in_h-{h})/2")
    # 2. 变速（钩子恒 1.0）
    if abs(seg.speed - 1.0) > 1e-6:
        f.append(f"setpts=PTS/{seg.speed:.3f}")
    # 2b. 抽帧：每 30 帧丢 1 帧（破帧级指纹，观感几乎无影响）
    if seg.drop_frames:
        f.append(r"select='not(mod(n\,30))',setpts=N/FRAME_RATE/TB")
    # 3. 色调 ±5%
    if seg.tone:
        b, s, c = seg.tone
        f.append(f"eq=brightness={b:.3f}:saturation={1 + s:.3f}:contrast={1 + c:.3f}")
    # 3b. P1 色彩空间微调（2026-10-02 开源调研）：比色调更细，
    #     色相/饱和度轻微偏移 + RGB 通道偏移，按 seed 随机
    #     只在中/强强度启用，轻度保持干净
    if getattr(seg, 'color_tweak', False):
        import random as _r
        _rr = _r.Random(hash((seg.clip.name, "color")) % (2**32))
        hue_h = round(_rr.uniform(-2, 2), 2)
        hue_s = round(_rr.uniform(0.97, 1.03), 3)
        rs = round(_rr.uniform(-0.02, 0.02), 3)
        gs = round(_rr.uniform(-0.02, 0.02), 3)
        bs = round(_rr.uniform(-0.02, 0.02), 3)
        f.append(f"hue=h={hue_h}:s={hue_s}")
        f.append(f"colorbalance=rs={rs}:gs={gs}:bs={bs}")
    # 4. 镜像
    if seg.mirror:
        f.append("hflip")
    # 5. 特效层：时域噪点（破坏暗水印提取的主力）
    #    三个变体按 seed 轮换，保证同一源切片在不同成片里噪点 pattern 不同
    n = seg.noise_n
    if seg.noise_variant == 0:
        f.append(f"noise=alls={n}:allf=t")
    elif seg.noise_variant == 1:
        f.append(f"noise=alls={n}:allf=a+t")
    else:
        f.append(f"noise=alls={n}:allf=t+u")
    # 5c. 暗角（§3.4）：轻微，PI/5 强度
    if seg.vignette:
        f.append("vignette=PI/5")
    # 5d. P1 相邻帧混合（2026-10-02 开源调研）：tblend 平均相邻帧，
    #     改变帧间关系，对抗抽帧哈希。轻微拖影，强度可控
    if seg.frame_blend:
        f.append("tblend=all_mode=average")
    # 5e. P2 边缘畸变+锐度微调（2026-10-02 开源调研）：
    #     极轻微镜头畸变对抗几何特征比对，锐度微调改变锐度指纹
    if seg.lens_distort:
        f.append("lenscorrection=k1=0.01:k2=0.01")
    if seg.sharp_tweak:
        import random as _r2
        _rr2 = _r2.Random(hash((seg.clip.name, "sharp")) % (2**32))
        # 随机轻微锐化或柔化
        amount = round(_rr2.uniform(-0.3, 0.5), 2)
        if amount >= 0:
            f.append(f"unsharp=5:5:{amount}")
        else:
            f.append(f"unsharp=5:5:{amount}")  # 负值=柔化
    # 5b. 底部字幕带模糊（盖掉源片烧录字幕，防 OCR 查重）
    #     注：与 crop_shift/zoom_103 互斥，见 _segment_filter_sub_blur
    if seg.blur_sub_band:
        return _segment_filter_sub_blur(seg, idx, w, h, fps, f)
    # 6. 转场（0.25s 淡入淡出；0.5s 会闪黑屏）
    d = max(seg.out_duration, 0.6)
    if seg.transition == "fade":
        f.append(f"fade=t=in:st=0:d=0.25,fade=t=out:st={d - 0.25:.2f}:d=0.25")
    elif seg.transition == "zoom":
        # 轻微推进缩放（整段 6% 放大），居中裁剪回规格
        # 先缩放到目标尺寸再放大，避免小分辨率源越界
        f.append(f"scale={w}:{h}:flags=lanczos,"
                 f"scale=iw*1.06:ih*1.06,crop={w}:{h}:(in_w-{w})/2:(in_h-{h})/2")
    # 7. 规格归一
    f.append(f"scale={w}:{h}:flags=lanczos,setsar=1,fps={fps}")
    return f"[{idx}:v]" + ",".join(f) + f"[v{idx}]"


def extract_cover_frame(video_path: Path, out_path: Path,
                        seed: int | None = None) -> Path:
    """从成片随机抽一帧做封面（2026-10-02 用户确认：首帧从视频里提取，随机）。
    
    用作视频封面/缩略图，改变首帧哈希。seed 不同则抽不同帧。
    """
    import random as _r
    import subprocess
    from .media import probe
    info = probe(video_path)
    dur = max(info.duration, 1.0)
    rng = _r.Random(seed) if seed is not None else _r.Random()
    # 避开前 0.5s（可能黑场）和最后 0.5s，随机取一帧
    t = round(rng.uniform(0.5, max(0.6, dur - 0.5)), 2)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-ss", f"{t:.2f}", "-i", str(video_path),
         "-frames:v", "1", "-q:v", "2", str(out_path)],
        check=True)
    return out_path


def _segment_filter_sub_blur(seg: SegmentPlan, idx: int, w: int, h: int,
                             fps: int, prefix_filters: list[str]) -> str:
    """底部 18% 字幕带模糊。prefix_filters 是已拼好的前置滤镜（噪点等）。"""
    pre = ",".join(prefix_filters)
    # 先走前置滤镜，再 split 做字幕带模糊，最后规格归一
    return (
        f"[{idx}:v]{pre},split=2[s1{idx}][s2{idx}];"
        f"[s1{idx}]crop=iw:ih*0.82:0:0[top{idx}];"
        f"[s2{idx}]crop=iw:ih*0.18:0:ih*0.82,"
        f"boxblur=luma_radius=20:luma_power=2[bot{idx}];"
        f"[top{idx}][bot{idx}]overlay=0:H,"
        f"scale={w}:{h}:flags=lanczos,setsar=1,fps={fps}[v{idx}]"
    )


def _segment_filter_blur_bg(seg: SegmentPlan, idx: int,
                            w: int, h: int, fps: int) -> str:
    """横屏源 -> 模糊背景填充转竖屏。"""
    # 背景：放大铺满 + 高斯模糊 + 稍暗
    # 前景：按高度缩放，居中叠加
    return (
        f"[{idx}:v]split=2[bg{idx}][fg{idx}];"
        f"[bg{idx}]scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h},gblur=sigma=40,eq=brightness=-0.15[bgb{idx}];"
        f"[fg{idx}]scale=-2:{int(h*0.75)}[fgs{idx}];"
        f"[bgb{idx}][fgs{idx}]overlay=(W-w)/2:(H-h)/2,"
        f"scale={w}:{h}:flags=lanczos,setsar=1,fps={fps}[v{idx}]"
    )


def build_filtergraph(plan: MixPlan) -> tuple[str, bool]:
    """拼接全部片段。返回 (filter_complex, 需要 tpad 补齐)。"""
    w, h, fps = plan.width, plan.height, plan.fps
    parts = [segment_filter(s, i, w, h, fps) for i, s in enumerate(plan.segments)]
    ins = "".join(f"[v{i}]" for i in range(len(plan.segments)))
    parts.append(f"{ins}concat=n={len(plan.segments)}:v=1:a=0[vcat]")
    need_pad = plan.video_duration < plan.target_duration - 0.05
    if need_pad:
        gap = round(plan.target_duration - plan.video_duration, 2)
        parts.append(f"[vcat]tpad=stop=-1:stop_duration={gap}:stop_mode=clone[vout]")
    else:
        parts.append("[vcat]null[vout]")
    return ";".join(parts), need_pad


def render(plan: MixPlan, audio_path: Path, out_path: Path,
           workdir: Path | None = None,
           ass_path: Path | None = None,
           fonts_dir: Path | None = None,
           pad_bytes: int = 0) -> Path:
    """渲染一条成片。

    ass_path: 字幕文件（.ass），有则烧录到 [vout] 之后。
    fonts_dir: 字幕字体目录。
    pad_bytes: 文件尾追加随机字节数（>0 启用；混剪重编码后 MD5 本就不同，
    此为破文件哈希的可选项）。

    返回输出路径。抛 RuntimeError 表示失败。
    """
    from .subtitles import burn_filter
    segs = plan.segments
    if not segs:
        raise ValueError("空规划，无法渲染")
    for s in segs:
        if not s.clip.local_path or not s.clip.local_path.exists():
            raise RuntimeError(f"切片本地文件缺失: {s.clip.name}")

    fg, _need_pad = build_filtergraph(plan)
    # 移动水印：在 [vout] 之后，字幕之前
    # 用 drawtext 做正弦移动，半透明，不遮挡主体
    vlabel = "[vout]"
    if plan.watermark_text:
        from .subtitles import _escape_drawtext, assets_fonts_dir
        wt = _escape_drawtext(plan.watermark_text)
        # 中文字体：用 assets/fonts 下的字体，否则 drawtext 显示方框
        fontfile = ""
        fd = fonts_dir or assets_fonts_dir()
        for fp in fd.glob("*.ttf"):
            safe_fp = str(fp).replace("'", "\\'")
            fontfile = f":fontfile='{safe_fp}'"
            break
        # x/y 按正弦移动，周期按 seed 变化
        period = 7 + (plan.seed % 5)
        fg += (f";{vlabel}drawtext=text='{wt}'{fontfile}:fontsize=36:"
               f"fontcolor=white@0.7:borderw=2:bordercolor=black@0.5:"
               f"x='(w-text_w)/2+(w/3)*sin(2*PI*t/{period})':"
               f"y='h*0.15+(h/10)*cos(2*PI*t/{period+2})'[vwm]")
        vlabel = "[vwm]"
    # 贴纸叠加：在水印之后、字幕之前（2026-10-02 用户要求混剪加贴纸）
    # 用 -loop 1 额外输入（movie filter 的 loop 会挂起）
    # _has_sticker/_sticker_idx 在后面 cmd 组装时定义，这里先算
    _has_sticker = bool(plan.sticker_path and Path(plan.sticker_path).exists())
    _sticker_idx = len(segs) + 1 if _has_sticker else -1
    if _has_sticker:
        pos_map = {
            "top-left": "30:30",
            "top-right": "W-w-30:30",
            "bottom-left": "30:H-h-30",
            "bottom-right": "W-w-30:H-h-30",
        }
        pos = pos_map.get(plan.sticker_pos, pos_map["top-right"])
        sz = getattr(plan, "sticker_size", 160) or 160
        fg += (f";[{_sticker_idx}:v]scale={sz}:{sz}[st];"
               f"{vlabel}[st]overlay={pos}:shortest=1[vst]")
        vlabel = "[vst]"
    # 字幕烧录：在水印/贴纸之后
    if ass_path and ass_path.exists():
        fg += f";{vlabel}{burn_filter(ass_path, fonts_dir)}[vsub]"
        vlabel = "[vsub]"
    ta = plan.target_duration
    n = len(segs)
    # P0 音频指纹对抗（2026-10-02 开源调研）：音频指纹是查重主力之一，
    # 加轻微变调+混响+EQ+重采样，人耳几乎无感但指纹失配。参数按 seed 变化。
    rng = random.Random(plan.seed * 31 + 7)
    pitch = round(rng.uniform(0.99, 1.01), 4)  # ±1% 变调
    eq_f = rng.choice([800, 1000, 1200, 1500])  # EQ 中心频率
    eq_g = round(rng.uniform(-2, 2), 1)  # EQ 增益 dB
    # aecho 参数：in_gain:out_gain:delay:decay，轻微空间感
    af = (f"asetrate=44100*{pitch},aresample=44100,"
          f"aecho=0.8:0.88:60:0.4,"
          f"equalizer=f={eq_f}:t=q:w=1:g={eq_g},"
          f"aresample=48000")
    fg += f";[{n}:a:0]{af}[aout]"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for s in segs:
        cmd += ["-i", str(s.clip.local_path)]
    cmd += ["-i", str(audio_path)]
    # 贴纸用 -loop 1 做额外输入（movie filter 的 loop 会挂起，改用输入方式）
    # _has_sticker/_sticker_idx 已在 filter 构建时算好
    if _has_sticker:
        cmd += ["-loop", "1", "-i", str(plan.sticker_path)]
    cmd += ["-filter_complex", fg,
            "-map", vlabel, "-map", "[aout]",
            "-t", f"{ta:.2f}",
            "-c:v", "libx264", "-preset", "medium",
            "-g", str(plan.gop), "-b:v", plan.bitrate,
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k",
            "-map_metadata", "-1",  # P3: 清洗元数据，防剪辑软件指纹残留
            "-movflags", "+faststart",
            str(out_path)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if r.returncode != 0 or not out_path.exists():
        raise RuntimeError(f"混剪渲染失败: {r.stderr.strip()[:500]}")
    if pad_bytes > 0:
        import random as _random
        with open(out_path, "ab") as f:
            f.write(_random.randbytes(pad_bytes))
    return out_path


# ---------------------------------------------------------------------------
# 切片库加载（解析各分类 00-索引.md）
# ---------------------------------------------------------------------------

def has_text_overlay(seg: SegmentPlan) -> bool:
    """检测切片是否有烧录文字/字幕（§3.2 禁忌：有文字的不镜像）。

    方法：抽中间帧，检测底部 25% 区域的边缘密度。
    文字区域边缘密集，无文字的自然画面边缘稀疏。
    """
    from .media import probe
    import subprocess
    import tempfile
    p = seg.clip.local_path
    if not p or not p.exists():
        return True  # 保守：未知则视为有文字
    try:
        info = probe(p)
        mid = info.duration / 2
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
            frame_path = tf.name
        # 截底部 25% 区域
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-ss", str(mid), "-i", str(p), "-frames:v", "1",
             "-vf", "crop=in_w:in_h*0.25:0:in_h*0.75",
             frame_path],
            check=True, timeout=30)
        # 用边缘检测计算边缘像素比例
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf2:
            edge_path = tf2.name
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-i", frame_path, "-vf", "edgedetect=d_0=0.1:d_1=0.4",
             "-frames:v", "1", edge_path],
            check=True, timeout=30)
        # 统计非黑像素比例（用 signalstats）
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-i", edge_path, "-vf", "signalstats",
             "-frames:v", "1", "-f", "null", "-"],
            capture_output=True, text=True, timeout=30)
        import os
        os.unlink(frame_path)
        os.unlink(edge_path)
        # 从 stderr 解析 YAVG（亮度均值，边缘越多越亮）
        import re
        m = re.search(r"YAVG:(\d+\.?\d*)", r.stderr)
        if m:
            yavg = float(m.group(1))
            return yavg > 8.0  # 阈值：有文字时边缘亮度明显高
        return True
    except Exception:
        return True  # 出错时保守处理


def detect_blur_bg(seg: SegmentPlan) -> None:
    """探测切片宽高比，横屏（宽>高）则启用模糊背景填充。

    在下载完成后、渲染前调用。
    """
    from .media import probe
    p = seg.clip.local_path
    if not p or not p.exists():
        return
    try:
        info = probe(p)
        if info.width > info.height:
            seg.blur_bg = True
    except Exception:
        pass


def parse_slice_index(text: str) -> list[dict]:
    """解析 `- name | 6.0s | 开头 | speech` 行。"""
    out = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s.startswith("- "):
            continue
        cells = [c.strip() for c in s[2:].split("|")]
        if len(cells) < 2 or not cells[0].endswith(".mp4"):
            continue
        try:
            dur = float(cells[1].lower().replace("s", "").strip())
        except ValueError:
            continue
        out.append({"name": cells[0], "duration": dur,
                    "speech": cells[3] if len(cells) > 3 else ""})
    return out


def load_library(client, layout, categories: list[str] | None = None
                ) -> tuple[dict[str, list[ClipInfo]], dict[str, str]]:
    """从网盘切片库加载候选。返回 (分类->ClipInfo, 文件名->file_id)。"""
    from .drive import DriveClient  # noqa: F401 (类型提示用)
    cats = categories or CATEGORIES
    granule = client.resolve_path(layout.project_root_id, layout.source_root_name)
    slices = client.find_child(granule.id, layout.slices_dir_name)
    if not slices:
        raise RuntimeError("网盘没有 切片库 目录")
    library: dict[str, list[ClipInfo]] = {}
    name_to_id: dict[str, str] = {}
    for cat in cats:
        folder = client.find_child(slices.id, cat)
        if not folder:
            continue
        files = {f.name: f for f in client.list_folder(folder.id) if f.is_video}
        idx = client.find_child(folder.id, "00-索引.md")
        entries = []
        if idx:
            tmp = Path(f"/tmp/idx_{cat}.md")
            client.download(idx, tmp)
            entries = parse_slice_index(tmp.read_text(encoding="utf-8"))
            tmp.unlink(missing_ok=True)
        clips = []
        for e in entries:
            f = files.get(e["name"])
            if not f:
                continue
            clips.append(ClipInfo(file_id=f.id, name=f.name, category=cat,
                                  duration=e["duration"], speech=e["speech"]))
            name_to_id[f.name] = f.id
        # 索引缺失的条目也兜底加入（用 ffprobe 太慢，跳过）
        library[cat] = clips
    return library, name_to_id
