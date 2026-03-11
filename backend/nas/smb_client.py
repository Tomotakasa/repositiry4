"""
SMB/CIFS NAS client using pysmb.
Supports Synology, QNAP, Windows shares, etc.
"""

import io
import os
from datetime import date
from pathlib import Path
from typing import List, Dict, Any, Optional

try:
    from smb.SMBConnection import SMBConnection
    from smb import smb_structs
    HAS_SMB = True
except ImportError:
    HAS_SMB = False

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".tiff", ".tif"}
RAW_EXTENSIONS = {".raw", ".cr2", ".cr3", ".nef", ".arw", ".dng", ".orf", ".rw2", ".pef"}


class SMBClient:
    def __init__(self, host: str, port: int, username: str, password: str, share_name: str):
        if not HAS_SMB:
            raise RuntimeError("pysmb is not installed. Run: pip install pysmb")
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.share_name = share_name
        self._conn: Optional[SMBConnection] = None

    def _connect(self) -> SMBConnection:
        if self._conn:
            try:
                self._conn.listShares()
                return self._conn
            except Exception:
                self._conn = None

        conn = SMBConnection(
            self.username,
            self.password,
            "nas_album_client",
            self.host,
            use_ntlm_v2=True,
            is_direct_tcp=(self.port == 445),
        )
        ok = conn.connect(self.host, self.port)
        if not ok:
            raise ConnectionError(f"SMB接続失敗: {self.host}:{self.port}")
        self._conn = conn
        return conn

    def test_connection(self) -> str:
        conn = self._connect()
        shares = [s.name for s in conn.listShares() if not s.isSpecial and not s.isTemporary]
        return f"接続成功。共有フォルダ: {', '.join(shares)}"

    def list_directory(self, folder_path: str) -> List[Dict[str, Any]]:
        conn = self._connect()
        folder_path = folder_path.lstrip("/") or ""
        items = []
        try:
            entries = conn.listPath(self.share_name, folder_path or "/")
            for entry in entries:
                if entry.filename in (".", ".."):
                    continue
                items.append({
                    "name": entry.filename,
                    "path": ("/" + folder_path + "/" + entry.filename).replace("//", "/"),
                    "is_dir": entry.isDirectory,
                    "size": entry.file_size,
                    "modified": entry.last_write_time,
                })
        except Exception as e:
            raise RuntimeError(f"フォルダ一覧取得エラー: {e}")
        return sorted(items, key=lambda x: (not x["is_dir"], x["name"].lower()))

    def list_photos(
        self,
        folder_path: str,
        date_from: Optional[date],
        date_to: Optional[date],
        include_raw: bool = False,
    ) -> List[Dict[str, Any]]:
        """Recursively list photos in folder with date filter."""
        conn = self._connect()
        results = []
        self._collect_photos_smb(conn, folder_path, date_from, date_to, include_raw, results)
        return results

    def _collect_photos_smb(self, conn, folder_path, date_from, date_to, include_raw, results):
        folder_path = folder_path.lstrip("/") or "/"
        try:
            entries = conn.listPath(self.share_name, folder_path)
        except Exception:
            return

        allowed_exts = PHOTO_EXTENSIONS.copy()
        if include_raw:
            allowed_exts |= RAW_EXTENSIONS

        for entry in entries:
            if entry.filename in (".", ".."):
                continue
            full_path = (folder_path.rstrip("/") + "/" + entry.filename)
            if entry.isDirectory:
                self._collect_photos_smb(conn, full_path, date_from, date_to, include_raw, results)
            else:
                ext = Path(entry.filename).suffix.lower()
                if ext not in allowed_exts:
                    continue
                mod_date = date.fromtimestamp(entry.last_write_time) if entry.last_write_time else None
                if date_from and mod_date and mod_date < date_from:
                    continue
                if date_to and mod_date and mod_date > date_to:
                    continue
                results.append({
                    "path": "/" + full_path,
                    "filename": entry.filename,
                    "size": entry.file_size,
                    "date": mod_date.isoformat() if mod_date else "",
                })

    def download_file(self, file_path: str) -> bytes:
        conn = self._connect()
        buf = io.BytesIO()
        file_path = file_path.lstrip("/")
        conn.retrieveFile(self.share_name, file_path, buf)
        return buf.getvalue()

    def upload_file(self, file_path: str, data: bytes):
        conn = self._connect()
        buf = io.BytesIO(data)
        file_path = file_path.lstrip("/")
        conn.storeFile(self.share_name, file_path, buf)

    def create_directory(self, dir_path: str):
        conn = self._connect()
        parts = dir_path.strip("/").split("/")
        current = ""
        for part in parts:
            current = current + "/" + part if current else part
            try:
                conn.createDirectory(self.share_name, current)
            except Exception:
                pass  # Already exists
