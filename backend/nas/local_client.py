"""
Local path NAS client.
For NAS drives already mounted as local filesystem paths
(e.g., /mnt/nas, /Volumes/NAS, Z:\\).
"""

import os
import shutil
from datetime import date
from pathlib import Path
from typing import List, Dict, Any, Optional

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".tiff", ".tif"}
RAW_EXTENSIONS = {".raw", ".cr2", ".cr3", ".nef", ".arw", ".dng", ".orf", ".rw2", ".pef"}


class LocalNASClient:
    def __init__(self, base_path: str):
        self.base_path = Path(base_path)

    def _full_path(self, rel_path: str) -> Path:
        # Strip leading slash and join with base
        clean = rel_path.lstrip("/\\")
        full = self.base_path / clean
        # Safety: ensure path stays within base_path
        try:
            full.resolve().relative_to(self.base_path.resolve())
        except ValueError:
            raise PermissionError(f"パス外へのアクセスが拒否されました: {rel_path}")
        return full

    def test_connection(self) -> str:
        if not self.base_path.exists():
            raise FileNotFoundError(f"パスが見つかりません: {self.base_path}")
        if not self.base_path.is_dir():
            raise NotADirectoryError(f"ディレクトリではありません: {self.base_path}")
        return f"接続成功。ベースパス: {self.base_path}"

    def list_directory(self, folder_path: str) -> List[Dict[str, Any]]:
        full = self._full_path(folder_path)
        if not full.exists():
            raise FileNotFoundError(f"フォルダが見つかりません: {folder_path}")
        items = []
        for entry in sorted(full.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
            stat = entry.stat()
            items.append({
                "name": entry.name,
                "path": "/" + str(entry.relative_to(self.base_path)).replace("\\", "/"),
                "is_dir": entry.is_dir(),
                "size": stat.st_size,
                "modified": stat.st_mtime,
            })
        return items

    def list_photos(
        self,
        folder_path: str,
        date_from: Optional[date],
        date_to: Optional[date],
        include_raw: bool = False,
    ) -> List[Dict[str, Any]]:
        full = self._full_path(folder_path)
        allowed_exts = PHOTO_EXTENSIONS.copy()
        if include_raw:
            allowed_exts |= RAW_EXTENSIONS

        results = []
        for entry in full.rglob("*"):
            if not entry.is_file():
                continue
            if entry.suffix.lower() not in allowed_exts:
                continue
            stat = entry.stat()
            mod_date = date.fromtimestamp(stat.st_mtime)
            if date_from and mod_date < date_from:
                continue
            if date_to and mod_date > date_to:
                continue
            results.append({
                "path": "/" + str(entry.relative_to(self.base_path)).replace("\\", "/"),
                "filename": entry.name,
                "size": stat.st_size,
                "date": mod_date.isoformat(),
            })
        return results

    def download_file(self, file_path: str) -> bytes:
        full = self._full_path(file_path)
        return full.read_bytes()

    def upload_file(self, file_path: str, data: bytes):
        full = self._full_path(file_path)
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(data)

    def create_directory(self, dir_path: str):
        full = self._full_path(dir_path)
        full.mkdir(parents=True, exist_ok=True)
