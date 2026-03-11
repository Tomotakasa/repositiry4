"""
SFTP NAS client using paramiko.
For NAS devices accessible via SSH/SFTP (Linux-based NAS like Synology with SSH enabled).
"""

import io
import stat as stat_module
from datetime import date
from pathlib import Path
from typing import List, Dict, Any, Optional

try:
    import paramiko
    HAS_PARAMIKO = True
except ImportError:
    HAS_PARAMIKO = False

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".tiff", ".tif"}
RAW_EXTENSIONS = {".raw", ".cr2", ".cr3", ".nef", ".arw", ".dng", ".orf", ".rw2", ".pef"}


class SFTPClient:
    def __init__(self, host: str, port: int, username: str, password: str):
        if not HAS_PARAMIKO:
            raise RuntimeError("paramiko is not installed. Run: pip install paramiko")
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self._ssh: Optional[paramiko.SSHClient] = None
        self._sftp: Optional[paramiko.SFTPClient] = None

    def _connect(self):
        if self._sftp:
            try:
                self._sftp.listdir("/")
                return self._sftp
            except Exception:
                self._sftp = None
                self._ssh = None

        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(self.host, port=self.port, username=self.username,
                    password=self.password, timeout=15)
        self._ssh = ssh
        self._sftp = ssh.open_sftp()
        return self._sftp

    def test_connection(self) -> str:
        sftp = self._connect()
        entries = sftp.listdir("/")
        return f"SFTP接続成功。ルートディレクトリ: {', '.join(entries[:5])}"

    def list_directory(self, folder_path: str) -> List[Dict[str, Any]]:
        sftp = self._connect()
        items = []
        try:
            for attr in sorted(sftp.listdir_attr(folder_path),
                               key=lambda a: (not stat_module.S_ISDIR(a.st_mode or 0), a.filename.lower())):
                is_dir = stat_module.S_ISDIR(attr.st_mode or 0)
                items.append({
                    "name": attr.filename,
                    "path": folder_path.rstrip("/") + "/" + attr.filename,
                    "is_dir": is_dir,
                    "size": attr.st_size or 0,
                    "modified": attr.st_mtime or 0,
                })
        except Exception as e:
            raise RuntimeError(f"フォルダ一覧取得エラー: {e}")
        return items

    def list_photos(
        self,
        folder_path: str,
        date_from: Optional[date],
        date_to: Optional[date],
        include_raw: bool = False,
    ) -> List[Dict[str, Any]]:
        sftp = self._connect()
        results = []
        self._collect_photos_sftp(sftp, folder_path, date_from, date_to, include_raw, results)
        return results

    def _collect_photos_sftp(self, sftp, folder_path, date_from, date_to, include_raw, results):
        allowed_exts = PHOTO_EXTENSIONS.copy()
        if include_raw:
            allowed_exts |= RAW_EXTENSIONS
        try:
            attrs = sftp.listdir_attr(folder_path)
        except Exception:
            return
        for attr in attrs:
            full_path = folder_path.rstrip("/") + "/" + attr.filename
            if stat_module.S_ISDIR(attr.st_mode or 0):
                self._collect_photos_sftp(sftp, full_path, date_from, date_to, include_raw, results)
            else:
                ext = Path(attr.filename).suffix.lower()
                if ext not in allowed_exts:
                    continue
                mod_date = date.fromtimestamp(attr.st_mtime) if attr.st_mtime else None
                if date_from and mod_date and mod_date < date_from:
                    continue
                if date_to and mod_date and mod_date > date_to:
                    continue
                results.append({
                    "path": full_path,
                    "filename": attr.filename,
                    "size": attr.st_size or 0,
                    "date": mod_date.isoformat() if mod_date else "",
                })

    def download_file(self, file_path: str) -> bytes:
        sftp = self._connect()
        buf = io.BytesIO()
        sftp.getfo(file_path, buf)
        return buf.getvalue()

    def upload_file(self, file_path: str, data: bytes):
        sftp = self._connect()
        buf = io.BytesIO(data)
        sftp.putfo(buf, file_path)

    def create_directory(self, dir_path: str):
        sftp = self._connect()
        parts = dir_path.strip("/").split("/")
        current = ""
        for part in parts:
            current = current + "/" + part if current else "/" + part
            try:
                sftp.mkdir(current)
            except Exception:
                pass  # Already exists
