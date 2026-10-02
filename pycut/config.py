"""全局配置：切片规则、分类体系、网盘目录布局。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SliceRules:
    """功能一切片规则（用户已确认）。"""

    skip_below_seconds: float = 10.0   # <10s 的视频不切片，直接跳过
    min_slice_seconds: float = 2.0     # 切片最小时长
    max_slice_seconds: float = 8.0     # 切片最大时长
    scene_threshold: float = 0.35      # 场景检测阈值
    width: int = 1080                  # 输出宽（9:16）
    height: int = 1920                 # 输出高
    fps: int = 30                      # 输出帧率
    crf: int = 20                      # x264 质量
    preset: str = "veryfast"


#: 切片分类体系（2026-10-01 用户确认 12 细分类）
#: 开头: 开头钩子/人设备书；中段: 痛点描述/旧法批判/产品介绍/原理说明/
#: 使用方法/使用场景/效果承诺/结果证明/互动引导；结尾: 结尾转化
CATEGORIES = [
    "开头钩子", "人设备书",
    "痛点描述", "旧法批判", "产品介绍", "原理说明",
    "使用方法", "使用场景", "效果承诺", "结果证明", "互动引导",
    "结尾转化",
]

#: 开头/中段/结尾位置映射（用于索引标注）
CATEGORY_POSITION = {
    "开头钩子": "开头", "人设备书": "开头",
    "痛点描述": "中段", "旧法批判": "中段", "产品介绍": "中段",
    "原理说明": "中段", "使用方法": "中段", "使用场景": "中段",
    "效果承诺": "中段", "结果证明": "中段", "互动引导": "中段",
    "结尾转化": "结尾",
}

#: 无法自动判定时的兜底分类（不进入混剪候选）
REVIEW_CATEGORY = "待复核"


@dataclass
class DriveLayout:
    """网盘目录布局。project_root_id 为用户项目根目录。"""

    project_root_id: str = "1hsTHN6n5rZhIjLdKIigH1-jV1s6gBAu7"
    source_root_name: str = "颗粒"      # 素材/切片库/任务单都在此目录下
    slices_dir_name: str = "切片库"
    outputs_dir_name: str = "成片"
    ledger_name: str = "EasyCut-处理进度.md"
    batch_gb: float = 1.0              # 每批下载量上限（GB）


@dataclass
class SourceSpec:
    """一个待处理源目录的确认信息（对应 docs/04-目录作用确认表）。"""

    folder_id: str
    name: str
    role: str          # slice=待切片长视频 / shorts=短视频直接入库 / assets=图片素材
    note: str = ""


#: 已确认的目录映射（2026-10-01 用户指定），folder_id 在运行时解析
def confirmed_sources() -> list[SourceSpec]:
    return [
        SourceSpec(folder_id="", name="爆款素材/抖音", role="slice",
                   note="用户抖音爆款视频，全部切出片段并按类型归类"),
        SourceSpec(folder_id="", name="爆款素材/快手", role="slice",
                   note="用户快手爆款视频，全部切出片段并按类型归类"),
        SourceSpec(folder_id="", name="片段/片段手机录制", role="shorts",
                   note="手机录制短视频，太短不用切片，直接归档入库"),
        SourceSpec(folder_id="", name="片段/死老鼠", role="shorts",
                   note="单条视频，不用切片，混剪时按需拼接"),
        SourceSpec(folder_id="", name="主图", role="assets",
                   # Arlo = 用户的 AI 助手（Muse），负责在混剪时决定产品图片的拼入时机
                   note="产品图片素材，混剪时由 Arlo 决定拼入时机"),
    ]
