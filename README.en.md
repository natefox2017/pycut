# PyCut

[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![FFmpeg 8](https://img.shields.io/badge/ffmpeg-8-green.svg)](https://ffmpeg.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

中文 | [English](README.en.md)

Cloud video slicing & mixing pipeline: batch-organize long videos into a categorized
slice library, then batch-mix finished videos from the slices.
Python + FFmpeg, pure CLI, no GUI.

## Features

| Feature | Input | Output | Status |
|---|---|---|---|
| **Organize slices** | Long-video folders on Drive | 2–8s slices archived by category under `切片库/` | ✅ Ready |
| Mix videos | `切片库/` + scripts | Finished videos under `成片/` | 🚧 Designed (see `docs/06`) |
| Clean videos | Drive folder | Dedupe + remove system junk (to trash) | ✅ Ready |
| Script pipeline | Viral videos / product info | Scripts in the task workbook | ✅ Ready |

Highlights:

- **AI analysis**: scene pre-segmentation → faster-whisper transcription for sentence
  timestamps → cut points snapped to sentence ends (a slice with speech must finish
  the sentence) → AI reviews frames to classify
- **Resumable**: progress ledger on Drive (path + MD5); processed videos are never re-downloaded
- **Batched**: ~1 GB per batch — download → process → upload → delete local → next batch
- **Task workbook gate**: an xlsx task form is generated before each batch; work starts
  only after the user fills it in

## Runtime

> **Currently supports Muse cloud computers only** (muse.ai runtime): Python 3.12,
> FFmpeg 8, Google Drive via `hatch_gws_cli` (auth managed by the runtime, zero-config).
>
> Roadmap: support for other cloud computers and local machines later
> (`drive.py` will become a pluggable backend).

## Quickstart (Muse cloud computer)

```bash
cd ~/workspace/pycut

# 1. Scan Drive folders (read-only)
python -m pycut scan --path 颗粒/爆款素材/抖音

# 2. Generate task form → user fills it → validate
python -m pycut taskform new --path 颗粒 --name 任务单-2026-10-01.xlsx
python -m pycut taskform read --path 颗粒 --name 任务单-2026-10-01.xlsx

# 3. AI analysis: build evidence packs (with transcribed sentence timestamps)
python -m pycut analyze --path 颗粒/爆款素材/抖音 --limit 2

# 4. Fill run/decisions/<md5>.json (cut points + categories + reasons), then slice
python -m pycut slice --path 颗粒/爆款素材/抖音 --decisions run/decisions

# 5. Import short videos directly / clean videos
python -m pycut import-shorts --path 颗粒/片段/片段手机录制
python -m pycut organize --path 颗粒 --dry-run
```

Full command reference: [`docs/05-运行手册.md`](docs/05-运行手册.md) (Chinese).
Cloud setup & recovery: [`docs/07-云电脑运行指南.md`](docs/07-云电脑运行指南.md) (Chinese).

## Docs

| Doc | Content |
|---|---|
| [00-需求说明](docs/00-需求说明.md) | Background, requirements, constraints, scope |
| [01-处理流程](docs/01-处理流程.md) | Full pipelines of both features |
| [02-文件夹命名规范](docs/02-文件夹命名规范.md) | Drive & local naming conventions |
| [03-开工前准备清单](docs/03-开工前准备清单.md) | Pre-flight checklist |
| [04-目录作用确认表](docs/04-目录作用确认表.md) | Drive folder purpose confirmation sheet |
| [05-运行手册](docs/05-运行手册.md) | Command reference & SOP |
| [06-混剪详细设计](docs/06-混剪详细设计.md) | Mixing: dedup, duration alignment, subtitle styles |
| [07-云电脑运行指南](docs/07-云电脑运行指南.md) | Cloud setup, recovery, FAQ |

(Docs are in Chinese; translations welcome.)

## Module layout

```
pycut/
├── cli.py        # CLI entry (taskform/slice/transcribe/scripts/clean…)
├── config.py     # Slice rules, Drive layout
├── drive.py      # Google Drive client (hatch_gws_cli, cloud-only)
├── ledger.py     # Progress ledger on Drive (MD5 dedupe, resume)
├── media.py      # ffprobe/scene detection/frame extraction/volume
├── slicer.py     # Cut planning (sentence-aligned) + export + QC
├── speech.py     # faster-whisper transcription (isolated venv)
├── analyze.py    # AI evidence packs + decision validation
├── categorize.py # Slice classification
├── pipeline.py   # Batch pipeline (download→process→upload→ledger→cleanup)
├── clean.py      # Clean videos (dedupe/remove junk)
├── product.py    # Product fact sheet + script fact-check
├── voice.py      # Script transcription (subtitle frames)
├── scriptgen.py  # Script generation (two modes)
├── hooks.py      # Viral opening pool
└── taskform.py   # Task form generation/validation
```

## Relationship with EasyCut desktop

Slicing semantics match `docs/SLICE-PREPARATION.md` of
[easycut](https://github.com/natefox2017/easycut)
(scan → validate → memory check → analyze → plan → export → QC);
the execution layer is "Muse cloud CLI" instead of "desktop GUI + local machine".
No code shared between the repos.

Confirmed differences: **videos <10s are not sliced**; completion memory uses the
Drive ledger `EasyCut-处理进度.md` (path + MD5) instead of sqlite.

## License

MIT, see [LICENSE](LICENSE).
