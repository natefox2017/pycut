# PyCut

[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![FFmpeg 8](https://img.shields.io/badge/ffmpeg-8-green.svg)](https://ffmpeg.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[English](README.en.md) | 中文

云端视频切片与混剪管线：把长视频批量整理成分类切片库，再用切片批量混剪成片。
Python + FFmpeg，纯命令行，无图形界面。

## 功能

| 功能 | 输入 | 输出 | 状态 |
|---|---|---|---|
| **整理切片** | 网盘长视频目录 | 网盘 `切片库/` 下按分类归档的 2~8s 切片 | ✅ 可用 |
| 混剪成片 | `切片库/` + 话术 | 网盘 `成片/` 下的成品视频 | 🚧 设计中（见 `docs/06`） |
| 整理视频 | 网盘目录 | 去重、清系统垃圾（进回收站） | ✅ 可用 |
| 话术管线 | 爆款视频 / 产品信息 | 任务单表格中的话术 | ✅ 可用 |

核心特性：

- **AI 分析**：场景初分段 → faster-whisper 转录取句子时间戳 → 切点对齐到句尾（有话术必须说完一句）→ AI 看帧定分类
- **断点续跑**：网盘台账（路径 + MD5）记录进度，已处理的不重复下载
- **分批处理**：约 1G/批，下载 → 处理 → 上传 → 删本地 → 下一批
- **任务单关口**：每批处理前生成 xlsx 任务单，用户填写确认后才开工

## 运行环境

> **当前仅支持 Muse 云电脑**（muse.ai 运行时）：Python 3.12、FFmpeg 8、
> Google Drive 经 `hatch_gws_cli` 接入（认证由运行时托管，开箱即用）。
>
> 路线图：后续支持其他云电脑与本机运行（`drive.py` 将抽象为可插拔后端）。

## 快速开始（Muse 云电脑）

```bash
cd ~/workspace/pycut

# 1. 扫描网盘目录（只读）
python -m pycut scan --path 颗粒/爆款素材/抖音

# 2. 生成任务单 → 用户填写 → 校验
python -m pycut taskform new --path 颗粒 --name 任务单-2026-10-01.xlsx
python -m pycut taskform read --path 颗粒 --name 任务单-2026-10-01.xlsx

# 3. AI 分析：生成证据包（含转录句子时间戳）
python -m pycut analyze --path 颗粒/爆款素材/抖音 --limit 2

# 4. 填写 run/decisions/<md5>.json（切点+分类+理由）后执行切片
python -m pycut slice --path 颗粒/爆款素材/抖音 --decisions run/decisions

# 5. 短视频直接入库 / 整理视频
python -m pycut import-shorts --path 颗粒/片段/片段手机录制
python -m pycut organize --path 颗粒 --dry-run
```

完整命令与流程见 [`docs/05-运行手册.md`](docs/05-运行手册.md)，
云电脑搭建与恢复见 [`docs/07-云电脑运行指南.md`](docs/07-云电脑运行指南.md)。

## 文档

| 文档 | 内容 |
|---|---|
| [00-需求说明](docs/00-需求说明.md) | 背景、功能需求、约束、范围 |
| [01-处理流程](docs/01-处理流程.md) | 两个功能的完整处理流程 |
| [02-文件夹命名规范](docs/02-文件夹命名规范.md) | 网盘与本地的文件夹、文件名规范 |
| [03-开工前准备清单](docs/03-开工前准备清单.md) | 开工前必须确认的所有事项 |
| [04-目录作用确认表](docs/04-目录作用确认表.md) | 网盘目录作用确认模板表 |
| [05-运行手册](docs/05-运行手册.md) | 全部命令与标准作业流程 |
| [06-混剪详细设计](docs/06-混剪详细设计.md) | 混剪：去重机制、时长对齐、字幕样式 |
| [07-云电脑运行指南](docs/07-云电脑运行指南.md) | 环境搭建、换机恢复、常见问题 |

## 模块结构

```
pycut/
├── cli.py        # 命令行入口（任务单/切片/转录/话术/清理…）
├── config.py     # 切片规则、网盘目录布局
├── drive.py      # Google Drive 客户端（hatch_gws_cli，云电脑专用）
├── ledger.py     # 网盘进度台账（MD5 去重、断点续跑）
├── media.py      # ffprobe/场景检测/抽帧/音量
├── slicer.py     # 切点规划（句子对齐）+ 导出 + QC
├── speech.py     # faster-whisper 转录（独立 venv）
├── analyze.py    # AI 证据包 + 决策校验
├── categorize.py # 切片分类
├── pipeline.py   # 分批流水线（下载→处理→上传→台账→清理）
├── clean.py      # 整理视频（去重/清垃圾）
├── product.py    # 产品信息表 + 话术事实核对
├── voice.py      # 话术转录（字幕帧）
├── scriptgen.py  # 话术生成（双模式）
├── hooks.py      # 爆款开头池
└── taskform.py   # 任务单生成/校验
```

## 与 EasyCut 桌面版的关系

切片流程语义与 [easycut](https://github.com/natefox2017/easycut) 仓库
`docs/SLICE-PREPARATION.md` 一致（扫描 → 校验 → 查记忆 → 分析 → 计划 → 导出 → 验收），
执行层由"桌面 GUI + 本机"替换为"Muse 云电脑命令行"。两仓库无代码共享。

已确认的差异：**<10 秒的视频不切片**；完成记忆用网盘 `EasyCut-处理进度.md`（路径 + MD5）代替 sqlite。

## License

MIT，见 [LICENSE](LICENSE)。
