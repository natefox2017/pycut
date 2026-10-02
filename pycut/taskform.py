"""任务单：每批处理前的用户确认关口（用户 2026-10-01 确认流程）。

流程：
1. Arlo 跑 `pycut taskform new`，按当前规划生成 xlsx 任务单并上传到网盘目录。
2. 用户在手机上（Sheets App）填写：处理范围、话术、产品信息确认等，
   填完把"状态"改为"已填写"，再在聊天里告诉 Arlo。
3. Arlo 跑 `pycut taskform read` 下载并校验：
   - 状态不是"已填写" → 拒绝执行；
   - 表内处理范围只认规划内的目录，未知项只警告、不处理；
   - 产品信息覆盖 product.yaml，话术经 product.check_script() 核对。
4. 只执行任务单内的事项。

一句话：表格内容符合规划的都处理，没有的不处理。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font

from .product import ProductInfo, brief_text, load_product

# 规划内的处理范围：只认这些，表里写别的只警告不处理
KNOWN_SCOPES = [
    "颗粒/爆款素材/抖音",
    "颗粒/爆款素材/快手",
    "颗粒/片段/片段手机录制",
    "颗粒/片段/死老鼠",
]

TASK_ROWS = [
    ("处理范围", "颗粒/爆款素材/抖音", "", "只处理表内列出的目录，一行一个"),
    ("长视频切片入库", "是", "", "爆款素材按 AI 决策切片入切片库"),
    ("短视频直接入库", "是", "", "片段类短视频不切割直接入库"),
    ("话术来源", "爆款音频转录", "", "填：转录 / 我提供（见话术表）"),
    ("开头策略", "爆款同款开头", "", "混剪用爆款同款前几秒+轻微去重"),
    ("本批混剪", "否（一期先做切片）", "", "二期功能，暂不执行"),
    # --- 二期：混剪参数 ---
    ("混剪成片数", "5", "", "二期：本批混剪几条成片"),
    ("混剪话术", "", "填话术表序号，一行一个", "二期：用哪几条话术"),
    ("去重强度", "中", "", "二期：轻/中/强"),
    ("混剪规格", "1080x1920", "", "二期：输出分辨率"),
    ("状态", "待填写", "", '填完改为"已填写"，再在聊天里告诉我'),
]

PRODUCT_ROWS = [
    ("产品名", "name", "话术不得虚构此表之外的信息"),
    ("外观", "appearance", ""),
    ("价格", "price", ""),
    ("核心特点", "features", "一行一条"),
    ("禁止说法", "forbidden", "一行一条"),
]


@dataclass
class TaskForm:
    scopes: list[str] = field(default_factory=list)
    do_slice: bool = True
    do_shorts: bool = True
    script_source: str = "转录"
    hook_strategy: str = "爆款同款开头"
    product: ProductInfo = field(default_factory=ProductInfo)
    scripts: list[str] = field(default_factory=list)
    status: str = ""
    warnings: list[str] = field(default_factory=list)
    # --- 二期：混剪参数 ---
    mix_count: int = 5
    mix_script_rows: list[int] = field(default_factory=list)  # 话术表序号
    mix_intensity: str = "中"
    mix_size: str = "1080x1920"

    @property
    def ready(self) -> bool:
        return self.status.strip() in ("已填写", "完成", "OK", "ok")


def _style_header(ws, ncols: int):
    for c in range(1, ncols + 1):
        cell = ws.cell(row=1, column=c)
        cell.font = Font(bold=True)
    widths = [22, 34, 30, 40]
    for i, w in enumerate(widths[:ncols], 1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w


def generate_taskform(path: Path, product: ProductInfo,
                      batch_name: str = "") -> Path:
    """按当前规划生成任务单 xlsx。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "本批任务"
    ws.append(["项", "我的规划", "你填写/确认", "说明"])
    for item, plan, fill, note in TASK_ROWS:
        ws.append([item, plan, fill, note])
    _style_header(ws, 4)

    wp = wb.create_sheet("产品信息")
    wp.append(["字段", "内容", "说明"])
    for label, attr, note in PRODUCT_ROWS:
        v = getattr(product, attr)
        if isinstance(v, list):
            for i, x in enumerate(v):
                wp.append([label if i == 0 else "", x, note if i == 0 else ""])
        else:
            wp.append([label, v, note])
    _style_header(wp, 3)

    wh = wb.create_sheet("话术")
    wh.append(["序号", "话术内容（一行一条）"])
    for i in range(1, 21):
        wh.append([i, ""])
    _style_header(wh, 2)
    wh.column_dimensions["B"].width = 80

    if batch_name:
        ws["B1"] = f"项（{batch_name}）"
    wb.save(path)
    return path


