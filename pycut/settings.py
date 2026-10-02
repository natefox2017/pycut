"""从 config.yaml 读取默认配置。

用户 2026-10-02 要求：默认配置以 yml 放 git，程序从配置文件读取。
"""
from pathlib import Path
import yaml

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"

_cache = None


def load() -> dict:
    """加载 config.yaml，返回配置字典（带缓存）。"""
    global _cache
    if _cache is None:
        if not _CONFIG_PATH.exists():
            return {}
        _cache = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    return _cache


def get(*keys, default=None):
    """按路径取值，如 get("mix", "target_duration")。"""
    cfg = load()
    for k in keys:
        if not isinstance(cfg, dict):
            return default
        cfg = cfg.get(k)
        if cfg is None:
            return default
    return cfg


def reload():
    """清缓存，下次 load 重新读文件。"""
    global _cache
    _cache = None


# 强度数字映射：1=轻, 2=中, 3=强（配置用数字，代码内部用中文）
INTENSITY_MAP = {1: "轻", 2: "中", 3: "强"}


def get_intensity() -> str:
    """从配置读取强度（数字），映射为中文供内部使用。"""
    v = get("mix", "intensity", default=2)
    if isinstance(v, int):
        return INTENSITY_MAP.get(v, "中")
    # 兼容旧的中文值
    return v if v in ("轻", "中", "强") else "中"
