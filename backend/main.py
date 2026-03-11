"""
NAS Photo Album Selector - FastAPI Backend
Automatically selects and organizes photos from NAS for albums using Claude AI.
"""

import os
import json
import asyncio
import tempfile
import shutil
from datetime import datetime, date
from pathlib import Path
from typing import List, Optional, Dict, Any

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from nas.smb_client import SMBClient
from nas.local_client import LocalNASClient
from nas.sftp_client import SFTPClient
from photo.processor import PhotoProcessor
from ai.selector import ClaudePhotoSelector

app = FastAPI(title="NAS Photo Album Selector", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory state for jobs
jobs: Dict[str, Dict[str, Any]] = {}

# Config storage path
CONFIG_FILE = Path("/tmp/nas_album_config.json")


# ── Request Models ──────────────────────────────────────────────────────────

class NASConnectionRequest(BaseModel):
    connection_type: str  # "smb", "local", "sftp"
    # SMB fields
    host: Optional[str] = None
    port: Optional[int] = 445
    username: Optional[str] = None
    password: Optional[str] = None
    share_name: Optional[str] = None
    # Local / mount path
    base_path: Optional[str] = None
    # SFTP fields
    sftp_port: Optional[int] = 22


class AlbumRequest(BaseModel):
    connection_type: str
    host: Optional[str] = None
    port: Optional[int] = 445
    username: Optional[str] = None
    password: Optional[str] = None
    share_name: Optional[str] = None
    base_path: Optional[str] = None
    sftp_port: Optional[int] = 22

    source_folders: List[str]
    date_from: Optional[str] = None   # YYYY-MM-DD
    date_to: Optional[str] = None     # YYYY-MM-DD
    photo_count: int = 30
    album_type: str = "general"       # see ALBUM_TYPES
    album_name: str = ""
    output_folder: str = ""           # relative path on NAS
    custom_prompt: Optional[str] = None
    include_raw: bool = False
    min_resolution: Optional[int] = None  # min width or height in px


class FolderBrowseRequest(BaseModel):
    connection_type: str
    host: Optional[str] = None
    port: Optional[int] = 445
    username: Optional[str] = None
    password: Optional[str] = None
    share_name: Optional[str] = None
    base_path: Optional[str] = None
    sftp_port: Optional[int] = 22
    folder_path: str = "/"


# ── Album Type Definitions ───────────────────────────────────────────────────

ALBUM_TYPES = {
    "general": {
        "label": "ベストショット",
        "description": "品質・構図・明るさを重視した最高の写真",
        "icon": "⭐",
    },
    "family": {
        "label": "家族の思い出",
        "description": "家族の笑顔・温かい瞬間を優先",
        "icon": "👨‍👩‍👧‍👦",
    },
    "travel": {
        "label": "旅行・おでかけ",
        "description": "景色・観光スポット・体験を重視",
        "icon": "✈️",
    },
    "birthday": {
        "label": "誕生日・パーティー",
        "description": "お祝いの瞬間・ケーキ・グループ写真",
        "icon": "🎂",
    },
    "kids": {
        "label": "子どもの成長",
        "description": "子どもの表情・遊び・成長の瞬間",
        "icon": "👶",
    },
    "wedding": {
        "label": "結婚式・記念日",
        "description": "フォーマル・ロマンチックな瞬間",
        "icon": "💒",
    },
    "nature": {
        "label": "自然・風景",
        "description": "景色・季節感・光の美しさを重視",
        "icon": "🌸",
    },
    "food": {
        "label": "グルメ・料理",
        "description": "美しい料理・食事の場面",
        "icon": "🍽️",
    },
    "pets": {
        "label": "ペット",
        "description": "ペットの可愛い瞬間・表情",
        "icon": "🐾",
    },
    "custom": {
        "label": "カスタム",
        "description": "自由にテーマを指定",
        "icon": "✏️",
    },
}


def get_nas_client(req: Any):
    """Return appropriate NAS client based on connection_type."""
    if req.connection_type == "smb":
        return SMBClient(
            host=req.host,
            port=req.port or 445,
            username=req.username or "",
            password=req.password or "",
            share_name=req.share_name or "",
        )
    elif req.connection_type == "sftp":
        return SFTPClient(
            host=req.host,
            port=req.sftp_port or 22,
            username=req.username or "",
            password=req.password or "",
        )
    else:  # local
        return LocalNASClient(base_path=req.base_path or "/")


# ── API Endpoints ────────────────────────────────────────────────────────────

@app.get("/api/album-types")
async def get_album_types():
    """Return available album types."""
    return ALBUM_TYPES


@app.post("/api/nas/test")
async def test_nas_connection(req: NASConnectionRequest):
    """Test NAS connection."""
    try:
        client = get_nas_client(req)
        result = client.test_connection()
        return {"success": True, "message": result}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/nas/browse")
async def browse_nas_folder(req: FolderBrowseRequest):
    """Browse NAS folders."""
    try:
        client = get_nas_client(req)
        items = client.list_directory(req.folder_path)
        return {"path": req.folder_path, "items": items}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/photos/count")
async def count_photos(req: AlbumRequest):
    """Count photos in selected folders with date filter (fast, no AI)."""
    try:
        client = get_nas_client(req)
        processor = PhotoProcessor()

        date_from = date.fromisoformat(req.date_from) if req.date_from else None
        date_to = date.fromisoformat(req.date_to) if req.date_to else None

        total = 0
        for folder in req.source_folders:
            photos = client.list_photos(folder, date_from, date_to, req.include_raw)
            total += len(photos)

        return {"count": total}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/album/create")
async def create_album(req: AlbumRequest, background_tasks: BackgroundTasks):
    """Start async album creation job."""
    import uuid
    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "status": "pending",
        "progress": 0,
        "message": "開始中...",
        "result": None,
        "error": None,
    }
    background_tasks.add_task(_run_album_creation, job_id, req)
    return {"job_id": job_id}


