"""话术生成：独立的执行过程，产出写入任务单"话术"表，混剪时只从表里读。

Arlo = 用户的 AI 助手（Muse）。
两种方式（用户 2026-10-01 确认）：
- 方式一：Arlo 从产品介绍（product.yaml）+ 爆款视频分析（参考话术库）
  总结生成爆款话术。用户指定每条目标秒数，Arlo 按时长生成。
- 方式二：前几秒直接用爆款视频的原声音（extract_opening_audio 提取），
  后面 Arlo 再拼接生成。

流程：scriptgen prepare → Arlo 写话术 → scriptgen write（校验+写入表格）
用户想改，自己去表格里手动编辑。用户可全程不参与。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from openpyxl import load_workbook

from .product import ProductInfo, check_script

# 中文口播语速：约 4.5 字/秒（含标点停顿）
CHARS_PER_SECOND = 4.5


def estimate_duration(script: str) -> float:
    """按字数估算口播时长（秒）。"""
    chars = len(script.strip())
    return round(chars / CHARS_PER_SECOND, 1)


def target_chars(seconds: float) -> int:
    """目标秒数 → 建议字数。"""
    return int(seconds * CHARS_PER_SECOND)


def extract_opening_audio(src: Path, seconds: float, dst: Path) -> Path:
    """方式二：提取爆款视频前 N 秒原音频（开头直接用原声）。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(src), "-t", f"{seconds:.1f}",
         "-vn", "-c:a", "aac", "-b:a", "128k", str(dst)],
        capture_output=True, text=True, timeout=300,
    )
    if r.returncode != 0 or not dst.exists():
        raise RuntimeError(f"开头音频提取失败 {src.name}: {r.stderr[:200]}")
    return dst


def build_prepare_brief(product: ProductInfo, ref_scripts: list[str],
                        seconds: float, count: int) -> str:
    """方式一：给 Arlo 生成话术用的输入包（产品简报+参考话术+目标）。"""
    L = ["# 话术生成输入包（方式一：Arlo 总结生成）",
         "",
         "## 产品信息（事实来源，话术不得虚构表外信息）"]
    L.append(f"产品：{product.name} / 外观：{product.appearance} / "
             f"价格：{product.price}")
    L += [f"- {f}" for f in product.features]
    if product.forbidden:
        L.append("禁止说法：" + "; ".join(product.forbidden))
    L += ["",
          f"## 目标：{count} 条，每条约 {seconds} 秒（约 {target_chars(seconds)} 字）",
          "",
          "## 参考：爆款视频转录话术（学习其话术结构和卖点表达）"]
    for i, s in enumerate(ref_scripts, 1):
        L.append(f"{i}. {s}")
    L += ["",
          "## 要求",
          "- 开头 3 秒必须出钩子（沿用爆款开头的话术风格）；",
          "- 每条话术独立成段，一行一条；",
          f"- 字数控制在 {target_chars(seconds)} 字上下 10%；",
          "- 只写产品信息表里的卖点，不虚构。"]
    return "\n".join(L)


def write_scripts_to_taskform(taskform_path: Path, scripts: list[str],
                              product: ProductInfo) -> list[str]:
    """校验并写入任务单"话术"表。返回被拦截的问题（空=全部通过）。"""
    blocked: list[str] = []
    ok_scripts: list[str] = []
    for s in scripts:
        s = s.strip()
        if not s:
            continue
        bad = check_script(product, s)
        if bad:
            blocked.append(f"「{s[:20]}…」-> {'; '.join(bad)}")
        else:
            ok_scripts.append(s)
    if blocked:
        return blocked
    wb = load_workbook(taskform_path)
    ws = wb["话术"]
    # 清空旧内容（保留表头），写入新话术
    for r in range(2, ws.max_row + 1):
        ws.cell(row=r, column=2).value = ""
    for i, s in enumerate(ok_scripts, 1):
        ws.cell(row=i + 1, column=1).value = i
        ws.cell(row=i + 1, column=2).value = s
        ws.cell(row=i + 1, column=3).value = f"约{estimate_duration(s)}s"
    ws.cell(row=1, column=3).value = "估算时长"
    wb.save(taskform_path)
    return []
