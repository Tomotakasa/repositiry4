"""
Photo processor: EXIF extraction, blur detection, duplicate detection,
thumbnail generation, quality scoring.

All processing is done locally (no API calls) to minimize Claude API usage.
"""

import hashlib
import io
import os
from datetime import date, datetime
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

from PIL import Image, ExifTags
import piexif

# Optional: numpy for faster perceptual hash
try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False


THUMB_SIZE = (480, 480)          # max thumbnail size for AI analysis
BLUR_THRESHOLD = 80.0            # Laplacian variance below this = blurry
DUPLICATE_THRESHOLD = 8          # hash distance for near-duplicate detection
AI_THUMB_SIZE = (400, 400)       # size sent to Claude (keep tokens low)


class PhotoProcessor:
    """Handles all local photo processing before AI selection."""

    def analyze_photo(
        self,
        img_bytes: bytes,
        meta: Dict[str, Any],
        thumb_dir: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Analyze a single photo:
        - Extract EXIF date
        - Generate thumbnail
        - Calculate blur score
        - Calculate perceptual hash for dedup
        Returns info dict or None if photo cannot be processed.
        """
        try:
            img = Image.open(io.BytesIO(img_bytes))
            img = _fix_orientation(img)

            width, height = img.size
            if width < 100 or height < 100:
                return None  # Too small

            # Extract date
            photo_date = _extract_date(img, meta)

            # Blur score (higher = sharper)
            blur_score = _compute_blur_score(img)

            # Perceptual hash for duplicate detection
            phash = _perceptual_hash(img)

            # Brightness / exposure quality (0-1)
            brightness_score = _compute_brightness_score(img)

            # Generate thumbnail
            thumb_path = _save_thumbnail(img, meta["path"], thumb_dir)

            # Composite quality score (0-100, used for pre-ranking)
            quality_score = _composite_quality(blur_score, brightness_score, width, height)

            return {
                "path": meta["path"],
                "filename": meta.get("filename", Path(meta["path"]).name),
                "date": photo_date,
                "width": width,
                "height": height,
                "blur_score": blur_score,
                "brightness_score": brightness_score,
                "quality_score": quality_score,
                "phash": phash,
                "thumb_path": thumb_path,
                "size": meta.get("size", len(img_bytes)),
            }
        except Exception:
            return None

    def filter_photos(
        self,
        photos: List[Dict[str, Any]],
        min_resolution: Optional[int] = None,
        max_blur_threshold: float = BLUR_THRESHOLD,
    ) -> List[Dict[str, Any]]:
        """
        Filter out:
        1. Blurry photos (below blur threshold)
        2. Near-duplicates (keep highest quality)
        3. Under/over-exposed photos
        4. Photos below minimum resolution
        Returns filtered, quality-sorted list.
        """
        filtered = []
        for p in photos:
            # Resolution filter
            if min_resolution:
                if p["width"] < min_resolution and p["height"] < min_resolution:
                    continue

            # Blur filter (skip screenshots/diagrams which may be "sharp" with 0 blur)
            # Only filter obviously blurry photos
            if p["blur_score"] < max_blur_threshold * 0.3:
                continue

            filtered.append(p)

        # Remove near-duplicates (keep best quality from each group)
        filtered = _remove_duplicates(filtered)

        # Sort by quality score descending
        filtered.sort(key=lambda x: x["quality_score"], reverse=True)
        return filtered


# ── Image Analysis Helpers ───────────────────────────────────────────────────

def _fix_orientation(img: Image.Image) -> Image.Image:
    """Rotate image according to EXIF orientation tag."""
    try:
        exif = img._getexif()
        if not exif:
            return img
        orient_tag = next(
            (k for k, v in ExifTags.TAGS.items() if v == "Orientation"), None
        )
        if orient_tag and orient_tag in exif:
            orientation = exif[orient_tag]
            rotations = {3: 180, 6: 270, 8: 90}
            if orientation in rotations:
                img = img.rotate(rotations[orientation], expand=True)
    except Exception:
        pass
    return img


def _extract_date(img: Image.Image, meta: Dict[str, Any]) -> str:
    """Extract photo date from EXIF, fallback to file metadata."""
    try:
        exif = img._getexif()
        if exif:
            for tag_id, value in exif.items():
                tag = ExifTags.TAGS.get(tag_id, "")
                if tag in ("DateTimeOriginal", "DateTimeDigitized", "DateTime"):
                    dt = datetime.strptime(str(value), "%Y:%m:%d %H:%M:%S")
                    return dt.date().isoformat()
    except Exception:
        pass
    # Fallback to file date from metadata
    return meta.get("date", "")


def _compute_blur_score(img: Image.Image) -> float:
    """
    Compute sharpness using Laplacian variance.
    Higher value = sharper image.
    Works without OpenCV using pure PIL.
    """
    try:
        gray = img.convert("L").resize((256, 256))
        pixels = list(gray.getdata())
        w, h = gray.size

        # Laplacian kernel: [0,1,0],[1,-4,1],[0,1,0]
        total = 0.0
        count = 0
        for y in range(1, h - 1):
            for x in range(1, w - 1):
                lap = (
                    pixels[(y - 1) * w + x]
                    + pixels[(y + 1) * w + x]
                    + pixels[y * w + (x - 1)]
                    + pixels[y * w + (x + 1)]
                    - 4 * pixels[y * w + x]
                )
                total += lap * lap
                count += 1

        return (total / count) if count > 0 else 0.0
    except Exception:
        return 100.0  # Assume sharp if can't compute


def _compute_brightness_score(img: Image.Image) -> float:
    """
    Score brightness quality (0-1).
    Penalize very dark (<0.15) or very bright (>0.9) images.
    """
    try:
        gray = img.convert("L").resize((64, 64))
        pixels = list(gray.getdata())
        avg = sum(pixels) / len(pixels) / 255.0
        # Optimal range: 0.3 - 0.8
        if avg < 0.15 or avg > 0.95:
            return 0.3
        elif avg < 0.3:
            return 0.6 + avg
        elif avg > 0.8:
            return 1.0 - (avg - 0.8) * 2
        return 1.0
    except Exception:
        return 0.7


def _perceptual_hash(img: Image.Image, hash_size: int = 16) -> str:
    """
    Compute perceptual hash (dHash) for duplicate detection.
    Pure PIL implementation.
    """
    try:
        small = img.convert("L").resize((hash_size + 1, hash_size), Image.LANCZOS)
        pixels = list(small.getdata())
        bits = []
        for y in range(hash_size):
            for x in range(hash_size):
                left = pixels[y * (hash_size + 1) + x]
                right = pixels[y * (hash_size + 1) + x + 1]
                bits.append("1" if left > right else "0")
        bit_str = "".join(bits)
        # Convert to hex
        hex_hash = hex(int(bit_str, 2))[2:].zfill(hash_size * hash_size // 4)
        return hex_hash
    except Exception:
        return hashlib.md5(img.tobytes()[:1024]).hexdigest()


def _hamming_distance(h1: str, h2: str) -> int:
    """Compute Hamming distance between two hex hashes."""
    try:
        b1 = bin(int(h1, 16))[2:].zfill(len(h1) * 4)
        b2 = bin(int(h2, 16))[2:].zfill(len(h2) * 4)
        if len(b1) != len(b2):
            return 999
        return sum(c1 != c2 for c1, c2 in zip(b1, b2))
    except Exception:
        return 999


def _remove_duplicates(photos: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Remove near-duplicate photos.
    For each duplicate group, keep the highest quality photo.
    """
    if not photos:
        return []

    # Sort by quality descending so we keep the best
    sorted_photos = sorted(photos, key=lambda x: x["quality_score"], reverse=True)
    kept = []
    kept_hashes = []

    for photo in sorted_photos:
        phash = photo.get("phash", "")
        is_dup = False
        for existing_hash in kept_hashes:
            if phash and existing_hash:
                dist = _hamming_distance(phash, existing_hash)
                if dist <= DUPLICATE_THRESHOLD:
                    is_dup = True
                    break
        if not is_dup:
            kept.append(photo)
            kept_hashes.append(phash)

    return kept


def _composite_quality(blur_score: float, brightness: float,
                       width: int, height: int) -> float:
    """Compute composite quality score (0-100)."""
    # Blur component (0-50): log scale to handle wide range
    import math
    blur_component = min(50, math.log1p(blur_score) * 5)

    # Brightness component (0-20)
    bright_component = brightness * 20

    # Resolution component (0-20): prefer higher res, diminishing returns
    megapixels = (width * height) / 1_000_000
    res_component = min(20, megapixels * 5)

    # Aspect ratio component (0-10): prefer common photo ratios
    ratio = width / height if height > 0 else 1
    common_ratios = [4/3, 3/2, 16/9, 1.0, 3/4, 2/3]
    ratio_diff = min(abs(ratio - r) for r in common_ratios)
    ratio_component = max(0, 10 - ratio_diff * 10)

    return blur_component + bright_component + res_component + ratio_component


def _save_thumbnail(img: Image.Image, photo_path: str, thumb_dir: str) -> str:
    """Save a small thumbnail for AI analysis."""
    # Create unique filename from path hash
    path_hash = hashlib.md5(photo_path.encode()).hexdigest()[:12]
    thumb_path = os.path.join(thumb_dir, f"{path_hash}.jpg")

    if os.path.exists(thumb_path):
        return thumb_path

    thumb = img.copy()
    thumb.thumbnail(AI_THUMB_SIZE, Image.LANCZOS)

    # Convert to RGB for JPEG
    if thumb.mode not in ("RGB", "L"):
        thumb = thumb.convert("RGB")
    elif thumb.mode == "L":
        thumb = thumb.convert("RGB")

    thumb.save(thumb_path, "JPEG", quality=75, optimize=True)
    return thumb_path


def get_thumbnail_base64(thumb_path: str) -> str:
    """Read thumbnail and encode as base64 for Claude API."""
    import base64
    with open(thumb_path, "rb") as f:
        return base64.standard_b64encode(f.read()).decode("utf-8")
