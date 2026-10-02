"""混剪字幕：话术文本 -> .ass -> subtitles 滤镜烧录。

三种样式按 seed 轮换（用户 2026-10-01 确认）：
0. 背景框字幕：半透明黑底框
1. 彩色文字：关键词/价格变色
2. 换字体：不同字体

字体放 assets/fonts/（仓库不存，首次运行下载免费可商用中文字体：
思源黑体 Bold / 站酷快乐体，用户已确认）。
"""
from __future__ import annotations

import re
import subprocess
import urllib.request
from pathlib import Path

#: 默认字体（用户已确认）
FONT_SANS_BOLD = "Noto Sans SC Bold"     # 思源黑体 Bold
FONT_ROUND = "ZhanKu KuaiLe"             # 站酷快乐体

#: 字体下载源（免费可商用）
FONT_URLS = {
    FONT_SANS_BOLD: [
        "https://github.com/google/fonts/raw/main/ofl/notosanssc/NotoSansSC%5Bwght%5D.ttf",
    ],
    FONT_ROUND: [
        "https://github.com/jeffreyxmonteiro/Chinese-Fonts/raw/master/ZhanKuKuaiLeTi/ZhanKuKuaiLe2016XiuDingBan-1.ttf",
    ],
}

ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
"""


def assets_fonts_dir() -> Path:
    d = Path(__file__).resolve().parent.parent / "assets" / "fonts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _system_cjk_font() -> str | None:
    """系统已装的中文字体兜底。"""
    try:
        r = subprocess.run(["fc-list", ":lang=zh", "family"],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in r.stdout.splitlines():
        fam = line.split(",")[0].strip()
        if fam:
            return fam
    return None


def ensure_fonts() -> dict[str, str]:
    """确保字体可用。返回 {逻辑名: 可用字体名}。

    优先下载到 assets/fonts/；下载失败则用系统中文字体兜底。
    """
    fonts_dir = assets_fonts_dir()
    resolved: dict[str, str] = {}
    for logical, urls in FONT_URLS.items():
        safe = re.sub(r"\W+", "_", logical)
        ok = any((fonts_dir / f"{safe}{Path(u).suffix or '.ttf'}").exists()
                 for u in urls)
        if not ok:
            for u in urls:
                try:
                    dst = fonts_dir / f"{safe}{Path(u.split('%5B')[0]).suffix or '.ttf'}"
                    urllib.request.urlretrieve(u, dst)
                    if dst.stat().st_size > 100_000:
                        ok = True
                        break
                    dst.unlink(missing_ok=True)
                except Exception:
                    continue
        resolved[logical] = logical if ok else (_system_cjk_font() or logical)
    return resolved


def split_lines(text: str, max_chars: int = 13) -> list[str]:
    """按标点分句，每行不超过 max_chars 字。"""
    sents = [s for s in re.split(r"([，。！？；、])", text.strip()) if s]
    # 合并标点到前一句
    merged: list[str] = []
    buf = ""
    for s in sents:
        if s in "，。！？；、":
            buf += s
            merged.append(buf)
            buf = ""
        else:
            buf += s
    if buf:
        merged.append(buf)
    # 过长再硬切
    lines: list[str] = []
    for m in merged:
        while len(m) > max_chars:
            lines.append(m[:max_chars])
            m = m[max_chars:]
        if m:
            lines.append(m)
    return [x for x in lines if x.strip()]


def distribute_times(lines: list[str], total: float,
                     ) -> list[tuple[float, float]]:
    """按字数比例把总时长分配给每行。"""
    weights = [max(len(x), 1) for x in lines]
    s = sum(weights)
    out: list[tuple[float, float]] = []
    t = 0.0
    for i, w in enumerate(weights):
        d = total * w / s
        end = total if i == len(lines) - 1 else t + d
        out.append((round(t, 2), round(end, 2)))
        t = end
    return out


def _ass_time(sec: float) -> str:
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _highlight_keywords(line: str) -> str:
    """价格/数字标红（ASS BGR：红色=&H000000FF&）。"""
    def _red(m: re.Match) -> str:
        return r"{\c&H0000FF&}" + m.group(0) + r"{\c&HFFFFFF&}"
    return re.sub(r"\d+\s*[元块包]", _red, line)


def write_ass(text: str, total_duration: float, style_idx: int,
              fonts: dict[str, str], out_path: Path) -> Path:
    """生成 .ass 字幕文件。style_idx: 0=背景框 1=彩色文字 2=换字体。"""
    lines = split_lines(text)
    if not lines:
        raise ValueError("话术文本为空，无法生成字幕")
    times = distribute_times(lines, total_duration)
    font_main = fonts.get(FONT_SANS_BOLD, FONT_SANS_BOLD)
    font_alt = fonts.get(FONT_ROUND, font_main)

    L = [ASS_HEADER.rstrip()]
    if style_idx == 0:
        # 背景框：BorderStyle=3 半透明黑底
        L.append(f"Style: sub,{font_main},64,&H00FFFFFF,&H000019FF,&H00000000,"
                 f"&H80000000,-1,0,0,0,100,100,0,0,3,2,0,2,30,30,120,1")
    elif style_idx == 1:
        # 彩色文字：白字描边，关键词行内标红
        L.append(f"Style: sub,{font_main},64,&H00FFFFFF,&H000019FF,&H80000000,"
                 f"&H00000000,-1,0,0,0,100,100,0,0,1,3,0,2,30,30,120,1")
    else:
        # 换字体：圆体
        L.append(f"Style: sub,{font_alt},66,&H00FFF0E0,&H000019FF,&H80000000,"
                 f"&H00000000,-1,0,0,0,100,100,0,0,1,3,0,2,30,30,120,1")
    L.append("")
    L.append("[Events]")
    L.append("Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
             "MarginV, Effect, Text")
    for (st, en), line in zip(times, lines):
        txt = _highlight_keywords(line) if style_idx == 1 else line
        # ASS 换行符与特殊字符转义
        txt = txt.replace("{", "｛").replace("}", "｝") if style_idx != 1 else txt
        L.append(f"Dialogue: 0,{_ass_time(st)},{_ass_time(en)},sub,,0,0,0,,{txt}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(L) + "\n", encoding="utf-8")
    return out_path


def burn_filter(ass_path: Path, fonts_dir: Path | None = None) -> str:
    """返回可拼进 filter_complex 的 subtitles 滤镜片段。"""
    p = str(ass_path).replace("'", r"'\''").replace(":", r"\:")
    filt = f"subtitles='{p}'"
    if fonts_dir:
        fd = str(fonts_dir).replace("'", r"'\''").replace(":", r"\:")
        filt += f":fontsdir='{fd}'"
    return filt


def _escape_drawtext(s: str) -> str:
    """drawtext 的 text 参数转义。"""
    return (s.replace("\\", r"\\").replace("'", r"\'")
             .replace(":", r"\:").replace("%", r"\%"))