def _cell(ws, r: int, c: int) -> str:
    v = ws.cell(row=r, column=c).value
    return str(v).strip() if v is not None else ""


def read_taskform(path: Path) -> TaskForm:
    """读回任务单并校验。未知项只记警告，不处理。"""
    wb = load_workbook(path, data_only=True)
    form = TaskForm()
    ws = wb["本批任务"]
    answers: dict[str, str] = {}
    for r in range(2, ws.max_row + 1):
        item = _cell(ws, r, 1)
        if item:
            answers[item] = _cell(ws, r, 3) or _cell(ws, r, 2)

    form.status = answers.get("状态", "")
    # 处理范围：只认规划内
    raw_scopes = [x.strip() for x in answers.get("处理范围", "").split("\n")
                  if x.strip()]
    if not raw_scopes:
        raw_scopes = [x.strip() for x in TASK_ROWS[0][1].split("\n")]
    for s in raw_scopes:
        if s in KNOWN_SCOPES:
            form.scopes.append(s)
        else:
            form.warnings.append(f"处理范围不在规划内，已跳过: {s}")
    form.do_slice = answers.get("长视频切片入库", "是").startswith("是")
    form.do_shorts = answers.get("短视频直接入库", "是").startswith("是")
    form.script_source = answers.get("话术来源", "爆款音频转录")
    form.hook_strategy = answers.get("开头策略", "爆款同款开头")

    # 二期：混剪参数
    try:
        form.mix_count = max(1, int(answers.get("混剪成片数", "5")))
    except ValueError:
        form.mix_count = 5
        form.warnings.append("混剪成片数不是数字，已用默认值 5")
    rows: list[int] = []
    for x in answers.get("混剪话术", "").replace("，", ",").split(","):
        x = x.strip()
        if x.isdigit():
            rows.append(int(x))
    form.mix_script_rows = rows
    inten = answers.get("去重强度", "中").strip()
    form.mix_intensity = inten if inten in ("轻", "中", "强") else "中"
    form.mix_size = answers.get("混剪规格", "1080x1920").strip() or "1080x1920"

    # 产品信息表覆盖
    if "产品信息" in wb.sheetnames:
        wp = wb["产品信息"]
        pdata: dict[str, list[str]] = {}
        for r in range(2, wp.max_row + 1):
            k, v = _cell(wp, r, 1), _cell(wp, r, 2)
            if k:
                cur = k
                pdata.setdefault(cur, [])
                if v:
                    pdata[cur].append(v)
            elif v:
                pdata.setdefault(cur, []).append(v)
        prod = ProductInfo(
            name=";".join(pdata.get("产品名", [])),
            appearance=";".join(pdata.get("外观", [])),
            price=";".join(pdata.get("价格", [])),
            features=pdata.get("核心特点", []),
            forbidden=pdata.get("禁止说法", []),
        )
        if prod.name:
            form.product = prod

    # 话术
    if "话术" in wb.sheetnames:
        wh = wb["话术"]
        for r in range(2, wh.max_row + 1):
            v = _cell(wh, r, 2)
            if v:
                form.scripts.append(v)

    return form


def summarize(form: TaskForm) -> str:
    L = ["📋 任务单校验"]
    L.append(f"  状态: {form.status} -> "
             + ("✅ 可执行" if form.ready else "⏸ 未填写完成，拒绝执行"))
    L.append(f"  处理范围: {form.scopes or ['（空）']}")
    L.append(f"  长视频切片: {'是' if form.do_slice else '否'}  "
             f"短视频入库: {'是' if form.do_shorts else '否'}")
    L.append(f"  话术来源: {form.script_source}（{len(form.scripts)} 条）")
    L.append(f"  开头策略: {form.hook_strategy}")
    L.append(f"  混剪: {form.mix_count} 条 × {form.mix_size}，去重{form.mix_intensity}，"
             f"话术行号 {form.mix_script_rows or ['（未填，用全部）']}")
    L.append("  产品信息:\n" + "\n".join("    " + x
             for x in brief_text(form.product).splitlines()))
    for w in form.warnings:
        L.append(f"  ⚠️ {w}")
    return "\n".join(L)
