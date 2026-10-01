"""Google Drive 客户端。

云电脑说明（必读）：
- 所有 Drive 操作经 `hatch_gws_cli drive` 执行。这是 Hatch 云电脑运行时自带的
  Google Workspace CLI，认证由运行时托管（OAuth token 对代码透明），开箱即用。
- 因此本模块在云电脑上零配置即可跑；但它**只能在 Hatch 云电脑上跑**——
  `hatch_gws_cli` 不存在于其他机器。
- 若将来要搬到用户本地电脑，需新增一个 DriveBackend 实现（Google 官方
  OAuth API），并让 DriveClient 按环境自动选择后端。接口保持不变：
  list_folder / find_child / ensure_folder / resolve_path / download /
  upload / update_content / trash / move / md5_of。
- 上传用 `+upload` helper（自动处理 multipart）；删除一律进回收站（trash），
  不做永久删除。
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from json import JSONDecoder
from pathlib import Path


@dataclass
class DriveFile:
    id: str
    name: str
    mime_type: str
    size: int = 0
    md5: str = ""

    @property
    def is_folder(self) -> bool:
        return "folder" in self.mime_type

    @property
    def is_video(self) -> bool:
        return self.mime_type.startswith("video") or self.name.lower().endswith(
            (".mp4", ".mov", ".mkv", ".m4v", ".webm", ".avi")
        )


class DriveClient:
    """所有 Drive 操作经 hatch_gws_cli 执行（云电脑运行时自带，认证免配置）。

    约定：
    - folder_id 优先用 DriveLayout 里的常量，不硬编码在业务代码里；
    - 下载/上传的本地中转目录统一用 cli.WORKDIR（run/downloads 等）；
    - 批量操作前先用 list_folder 确认目标存在，避免误建目录。
    """

    def _run(self, *args: str) -> dict:
        r = subprocess.run(
            ["hatch_gws_cli", "drive", *args],
            capture_output=True, text=True, timeout=300,
        )
        if r.returncode != 0:
            raise RuntimeError(f"drive 命令失败: {r.stderr.strip()[:300]}")
        raw = r.stdout
        obj, _ = JSONDecoder().raw_decode(raw[raw.find("{"):])
        return obj

    # ---------- 查询 ----------

    def list_folder(self, folder_id: str) -> list[DriveFile]:
        obj = self._run(
            "files", "list", "--params",
            json.dumps({
                "q": f"'{folder_id}' in parents and trashed=false",
                "pageSize": 200,
                "fields": "files(id,name,mimeType,size,md5Checksum)",
            }),
        )
        out = []
        for f in obj.get("files", []):
            out.append(DriveFile(
                id=f["id"], name=f["name"], mime_type=f.get("mimeType", ""),
                size=int(f.get("size") or 0), md5=f.get("md5Checksum") or "",
            ))
        return out

    def find_child(self, parent_id: str, name: str) -> DriveFile | None:
        for f in self.list_folder(parent_id):
            if f.name == name:
                return f
        return None

    def ensure_folder(self, parent_id: str, name: str) -> DriveFile:
        """目录不存在则创建，返回目录对象。"""
        hit = self.find_child(parent_id, name)
        if hit and hit.is_folder:
            return hit
        obj = self._run(
            "files", "create", "--params", '{"ignoreDefaultVisibility":true}',
            "--json", json.dumps({
                "name": name,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent_id],
            }),
        )
        return DriveFile(id=obj["id"], name=name,
                         mime_type="application/vnd.google-apps.folder")

    def resolve_path(self, root_id: str, *parts: str) -> DriveFile:
        """按相对路径逐级查找（如 '爆款素材', '抖音'），不存在抛错。"""
        cur_id, cur_name = root_id, ""
        for p in parts:
            hit = self.find_child(cur_id, p)
            if hit is None:
                raise FileNotFoundError(f"网盘目录不存在: {cur_name}/{p}")
            cur_id, cur_name = hit.id, f"{cur_name}/{p}" if cur_name else p
        # 取详细信息
        obj = self._run("files", "get", "--params",
                        json.dumps({"fileId": cur_id,
                                    "fields": "id,name,mimeType,size"}))
        return DriveFile(id=obj["id"], name=obj["name"],
                         mime_type=obj.get("mimeType", ""),
                         size=int(obj.get("size") or 0))

    # ---------- 下载 / 上传 ----------

    def download(self, file: DriveFile, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._run("files", "get", "--params",
                  json.dumps({"fileId": file.id, "alt": "media"}),
                  "--output", str(dest))
        return dest

    def upload(self, local_path: Path, parent_id: str,
               name: str | None = None) -> DriveFile:
        if not local_path.exists():
            raise FileNotFoundError(str(local_path))
        args = ["+upload", str(local_path), "--parent", parent_id]
        if name:
            args += ["--name", name]
        obj = self._run(*args)
        return DriveFile(id=obj["id"], name=obj.get("name", local_path.name),
                         mime_type=obj.get("mimeType", ""))

    def update_content(self, file_id: str, local_path: Path) -> None:
        """覆盖更新已有文件内容（保留文件 ID，如台账）。"""
        self._run("files", "update", "--params",
                  json.dumps({"fileId": file_id}),
                  "--upload", str(local_path))

    def trash(self, file_id: str) -> None:
        """移入回收站（可撤销，不做永久删除）。"""
        self._run("files", "update", "--params",
                  json.dumps({"fileId": file_id}),
                  "--json", json.dumps({"trashed": True}))

    def move(self, file_id: str, from_parent_id: str,
             to_parent_id: str) -> None:
        self._run(
            "files", "update", "--params",
            json.dumps({"fileId": file_id,
                        "addParents": to_parent_id,
                        "removeParents": from_parent_id}),
        )

    # ---------- 工具 ----------

    @staticmethod
    def md5_of(path: Path) -> str:
        h = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
