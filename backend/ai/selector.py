"""
Claude AI Photo Selector.

Strategy to minimize API consumption:
1. Pre-filter photos locally (blur, duplicates, quality) - NO API calls
2. Take top N candidates based on local quality score (N = photo_count * 3)
3. Split candidates into batches of up to 8 images per API call
4. Each Claude call evaluates a batch and scores/ranks photos (1-10)
5. Final selection = top `photo_count` photos by AI score
6. Use claude-haiku (cheapest) for batch scoring, use claude-sonnet only for
   optional final curation pass if enabled
7. Cache results by photo hash to avoid re-processing
"""

import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
from typing import List, Dict, Any, Callable, Optional

import anthropic

# Tuning constants
BATCH_SIZE = 8          # images per Claude API call (keeps token count low)
CANDIDATE_MULTIPLIER = 2  # select top N*2 locally before sending to Claude
MAX_CANDIDATES = 1500   # hard cap; auto-adjusted to max(count*2, 80) at runtime
MODEL = "claude-haiku-4-5-20251001"  # cheapest model; sufficient for photo ranking


ALBUM_PROMPTS = {
    "general": "最高品質の写真を選んでください。構図・明るさ・シャープネス・色彩バランスを重視してください。",
    "family": "家族の温かい瞬間、自然な笑顔、感情的な瞬間を優先してください。グループ写真と個人の表情を大切にしてください。",
    "travel": "旅行の記念になる写真を選んでください。観光地・景色・体験・地元文化を捉えた写真を優先してください。",
    "birthday": "誕生日やお祝いの雰囲気を伝える写真を選んでください。ケーキ・プレゼント・みんなの笑顔・特別な瞬間を優先してください。",
    "kids": "子どもの自然な表情・遊び・成長の瞬間を選んでください。生き生きとした表情と動きのある写真を優先してください。",
    "wedding": "結婚式の感動的な瞬間を選んでください。誓いの場面・感情的な瞬間・フォーマルなポーズ・美しい装飾を優先してください。",
    "nature": "自然の美しさを伝える写真を選んでください。景色・光・季節感・野生動物・植物の美しさを優先してください。",
    "food": "美しく撮れた料理・食事の写真を選んでください。色鮮やかで食欲をそそる写真を優先してください。",
    "pets": "ペットの可愛い・面白い・感動的な瞬間を選んでください。表情・動き・ペットらしさを重視してください。",
    "custom": "",  # filled by custom_prompt
}


