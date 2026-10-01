"""切片：切点规划 + FFmpeg 导出 + 9:16 标准化。"""
from __future__ import annotations

import subprocess
from pathlib import Path

from .config import SliceRules
from .media import MediaInfo


def plan_cuts(duration: float, scenes: list[float],
              rules: SliceRules) -> list[tuple[float, float]]:
    """根据场景边界规划切点，返回 [(start, end)]（秒）。

    规则：整段保留优先；小段合并；长段在场景边界处拆分；
    尾段重分配（如 9s -> 5+4 而非 8+1）；不机械等分。
    """
    if duration < rules.skip_below_seconds:
        return []

    bounds = sorted({0.0} | {s for s in scenes if 0.5 < s < duration - 0.5}
                    | {duration})
    segs = [(bounds[i], bounds[i + 1])
            for i in range(len(bounds) - 1) if bounds[i + 1] - bounds[i] > 0.3]

    # 1) 合并过小段（< min 并入后一段；末段并入前一段）
    merged: list[list[float]] = [list(s) for s in segs]
    i = 0
    while i < len(merged):
        s, e = merged[i]
        if e - s < rules.min_slice_seconds and len(merged) > 1:
            if i < len(merged) - 1:
                merged[i + 1][0] = s
            else:
                merged[i - 1][1] = e
            merged.pop(i)
            i = max(0, i - 1)
        else:
            i += 1

    # 2) 拆分过大段（> max），优先用内部场景边界，否则均匀拆分并做尾段重分配
    out: list[tuple[float, float]] = []
    for s, e in merged:
        d = e - s
        if d <= rules.max_slice_seconds:
            out.append((s, e))
            continue
        inner = [b for b in bounds if s < b < e]
        if inner:
            pts = [s] + inner + [e]
            # 合并相邻过小子段
            parts: list[list[float]] = [[pts[0], pts[1]]]
            for k in range(1, len(pts) - 1):
                if parts[-1][1] - parts[-1][0] < rules.min_slice_seconds:
                    parts[-1][1] = pts[k + 1]
                else:
                    parts.append([pts[k], pts[k + 1]])
            for ps, pe in parts:
                out.extend(_split_even(ps, pe, rules))
        else:
            out.extend(_split_even(s, e, rules))

    # 3) 最终校验：时长落在 [min, max] 内（允许 0.3s 浮动）
    final = [(round(s, 2), round(e, 2)) for s, e in out
             if e - s >= rules.min_slice_seconds - 0.3]
    return final


# ---------- 句子对齐（用户 2026-10-01 要求：有话术必须说完一句） ----------

def snap_cuts_to_speech(cuts: list[tuple[float, float]],
                        sentences: list[tuple[float, float]],
                        rules: SliceRules,
                        tol: float = 0.15) -> list[tuple[float, float]]:
    """把切点吸附到句子边界：切点不得落在句子内部。

    cuts: 场景初分段 [(s,e)]；sentences: 句子区间 [(s,e)]（STT 转录）。
    返回 [(s,e)]，每段 [min_slice_seconds, max_slice_seconds]，
    切点只落在句子边界或静音区。
    """
    min_d, max_d = rules.min_slice_seconds, rules.max_slice_seconds
    if not cuts:
        return []
    if not sentences:
        return cuts

    # 1) 内部切点吸附：落在句子内的切点移到句子边缘（优先句尾）
    bounds = [cuts[0][0]]
    for i in range(len(cuts) - 1):
        b = cuts[i][1]
        bounds.append(_snap_one(b, bounds[-1], cuts[i + 1][1],
                                sentences, min_d, max_d, tol))
    bounds.append(cuts[-1][1])
    chunks = [(bounds[i], bounds[i + 1])
              for i in range(len(bounds) - 1) if bounds[i + 1] - bounds[i] > 0.3]

    # 2) 拆超长段：在内部句子边界处贪心拆分
    out: list[tuple[float, float]] = []
    for a, b in chunks:
        out.extend(_split_long_at_sentences(a, b, sentences, min_d, max_d))

    # 3) 合并过短段
    merged: list[tuple[float, float]] = []
    for ch in out:
        if merged and (ch[1] - ch[0] < min_d or
                       merged[-1][1] - merged[-1][0] < min_d):
            a0, _ = merged.pop()
            merged.append((a0, ch[1]))
        else:
            merged.append(ch)
    return [(round(s, 2), round(e, 2)) for s, e in merged]


