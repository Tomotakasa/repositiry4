"""
Local CLIP-based photo scorer for pre-filtering before Claude API.

Uses CLIP ViT-B/32 to compute:
  1. Semantic relevance  - how well the photo matches the album theme
  2. Aesthetic quality   - penalizes blurry / dark / low-quality photos

Models are ~340 MB and downloaded on first use via HuggingFace hub
(cached in ~/.cache/huggingface after the initial download).

Requires optional dependencies:
    pip install torch transformers

Set LOCAL_SCORER=false in .env to disable without uninstalling the packages.
"""

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Album-type text prompts
# ---------------------------------------------------------------------------

# Positive prompts: images that match these descriptions get higher scores
ALBUM_TEXT_PROMPTS: Dict[str, List[str]] = {
    "general":  [
        "a beautiful well-composed photograph",
        "sharp clear high quality photo",
    ],
    "family": [
        "a happy family photo",
        "family smiling together",
        "parents and children together",
    ],
    "travel": [
        "a travel vacation photo",
        "tourist sightseeing destination",
        "scenic travel landscape",
    ],
    "birthday": [
        "birthday party celebration",
        "birthday cake with candles",
        "people celebrating together",
    ],
    "kids": [
        "cute child playing",
        "happy children laughing",
        "kids portrait",
    ],
    "wedding": [
        "wedding ceremony photo",
        "bride and groom",
        "wedding celebration",
    ],
    "nature": [
        "beautiful nature landscape",
        "scenic outdoor photography",
        "stunning natural scenery",
    ],
    "food": [
        "beautiful food photography",
        "delicious appetizing meal",
        "food plating presentation",
    ],
    "pets": [
        "cute pet portrait",
        "adorable dog or cat",
        "pet animal photo",
    ],
    "custom": [
        "a beautiful high quality photo",
    ],
}

# Negative prompts: penalise images that resemble these
NEGATIVE_PROMPTS = [
    "a blurry out of focus photo",
    "a dark underexposed photo",
    "a low quality bad photo",
]

POSITIVE_WEIGHT = 1.0
NEGATIVE_WEIGHT = 0.4

# Normalisation constants (CLIP cosine-sim typically ranges ~0.20-0.35)
_NORM_OFFSET = 0.15
_NORM_SCALE = 0.30


