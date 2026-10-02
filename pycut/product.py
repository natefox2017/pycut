"""产品信息表：话术生成的唯一事实来源。

文件格式为受限 YAML 子集（无第三方依赖即可解析）：
    key: value          # 标量
    key:                # 列表
      - item1
      - item2
    # 注释

用户通过改 product.yaml（仓库一份，网盘一份）来告诉 Arlo 产品的核心特点。
（Arlo = 用户的 AI 助手 Muse，负责写话术。）
话术生成/转录后必须经过 check_script() 事实核对，违规项需人工修正。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ProductInfo:
    name: str = ""
    appearance: str = ""
    price: str = ""
    features: list[str] = field(default_factory=list)
    forbidden: list[str] = field(default_factory=list)
    note: str = ""


def _parse_simple_yaml(text: str) -> dict:
    """只解析本表使用的子集：注释、key: value、key: 下的 - item 列表。"""
    data: dict = {}
    cur_key: str | None = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if line.startswith((" ", "\t")) and cur_key:
            s = line.strip()
            if s.startswith("- "):
                data.setdefault(cur_key, []).append(
                    s[2:].strip().strip("'\""))
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            k, v = k.strip(), v.strip().strip("'\"")
            cur_key = k
            if v == "":
                data[k] = []          # 后续缩进 - item 归入此列表
            elif v == "[]":
                data[k] = []
                cur_key = None
            else:
                data[k] = v
                cur_key = None
    return data


def load_product(path: Path) -> ProductInfo:
    data = _parse_simple_yaml(path.read_text(encoding="utf-8"))
    feats = data.get("features") or []
    forb = data.get("forbidden") or []
    return ProductInfo(
        name=str(data.get("name") or ""),
        appearance=str(data.get("appearance") or ""),
        price=str(data.get("price") or ""),
        features=[str(x) for x in feats],
        forbidden=[str(x) for x in forb],
        note=str(data.get("note") or ""),
    )


def brief_text(info: ProductInfo) -> str:
    """生成给话术撰写/转录整理的产品简报。"""
    L = [f"产品：{info.name}", f"外观：{info.appearance}",
         f"价格：{info.price}", "核心特点："]
    L += [f"  - {f}" for f in info.features]
    if info.forbidden:
        L.append("禁止说法：")
        L += [f"  - {f}" for f in info.forbidden]
    if info.note:
        L.append(f"注意：{info.note}")
    return "\n".join(L)


def check_script(info: ProductInfo, script: str) -> list[str]:
    """事实核对：检查话术是否与产品信息表冲突。返回违规描述列表（空=通过）。

    保守策略：只拦明确冲突，不拦未提及。人工终审仍必要。
    """
    bad: list[str] = []
    s = script
    # 1. 禁止说法
    for fb in info.forbidden:
        if fb and fb in s:
            bad.append(f"含禁止说法: {fb}")
    # 2. 价格：话术中出现"数字+块/元"时，数字须与表一致
    price_nums = set(re.findall(r"(\d+)\s*[块元]", s))
    table_nums = set(re.findall(r"\d+", info.price))
    if price_nums and not price_nums <= table_nums:
        bad.append(f"价格数字 {sorted(price_nums)} 与产品表 {info.price} 不一致")
    # 3. 外观颜色：出现颜色词时须与表一致
    colors = set(re.findall(r"[红黄蓝绿黑白紫灰棕]", s))
    if colors:
        app_colors = set(re.findall(r"[红黄蓝绿黑白紫灰棕]", info.appearance))
        if not colors <= app_colors:
            bad.append(f"颜色 {sorted(colors)} 与产品外观 {info.appearance} 不一致")
    return bad
