"""功能一主流程：分批下载 -> 处理 -> 分类上传 -> 台账 -> 清理。

两种输入模式：
- slice 模式：爆款长视频，场景检测 + 切点规划 + 逐段导出
- shorts 模式：短视频，直接标准化入库（不切割）
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from .analyze import decisions_to_cuts, load_decisions
from .categorize import decide_category
from .config import (CATEGORIES, REVIEW_CATEGORY, DriveLayout, SliceRules,
                     SourceSpec)
from .drive import DriveClient, DriveFile
from .ledger import Ledger, SliceRecord
from .media import MediaInfo, detect_scenes, probe
from .slicer import export_slice, normalize_full, plan_cuts, qc_ok


@dataclass
class BatchResult:
    source: DriveFile
    status: str            # done / skipped_short / skipped_done / failed
    slices: list[tuple[Path, str]] = None  # (本地路径, 分类)
    note: str = ""

    def __post_init__(self):
        if self.slices is None:
            self.slices = []


def _fmt_size(n: int) -> str:
    return f"{n / 1e6:.0f}MB" if n else "0MB"


class SlicePipeline:
    def __init__(self, client: DriveClient, layout: DriveLayout,
                 rules: SliceRules, workdir: Path,
                 keyword_map: dict[str, str] | None = None,
                 dry_run: bool = False,
                 decisions_dir: Path | None = None):
        self.client = client
        self.layout = layout
        self.rules = rules
        self.workdir = workdir
        self.dl_dir = workdir / "downloads"
        self.work = workdir / "work"
        self.out_dir = workdir / "outputs"
        self.keyword_map = keyword_map or {}
        self.dry_run = dry_run
        #: AI 决策目录：若包含 <md5>.json（decisions.json 改名），则用 AI 决策
        #: 代替机械切点规划。None 表示纯机械模式。
        self.decisions_dir = decisions_dir
        for d in (self.dl_dir, self.work, self.out_dir):
            d.mkdir(parents=True, exist_ok=True)
        # 网盘切片库目录（分类子目录按需创建，缓存 id）
        self._cat_ids: dict[str, str] = {}
        self._slices_root_id: str | None = None

    # ---------- 网盘目录 ----------

    def _slices_root(self) -> str:
        if self._slices_root_id is None:
            # 切片库建在 颗粒/ 下（不是外层共享根目录）
            grain = self.client.resolve_path(
                self.layout.project_root_id, self.layout.source_root_name)
            root = self.client.ensure_folder(
                grain.id, self.layout.slices_dir_name)
            self._slices_root_id = root.id
        return self._slices_root_id

    def _category_id(self, category: str) -> str:
        if category not in self._cat_ids:
            cat = self.client.ensure_folder(self._slices_root(), category)
            self._cat_ids[category] = cat.id
        return self._cat_ids[category]

    # ---------- 批量入口 ----------

    def run_sources(self, sources: list[DriveFile],
                    mode: str = "slice") -> list[BatchResult]:
        """按 ~1G 分批处理一批源视频。mode: slice | shorts。"""
        ledger = Ledger(self.client, self.layout.project_root_id,
                        self.layout.ledger_name, self.workdir).load()
        results: list[BatchResult] = []
        batch: list[DriveFile] = []
        batch_bytes = 0
        limit = int(self.layout.batch_gb * 1e9)

        def flush():
            if batch:
                results.extend(self._run_batch(batch, ledger, mode))
                batch.clear()

        for src in sources:
            md5 = src.md5 or ""
            if ledger.is_processed(md5):
                results.append(BatchResult(src, "skipped_done",
                                           note="台账已有 MD5，跳过"))
                continue
            if batch_bytes + src.size > limit and batch:
                flush()
                batch_bytes = 0
            batch.append(src)
            batch_bytes += src.size
        flush()
        if not self.dry_run:
            ledger.save()
        return results

    # ---------- 单批 ----------

    def _run_batch(self, batch: list[DriveFile], ledger: Ledger,
                   mode: str) -> list[BatchResult]:
        results: list[BatchResult] = []
        for src in batch:
            try:
                res = self._process_one(src, ledger, mode)
            except Exception as ex:  # noqa: BLE001 - 单个失败不阻断整批
                err = str(ex)[:200] or repr(ex)[:200]
                res = BatchResult(src, "failed", note=err)
                ledger.add_slice(SliceRecord(
                    source_path="", filename=src.name, md5=src.md5,
                    size=_fmt_size(src.size), slice_count=0,
                    output_dir="", status="failed", note=err))
            results.append(res)
        self._cleanup_batch(batch)
        return results

    def _process_one(self, src: DriveFile, ledger: Ledger,
                     mode: str) -> BatchResult:
        local = self.dl_dir / f"{src.md5[:8] if src.md5 else src.id[:8]}_{src.name}"
        if not local.exists():
            self.client.download(src, local)
        md5 = src.md5 or DriveClient.md5_of(local)
        info = probe(local)

        if mode == "slice" and info.duration < self.rules.skip_below_seconds:
            ledger.add_slice(SliceRecord(
                source_path="", filename=src.name, md5=md5,
                size=_fmt_size(src.size), slice_count=0,
                output_dir="", status="skipped_short"))
            return BatchResult(src, "skipped_short",
                               note=f"时长{info.duration:.1f}s<10s，不切片")

        if mode == "slice":
            slices = self._slice_video(local, md5, info)
        else:
            slices = self._import_short(local, md5, info, src.name)

        # 上传（直接传到最终分类文件夹，不移动）
        uploaded = 0
        by_cat: dict[str, list[tuple[str, float, str]]] = {}
        for item in slices:
            path, category = item[0], item[1]
            speech = item[2] if len(item) > 2 else ""
            dur = item[3] if len(item) > 3 else 0.0
            if self.dry_run:
                continue
            self.client.upload(path, self._category_id(category))
            uploaded += 1
            by_cat.setdefault(category, []).append(
                (path.name, dur, speech))

        # 更新各分类文件夹的 00-索引.md（简体）
        if not self.dry_run and by_cat:
            self._update_indexes(by_cat)

        rel_out = f"{self.layout.slices_dir_name}/<分类>"
        ledger.add_slice(SliceRecord(
            source_path="", filename=src.name, md5=md5,
            size=_fmt_size(src.size), slice_count=len(slices),
            output_dir=rel_out, status="done"))
        return BatchResult(src, "done",
                           note=f"{len(slices)}条切片，{uploaded}已上传")

    # ---------- 切片 / 直接入库 ----------

    def _slice_video(self, local: Path, md5: str,
                     info: MediaInfo) -> list[tuple]:
        # AI 决策优先：decisions_dir/<md5>.json 存在则用 AI 切点+分类
        # 返回 [(路径, 分类, 话术, 时长)]，话术用于写索引
        ai_plan = self._load_ai_plan(md5)
        if ai_plan is not None:
            return self._export_ai_plan(local, md5, ai_plan)
        scenes = detect_scenes(local, self.rules.scene_threshold)
        cuts = plan_cuts(info.duration, scenes, self.rules)
        out: list[tuple[Path, str]] = []
        for i, (s, e) in enumerate(cuts):
            name = f"{md5[:8]}_{i + 1:03d}.mp4"
            dst = self.out_dir / name
            export_slice(local, s, e, dst, self.rules)
            ok, reason = qc_ok(dst, self.rules, expect_dur=e - s)
            if not ok:
                dst.unlink(missing_ok=True)
                continue
            category, _why = decide_category(info, local.name, dst,
                                             self.keyword_map)
            # 文件名带分类后缀便于肉眼识别
            final = self.out_dir / f"{md5[:8]}_{i + 1:03d}_{category}.mp4"
            dst.rename(final)
            out.append((final, category))
        return out

    def _load_ai_plan(self, md5: str,
                      ) -> list[tuple[tuple[float, float], str, str, str]] | None:
        """读取 AI 决策（decisions_dir/<md5>.json），没有则返回 None。"""
        if self.decisions_dir is None:
            return None
        for cand in (self.decisions_dir / f"{md5}.json",
                     self.decisions_dir / f"{md5[:8]}.json"):
            if cand.exists():
                return decisions_to_cuts(load_decisions(cand))
        return None

    def _export_ai_plan(self, local: Path, md5: str,
                        plan: list[tuple[tuple[float, float], str, str, str]]
                        ) -> list[tuple[Path, str, str, float]]:
        """导出 AI 决策的切片，返回 [(本地路径, 分类, 话术, 时长)]。"""
        out: list[tuple[Path, str, str, float]] = []
        for i, ((s, e), category, _reason, speech) in enumerate(plan):
            dst = self.out_dir / f"{md5[:8]}_{i + 1:03d}_{category}.mp4"
            export_slice(local, s, e, dst, self.rules)
            ok, reason = qc_ok(dst, self.rules, expect_dur=e - s)
            if not ok:
                dst.unlink(missing_ok=True)
                continue
            out.append((dst, category, speech, round(e - s, 1)))
        return out

    def _import_short(self, local: Path, md5: str, info: MediaInfo,
                      src_name: str) -> list[tuple[Path, str]]:
        dst = self.out_dir / f"{md5[:8]}_001.mp4"
        normalize_full(local, dst, self.rules)
        ok, reason = qc_ok(dst, self.rules)
        if not ok:
            dst.unlink(missing_ok=True)
            raise RuntimeError(f"QC 不通过: {reason}")
        category, _why = decide_category(info, src_name, dst,
                                         self.keyword_map)
        final = self.out_dir / f"{md5[:8]}_001_{category}.mp4"
        dst.rename(final)
        return [(final, category)]

    # ---------- 清理 ----------

    def _cleanup_batch(self, batch: list[DriveFile]) -> None:
        for p in self.dl_dir.iterdir():
            p.unlink(missing_ok=True)
        for p in self.out_dir.iterdir():
            p.unlink(missing_ok=True)
        for p in self.work.iterdir():
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)

    def _update_indexes(self, by_cat: dict[str, list[tuple[str, float, str]]]
                      ) -> None:
        """更新各分类文件夹的 00-索引.md，追加新切片条目（简体中文）。

        by_cat: {分类: [(文件名, 时长秒, 话术)]}
        """
        try:
            from opencc import OpenCC
            cc = OpenCC("t2s")
            to_simp = cc.convert
        except ImportError:
            to_simp = lambda x: x  # noqa: E731

        from .config import CATEGORY_POSITION
        for category, items in by_cat.items():
            cat_id = self._category_id(category)
            idx_file = self.client.find_child(cat_id, "00-索引.md")
            if idx_file is None:
                # 新建索引
                tmp = self.work / f"idx_{category}.md"
                tmp.write_text(
                    f"# {category} 切片索引\n\n## 切片列表\n",
                    encoding="utf-8")
                self.client.upload(tmp, cat_id)
                idx_file = self.client.find_child(cat_id, "00-索引.md")
                tmp.unlink(missing_ok=True)
            if idx_file is None:
                continue
            # 下载、追加、上传
            tmp = self.work / f"idx_{category}.md"
            self.client.download(idx_file, tmp)
            content = tmp.read_text(encoding="utf-8")
            if "## 切片列表" not in content:
                content += "\n## 切片列表\n"
            pos = CATEGORY_POSITION.get(category, "中段")
            new_lines = []
            for name, dur, speech in items:
                speech_s = to_simp(speech).strip() if speech else ""
                new_lines.append(
                    f"- {name} | {dur}s | {pos} | {speech_s}")
            # 去重：已存在的跳过
            existing = set(content.split("\n"))
            to_add = [l for l in new_lines if l not in existing]
            if to_add:
                if not content.endswith("\n"):
                    content += "\n"
                content += "\n".join(to_add) + "\n"
                tmp.write_text(content, encoding="utf-8")
                self.client.update_content(idx_file.id, tmp)
            tmp.unlink(missing_ok=True)

    # ---------- 扫描 ----------

    def scan(self, folder_id: str) -> list[DriveFile]:
        """扫描目录，返回视频文件列表（只读，不下载）。"""
        return [f for f in self.client.list_folder(folder_id) if f.is_video]