class ClaudePhotoSelector:
    def __init__(self, api_key: str):
        self.client = anthropic.Anthropic(api_key=api_key)
        self._score_cache: Dict[str, float] = {}

    async def select_photos(
        self,
        photos: List[Dict[str, Any]],
        count: int,
        album_type: str,
        album_label: str,
        album_description: str,
        custom_prompt: Optional[str] = None,
        progress_callback: Optional[Callable] = None,
    ) -> List[Dict[str, Any]]:
        """
        Select best `count` photos using Claude AI.
        Returns photos sorted by AI score (best first).
        """
        if not photos:
            return []

        # Step 1: Select candidates (N*2 but at least count+20, hard cap at MAX_CANDIDATES)
        max_candidates = min(len(photos), max(count * CANDIDATE_MULTIPLIER, count + 20))
        max_candidates = min(max_candidates, MAX_CANDIDATES)
        # Always ensure we have at least `count` candidates
        max_candidates = max(max_candidates, min(count, len(photos)))
        candidates = photos[:max_candidates]  # Already sorted by quality_score

        # Build album-specific prompt
        album_prompt = ALBUM_PROMPTS.get(album_type, ALBUM_PROMPTS["general"])
        if album_type == "custom" and custom_prompt:
            album_prompt = custom_prompt
        elif custom_prompt:
            album_prompt = album_prompt + " 追加指示: " + custom_prompt

        # Step 2: Score candidates in batches
        if progress_callback:
            await asyncio.get_event_loop().run_in_executor(
                None, progress_callback, 0.0, f"AI評価中... (0/{len(candidates)}枚)")

        scored = []
        batches = [candidates[i:i + BATCH_SIZE] for i in range(0, len(candidates), BATCH_SIZE)]

        for batch_idx, batch in enumerate(batches):
            batch_scored = await self._score_batch(batch, album_type, album_label, album_prompt)
            scored.extend(batch_scored)

            if progress_callback:
                progress = (batch_idx + 1) / len(batches)
                done = min((batch_idx + 1) * BATCH_SIZE, len(candidates))
                await asyncio.get_event_loop().run_in_executor(
                    None, progress_callback, progress,
                    f"AI評価中... ({done}/{len(candidates)}枚)")

            # Small delay to respect rate limits
            if batch_idx < len(batches) - 1:
                await asyncio.sleep(0.3)

        # Step 3: Sort by AI score and return top `count`
        scored.sort(key=lambda x: x.get("ai_score", 0), reverse=True)
        selected = scored[:count]

        # Sort final selection by date for album order
        selected.sort(key=lambda x: x.get("date", ""))
        return selected

    async def _score_batch(
        self,
        batch: List[Dict[str, Any]],
        album_type: str,
        album_label: str,
        album_prompt: str,
    ) -> List[Dict[str, Any]]:
        """
        Score a batch of photos using a single Claude API call.
        Uses vision to analyze thumbnails.
        Returns photos with `ai_score` (1-10) and `reason` fields added.
        """
        # Check cache
        uncached = []
        for photo in batch:
            cache_key = _photo_cache_key(photo)
            if cache_key in self._score_cache:
                photo["ai_score"] = self._score_cache[cache_key]
            else:
                uncached.append(photo)

        if not uncached:
            return batch

        # Build message content with all thumbnails
        content = []
        valid_photos = []

        for i, photo in enumerate(uncached):
            thumb_path = photo.get("thumb_path", "")
            if not thumb_path or not os.path.exists(thumb_path):
                photo["ai_score"] = photo.get("quality_score", 50) / 100 * 7
                photo["reason"] = "サムネイル未生成"
                continue

            try:
                with open(thumb_path, "rb") as f:
                    img_data = base64.standard_b64encode(f.read()).decode("utf-8")
                content.append({
                    "type": "text",
                    "text": f"写真{i+1}: {photo.get('filename', '')} (撮影日: {photo.get('date', '不明')})"
                })
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": img_data,
                    }
                })
                valid_photos.append((i, photo))
            except Exception:
                photo["ai_score"] = 5.0
                photo["reason"] = "評価エラー"

        if not valid_photos:
            return batch

        # Build scoring instruction
        photo_list = ", ".join([f"写真{i+1}" for i, _ in valid_photos])
        instruction = f"""あなたはプロのフォトキュレーターです。
アルバムテーマ: 「{album_label}」
選別基準: {album_prompt}

上記の{len(valid_photos)}枚の写真({photo_list})を評価してください。

各写真に1〜10点のスコアを付けてください:
- 10点: このアルバムに絶対入れたい最高の写真
- 7-9点: アルバムに適した良い写真
- 4-6点: 普通の写真
- 1-3点: ぼけ・暗すぎ・テーマと不一致など

必ず以下のJSON形式のみで回答してください(他のテキスト不要):
{{"scores": [{{"photo": 1, "score": 8, "reason": "理由"}}, ...]}}"""

        content.append({"type": "text", "text": instruction})

        try:
            response = self.client.messages.create(
                model=MODEL,
                max_tokens=512,
                messages=[{"role": "user", "content": content}],
            )

            raw = response.content[0].text.strip()
            # Extract JSON even if wrapped in markdown
            if "```" in raw:
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            scores_data = json.loads(raw)
            scores_list = scores_data.get("scores", [])

            for item in scores_list:
                photo_num = item.get("photo", 0) - 1
                score = float(item.get("score", 5))
                reason = item.get("reason", "")
                if 0 <= photo_num < len(valid_photos):
                    _, photo = valid_photos[photo_num]
                    photo["ai_score"] = score
                    photo["reason"] = reason
                    # Cache the score
                    self._score_cache[_photo_cache_key(photo)] = score

        except Exception as e:
            # Fallback: use local quality score
            for _, photo in valid_photos:
                if "ai_score" not in photo:
                    photo["ai_score"] = photo.get("quality_score", 50) / 100 * 7
                    photo["reason"] = "AI評価失敗(品質スコアで代替)"

        # Ensure all photos in batch have a score
        for photo in batch:
            if "ai_score" not in photo:
                photo["ai_score"] = photo.get("quality_score", 50) / 100 * 7
                photo["reason"] = ""

        return batch


def _photo_cache_key(photo: Dict[str, Any]) -> str:
    """Generate cache key from photo path and quality score."""
    return hashlib.md5(
        f"{photo.get('path', '')}{photo.get('blur_score', 0):.1f}".encode()
    ).hexdigest()