@app.get("/api/album/status/{job_id}")
async def get_job_status(job_id: str):
    """Get status of an album creation job."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    return jobs[job_id]


@app.get("/api/photo/thumbnail/{job_id}/{index}")
async def get_thumbnail(job_id: str, index: int):
    """Serve a thumbnail from a completed job."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    job = jobs[job_id]
    result = job.get("result")
    if not result or "thumbnails" not in result:
        raise HTTPException(status_code=404, detail="No thumbnails")
    thumbnails = result["thumbnails"]
    if index >= len(thumbnails):
        raise HTTPException(status_code=404, detail="Index out of range")
    thumb_path = thumbnails[index]
    if not os.path.exists(thumb_path):
        raise HTTPException(status_code=404, detail="Thumbnail file not found")
    return FileResponse(thumb_path, media_type="image/jpeg")


@app.post("/api/config/save")
async def save_config(config: dict):
    """Save NAS connection config (passwords excluded)."""
    safe_config = {k: v for k, v in config.items() if k != "password"}
    CONFIG_FILE.write_text(json.dumps(safe_config, ensure_ascii=False, indent=2))
    return {"success": True}


@app.get("/api/config/load")
async def load_config():
    """Load saved NAS connection config."""
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}


# ── Background Job ───────────────────────────────────────────────────────────