def _snap_one(b: float, prev: float, nxt: float,
              sentences: list[tuple[float, float]],
              min_d: float, max_d: float, tol: float) -> float:
    """单个切点吸附。返回新的切点位置。"""
    for s, e in sentences:
        if s + tol < b < e - tol:
            # 优先移到句尾（左段保留完整句子）
            if e - prev <= max_d and nxt - e >= min_d:
                return e
            # 其次句首
            if s - prev >= min_d and nxt - s <= max_d:
                return s
            return b  # 两边都排不下，保持原样
    return b


def _split_long_at_sentences(a: float, b: float,
                             sentences: list[tuple[float, float]],
                             min_d: float, max_d: float
                             ) -> list[tuple[float, float]]:
    """超长段在句子边界处拆分，保证每段 [min_d, max_d]。"""
    if b - a <= max_d:
        return [(a, b)]
    pts = sorted({x for s, e in sentences for x in (s, e)
                  if a + min_d <= x <= b - min_d})
    res: list[tuple[float, float]] = []
    cur = a
    while b - cur > max_d:
        cands = [x for x in pts if cur + min_d <= x <= cur + max_d]
        nxt = max(cands) if cands else cur + max_d
        res.append((cur, nxt))
        cur = nxt
    # 收尾：最后一段过短则与前一段重新在句子边界处分
    if res and b - cur < min_d:
        p0, _p1 = res.pop()
        cands = [x for x in pts if p0 + min_d <= x <= b - min_d]
        if cands:
            m = max(cands)
            res.append((p0, m))
            res.append((m, b))
        else:
            res.append((p0, b))  # 退化：接受略超/略短
    else:
        res.append((cur, b))
    return res


def _split_even(s: float, e: float, rules: SliceRules) -> list[tuple[float, float]]:
    """无场景边界时的均匀拆分，尾段重分配避免产生过小尾巴。"""
    d = e - s
    n = max(1, int(round(d / rules.max_slice_seconds)))
    # 尾段重分配：若最后一段会 < min，减少段数重分
    while n > 1:
        part = d / n
        if part < rules.min_slice_seconds:
            n -= 1
        else:
            break
    n = max(1, n)
    part = d / n
    # 仍有个别段超 max 时继续细分（极端长段）
    if part > rules.max_slice_seconds * 1.5:
        return _split_even(s, s + d / 2, rules) + _split_even(s + d / 2, e, rules)
    return [(s + i * part, s + (i + 1) * part) for i in range(n)]


def _norm_filter(rules: SliceRules) -> str:
    return (
        f"scale={rules.width}:{rules.height}:force_original_aspect_ratio=increase,"
        f"crop={rules.width}:{rules.height},setsar=1,fps={rules.fps}"
    )


def export_slice(src: Path, start: float, end: float, dst: Path,
                 rules: SliceRules) -> Path:
    """按切点导出并标准化。失败抛 RuntimeError。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    dur = round(end - start, 2)
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(src), "-ss", f"{start:.2f}", "-t", f"{dur:.2f}",
         "-vf", _norm_filter(rules),
         "-c:v", "libx264", "-preset", rules.preset, "-crf", str(rules.crf),
         "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k",
         "-movflags", "+faststart", str(dst)],
        capture_output=True, text=True, timeout=1200,
    )
    if r.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        raise RuntimeError(f"切片导出失败 {src.name} [{start}-{end}]: "
                           f"{r.stderr.strip()[:200]}")
    return dst


def normalize_full(src: Path, dst: Path, rules: SliceRules) -> Path:
    """短视频直接入库：不切割，只做 9:16 标准化。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(src),
         "-vf", _norm_filter(rules),
         "-c:v", "libx264", "-preset", rules.preset, "-crf", str(rules.crf),
         "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k",
         "-movflags", "+faststart", str(dst)],
        capture_output=True, text=True, timeout=1200,
    )
    if r.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        raise RuntimeError(f"标准化失败 {src.name}: {r.stderr.strip()[:200]}")
    return dst


def qc_ok(path: Path, rules: SliceRules,
          expect_dur: float | None = None) -> tuple[bool, str]:
    """质量检查：可解码、画幅正确、时长合理。返回 (ok, reason)。"""
    from .media import probe
    try:
        info: MediaInfo = probe(path)
    except RuntimeError as ex:
        return False, f"probe失败: {ex}"
    if info.duration <= 0:
        return False, "时长为0"
    if info.width != rules.width or info.height != rules.height:
        return False, f"画幅{info.width}x{info.height}不符合"
    if expect_dur and abs(info.duration - expect_dur) > 1.0:
        return False, f"时长{info.duration:.1f}s与预期{expect_dur:.1f}s偏差过大"
    return True, "ok"
