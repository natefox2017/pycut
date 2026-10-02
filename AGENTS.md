# AGENTS.md - PyCut AI 使用手册

> 给 AI 助手看的完整用法文档。PyCut 是云端视频切片与混剪管线，跑在 Hatch 云电脑（Linux）上。

## 一句话定位

把网盘里的长视频批量整理成 2~8s 分类切片（功能一），再用切片+话术批量混剪成去重成片（功能二）。

## 环境要求

- Hatch 云电脑（Linux），`~/workspace/pycut/` 为工作目录
- Python：`~/workspace/pycut/.venv/bin/python`
- FFmpeg 8 可用（`ffmpeg` / `ffprobe` 在 PATH）
- Google Drive：经 `hatch_gws_cli` 访问，运行时已托管认证，开箱即用
- 运行 Drive 相关脚本前：`env -u no_proxy -u NO_PROXY`（去掉代理变量）

## 仓库结构

```
pycut/                  # 主包
  cli.py                # 命令行入口：python -m pycut <命令>
  config.py             # 网盘目录映射、分类体系（12 分类）
  drive.py              # Google Drive 客户端（经 hatch_gws_cli）
  ledger.py             # 网盘台账（EasyCut-处理进度.md）
  media.py              # ffprobe 封装、抽帧
  slicer.py             # 场景切分
  categorize.py         # 启发式分类 + AI 复核入口
  analyze.py            # AI 分析：证据包 → decisions.json → 导出
  pipeline.py           # 功能一切片流水线
  clean.py              # 整理视频：去重、清垃圾
  hooks.py              # 开头池提取
  taskform.py           # 任务单 xlsx 生成/读取/校验
  product.py            # product.yaml 产品信息表
  scriptgen.py          # 话术生成（prepare/write/extract-opening）
  voice.py              # 话术转录（抽字幕帧）
  speech.py             # faster-whisper 转录 + 去重
  subtitles.py          # ASS 字幕生成 + 烧录
  mix.py                # 功能二：混剪规划 + 渲染（核心）
docs/                   # 设计文档
  00-需求说明.md
  06-混剪详细设计.md    # 功能二完整设计（必读）
tests/                  # pytest 单元测试
run/                    # 本地中转区（downloads/outputs/decisions 等）
assets/fonts/           # 字幕字体（首次运行自动下载）
```

## 功能一：整理切片

```bash
# 扫描网盘目录
python -m pycut scan --path 颗粒/爆款素材/抖音

# 生成 AI 证据包（程序分段+抽帧，AI 看帧填 decisions.json）
python -m pycut analyze --path 颗粒/爆款素材/抖音 --limit 10

# 用 AI 决策切片（decisions 目录放 <md5>.json）
python -m pycut slice --path 颗粒/爆款素材/抖音 --decisions run/decisions

# 短视频直接入库（不切片）
python -m pycut import-shorts --path 颗粒/片段/片段手机录制

# 整理视频（去重、清垃圾，进回收站）
python -m pycut organize --path 颗粒 --dry-run
```

输出：网盘 `颗粒/切片库/<分类>/` 下 2~8s 切片 + `00-索引.md`

## 功能二：混剪成片

### 前置：任务单

```bash
# 生成任务单 xlsx 并上传网盘
python -m pycut taskform new --path 颗粒 --name 任务单-2026-10-01.xlsx

# 用户在手机上填完（状态改为"已填写"）后，下载校验
python -m pycut taskform read --path 颗粒/任务单-2026-10-01.xlsx
```

任务单字段：成片数、话术表、去重强度（轻/中/强）、输出规格（1080x1920/720x1280）、状态

### 话术准备（二选一）

```bash
# 方式一：Arlo 按产品信息生成话术
python -m pycut scriptgen prepare --path 颗粒  # 输出输入包，Arlo 写话术
python -m pycut scriptgen write --path 颗粒 --scripts-file 话术.txt  # 校验+写入任务单

# 方式二：提取爆款开头原音频
python -m pycut scriptgen extract-opening --video 颗粒/爆款素材/抖音/xxx.mp4 --seconds 4
# 输出到 颗粒/音频库/
```

### 开头池

```bash
python -m pycut hooks --path 颗粒/爆款素材/抖音 --limit 20
# 输出到 run/hook_pool/hook_*.mp4
```

### 执行混剪

```bash
# 基础（读任务单全部参数）
python -m pycut mix --taskform 颗粒/任务单-2026-10-01.xlsx

# 常用覆盖参数
python -m pycut mix --taskform 颗粒/任务单-2026-10-01.xlsx \
  --count 5 \                    # 成片数
  --intensity 强 \                # 去重强度：轻/中/强
  --size 720x1280 \               # 降级分辨率
  --seed 20261002 \               # 起始 seed（可复现）
  --audio /path/to/话术.mp3 \      # 话术配音（可选）
  --opening-audio /path/to/开头.mp3  # 方式二：开头爆款原声（可选）
  --pad-bytes 16                  # 文件尾追加字节（破文件哈希，可选）
```

输出：网盘 `颗粒/成片/批次-<时间>/` 下 N 条 mp4 + `混剪台账.md`

### 方式二音频规则（2026-10-02 用户确认）

- 开头爆款原声至少 **5 秒**，要让完整句子说完（之前 2.4 秒太短被用户指出）
- 开头原声 + 话术配音拼接前，自动用 `loudnorm=I=-16` 统一两段音量（之前配音比原声低 11.5dB 听不清）
- 相关代码：`pycut/cli.py` 的 3b 拼接逻辑

## 混剪去重机制（四层，见 docs/06）

1. **素材调度**：usage 均衡、source range 去重、seed 可复现、开头池轮换
2. **单片段变换**：裁剪位移 ±3~5%、变速 0.95~1.05、色调 ±5%、镜像（避文字）
3. **结构层**：打散重排、转场（硬切/淡入淡出/缩放）、3 种字幕样式轮换
4. **特效层**：时域噪点（3 变体）、暗角、1.03x 放大、抽帧、移动水印、横屏模糊填充、字幕带毛玻璃
5. **音频层**（P0）：变调 ±1%、混响、EQ 微调、重采样 48000（对抗音频指纹）
6. **文件层**：H.264 重编码、GOP/码率轮换、元数据清洗

## 话术生成流程（用户 2026-10-02 确认）

用户说"生成视频"时，Arlo 先在聊天里确认再开工：
- 话术来源：Arlo 按产品信息生成 / 用户自己提供
- 时长：默认 30 秒左右
- 去重强度、条数

话术结构固定：前几秒用自家爆款切片+原声，后面接 Arlo 按产品信息合成的话术（TTS 配音）。

## 测试

```bash
~/workspace/pycut/.venv/bin/python -m pytest tests/ -v
```

## Git 提交

走 SSH 直推（用户已配置）：
```bash
export GIT_SSH_COMMAND="ssh -F /home/hatch/.ssh/config"
cd /tmp && git clone git@github.com:natefox2017/pycut.git pycut-push
# 复制修改的文件...
cd pycut-push && git add -A && git commit -m "xxx" && git push origin main
```

Git commit message 必须用英文。

## 注意事项

- Arlo = 用户的 AI 助手（Muse），代码注释里提到 Arlo 的地方都是指 AI 执行的步骤
- Hatch = 云电脑运行时，`hatch_gws_cli` 只在云电脑上可用
- 去重是概率工程，不能保证 100% 过审（平台算法黑盒）
- 任务单状态不是"已填写"时，`pycut mix` 拒绝执行