async def _run_album_creation(job_id: str, req: AlbumRequest):
    """Main album creation workflow."""
    job = jobs[job_id]
    tmp_dir = tempfile.mkdtemp(prefix="nas_album_")

    try:
        # 1. Connect to NAS
        _update_job(job_id, 5, "NASに接続中...")
        client = get_nas_client(req)
        client.test_connection()

        # 2. Collect all photos
        _update_job(job_id, 10, "写真リストを取得中...")
        date_from = date.fromisoformat(req.date_from) if req.date_from else None
        date_to = date.fromisoformat(req.date_to) if req.date_to else None

        all_photos = []
        for folder in req.source_folders:
            photos = client.list_photos(folder, date_from, date_to, req.include_raw)
            all_photos.extend(photos)

        if not all_photos:
            _update_job(job_id, 100, "対象写真が見つかりませんでした",
                        status="error", error="指定した条件に合う写真が見つかりませんでした。")
            return

        _update_job(job_id, 15, f"写真 {len(all_photos)}枚 を検出。品質フィルタリング中...")

        # 3. Download sample thumbnails for processing
        processor = PhotoProcessor()
        thumb_dir = os.path.join(tmp_dir, "thumbs")
        os.makedirs(thumb_dir, exist_ok=True)

        # Download and create thumbnails (batch to avoid memory issues)
        photo_infos = []
        batch_size = 50
        total_batches = (len(all_photos) + batch_size - 1) // batch_size

        for batch_idx in range(total_batches):
            batch = all_photos[batch_idx * batch_size:(batch_idx + 1) * batch_size]
            progress = 15 + int(30 * batch_idx / total_batches)
            _update_job(job_id, progress,
                        f"サムネイル生成中... ({batch_idx * batch_size}/{len(all_photos)}枚)")

            for photo_meta in batch:
                try:
                    img_bytes = client.download_file(photo_meta["path"])
                    info = processor.analyze_photo(img_bytes, photo_meta, thumb_dir)
                    if info:
                        photo_infos.append(info)
                except Exception as e:
                    # Skip unreadable photos
                    pass

        if not photo_infos:
            _update_job(job_id, 100, "処理可能な写真がありませんでした",
                        status="error", error="写真を処理できませんでした。")
            return

        # 4. Pre-filter: remove blurry / duplicates
        _update_job(job_id, 48, f"重複・ぼけ写真を除外中... ({len(photo_infos)}枚)")
        filtered = processor.filter_photos(
            photo_infos,
            min_resolution=req.min_resolution,
        )

        _update_job(job_id, 55, f"フィルタ後 {len(filtered)}枚 → AI選別中...")

        # 5. Claude AI selection
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY が設定されていません。")

        selector = ClaudePhotoSelector(api_key=api_key)
        album_desc = ALBUM_TYPES.get(req.album_type, ALBUM_TYPES["general"])
        selected = await selector.select_photos(
            photos=filtered,
            count=req.photo_count,
            album_type=req.album_type,
            album_label=album_desc["label"],
            album_description=album_desc["description"],
            custom_prompt=req.custom_prompt,
            progress_callback=lambda p, m: _update_job(job_id, 55 + int(30 * p), m),
        )

        _update_job(job_id, 87, f"選別完了 {len(selected)}枚 → アルバムフォルダ作成中...")

        # 6. Create output album folder on NAS
        album_name = req.album_name or f"Album_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        output_path = req.output_folder.rstrip("/") + "/" + album_name if req.output_folder else album_name

        client.create_directory(output_path)

        # 7. Copy selected photos to album folder
        _update_job(job_id, 90, "写真をアルバムフォルダにコピー中...")
        copied = []
        for i, photo in enumerate(selected):
            try:
                img_bytes = client.download_file(photo["path"])
                dest_name = f"{i+1:03d}_{Path(photo['path']).name}"
                dest_path = output_path + "/" + dest_name
                client.upload_file(dest_path, img_bytes)
                copied.append(dest_path)
            except Exception as e:
                pass  # Skip failed copies

        # 8. Generate result info
        thumbnails = [p["thumb_path"] for p in selected if "thumb_path" in p and os.path.exists(p["thumb_path"])]

        result = {
            "album_name": album_name,
            "output_path": output_path,
            "total_scanned": len(all_photos),
            "after_filter": len(filtered),
            "selected_count": len(selected),
            "copied_count": len(copied),
            "thumbnails": thumbnails,
            "selected_photos": [
                {
                    "path": p["path"],
                    "filename": Path(p["path"]).name,
                    "score": p.get("score", 0),
                    "reason": p.get("reason", ""),
                    "date": p.get("date", ""),
                }
                for p in selected
            ],
        }

        _update_job(job_id, 100, f"完了! {len(copied)}枚をアルバムに保存しました。",
                    status="completed", result=result)

    except Exception as e:
        import traceback
        _update_job(job_id, 100, f"エラー: {str(e)}",
                    status="error", error=str(e))
    finally:
        # Clean up tmp files (keep thumbnails for preview)
        pass  # thumbnails are kept until next run


def _update_job(job_id: str, progress: int, message: str,
                status: str = "running", result=None, error=None):
    if job_id not in jobs:
        return
    jobs[job_id].update({
        "progress": progress,
        "message": message,
        "status": status,
    })
    if result is not None:
        jobs[job_id]["result"] = result
    if error is not None:
        jobs[job_id]["error"] = error


# ── Static Files ─────────────────────────────────────────────────────────────

frontend_path = Path(__file__).parent.parent / "frontend"
if frontend_path.exists():
    app.mount("/static", StaticFiles(directory=str(frontend_path)), name="static")

    @app.get("/")
    async def serve_index():
        return FileResponse(str(frontend_path / "index.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)