class LocalPhotoScorer:
    """CLIP-based photo scorer that runs entirely on the local machine.

    Usage::

        scorer = LocalPhotoScorer()
        if scorer.is_available():
            photos = scorer.score_photos(photos, album_type="family")
    """

    def __init__(self) -> None:
        self._processor = None
        self._model = None
        self._device = "cpu"
        self._available: Optional[bool] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Return True if torch + transformers are installed and enabled."""
        if self._available is not None:
            return self._available

        if os.getenv("LOCAL_SCORER", "true").lower() in ("false", "0", "no"):
            self._available = False
            logger.info("Local CLIP scorer disabled via LOCAL_SCORER env var.")
            return False

        try:
            import torch  # noqa: F401
            from transformers import CLIPModel, CLIPProcessor  # noqa: F401
            self._available = True
        except ImportError:
            self._available = False
            logger.info(
                "Local CLIP scoring unavailable "
                "(torch/transformers not installed). "
                "Install with: pip install torch transformers"
            )

        return self._available

    def score_photos(
        self,
        photos: List[Dict[str, Any]],
        album_type: str,
        custom_prompt: Optional[str] = None,
        batch_size: int = 16,
    ) -> List[Dict[str, Any]]:
        """Score *photos* locally and add ``local_clip_score`` (0-10) to each.

        Returns the list sorted by ``local_clip_score`` descending.
        Falls back to returning the original list unchanged if CLIP is
        unavailable or encounters an error.
        """
        if not self.is_available() or not photos:
            return photos

        try:
            self._load_model()
        except Exception as exc:
            logger.warning("Failed to load CLIP model: %s", exc)
            return photos

        # Build combined text prompts
        positive = list(ALBUM_TEXT_PROMPTS.get(album_type, ALBUM_TEXT_PROMPTS["general"]))
        if custom_prompt:
            positive.insert(0, custom_prompt)
        all_texts = positive + NEGATIVE_PROMPTS
        pos_count = len(positive)

        try:
            text_features = self._encode_texts(all_texts)
        except Exception as exc:
            logger.warning("CLIP text encoding failed: %s", exc)
            return photos

        # Score photos in batches
        for i in range(0, len(photos), batch_size):
            batch = photos[i: i + batch_size]
            try:
                self._score_batch(batch, text_features, pos_count)
            except Exception as exc:
                logger.warning("CLIP batch %d scoring failed: %s", i // batch_size, exc)
                for p in batch:
                    if "local_clip_score" not in p:
                        p["local_clip_score"] = p.get("quality_score", 50) / 10.0

        # Ensure every photo has a score
        for p in photos:
            if "local_clip_score" not in p:
                p["local_clip_score"] = p.get("quality_score", 50) / 10.0

        photos.sort(key=lambda x: x.get("local_clip_score", 0.0), reverse=True)
        return photos

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_model(self) -> None:
        """Lazy-load CLIP ViT-B/32 (~340 MB, cached after first download)."""
        if self._model is not None:
            return

        import torch
        from transformers import CLIPModel, CLIPProcessor

        model_id = "openai/clip-vit-base-patch32"
        logger.info("Loading CLIP model %s ...", model_id)
        self._processor = CLIPProcessor.from_pretrained(model_id)
        self._model = CLIPModel.from_pretrained(model_id)
        self._device = "cuda" if torch.cuda.is_available() else "cpu"
        self._model = self._model.to(self._device)
        self._model.eval()
        logger.info("CLIP model ready on %s", self._device)

    def _encode_texts(self, texts: List[str]):
        """Return L2-normalised text feature vectors [N, D]."""
        import torch

        inputs = self._processor(
            text=texts,
            return_tensors="pt",
            padding=True,
            truncation=True,
        )
        inputs = {k: v.to(self._device) for k, v in inputs.items()}
        with torch.no_grad():
            features = self._model.get_text_features(**inputs)
            features = features / features.norm(dim=-1, keepdim=True)
        return features

    def _score_batch(
        self,
        batch: List[Dict[str, Any]],
        text_features,
        pos_count: int,
    ) -> None:
        """Compute CLIP scores for one batch; sets ``local_clip_score`` in place."""
        import torch
        from PIL import Image

        images: List[Any] = []
        valid_indices: List[int] = []

        for idx, photo in enumerate(batch):
            thumb = photo.get("thumb_path", "")
            if thumb and os.path.exists(thumb):
                try:
                    images.append(Image.open(thumb).convert("RGB"))
                    valid_indices.append(idx)
                except Exception:
                    pass

        if not images:
            return

        inputs = self._processor(images=images, return_tensors="pt")
        inputs = {k: v.to(self._device) for k, v in inputs.items()}

        with torch.no_grad():
            img_features = self._model.get_image_features(**inputs)
            img_features = img_features / img_features.norm(dim=-1, keepdim=True)

        # Cosine similarity matrix [num_images, num_texts]
        similarity = (img_features @ text_features.T).cpu().numpy()

        for local_i, photo_idx in enumerate(valid_indices):
            pos_sim = float(similarity[local_i, :pos_count].mean())
            neg_sim = float(similarity[local_i, pos_count:].mean())
            raw = pos_sim * POSITIVE_WEIGHT - neg_sim * NEGATIVE_WEIGHT
            # Map to 0-10
            score = (raw + _NORM_OFFSET) / _NORM_SCALE * 10.0
            score = max(0.0, min(10.0, score))
            batch[photo_idx]["local_clip_score"] = round(score, 2)
