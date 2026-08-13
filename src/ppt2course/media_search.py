"""Optional Pexels photo/video search with normalized, non-fatal results."""

from dataclasses import dataclass
import json
import os
from typing import Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ppt2course.timeline_models import VisualAsset, VisualAssetType

PEXELS_API_KEY_ENV = "PEXELS_API_KEY"
PEXELS_API_BASE = "https://api.pexels.com"
DEFAULT_CANDIDATE_LIMIT = 6


@dataclass(frozen=True)
class MediaSearchResult:
    assets: tuple[VisualAsset, ...] = ()
    warning: str | None = None


def _photo_asset(row: dict, keyword: str, slide_number: int) -> VisualAsset | None:
    src = row.get("src") or {}
    preview = src.get("medium") or src.get("small")
    download = src.get("large2x") or src.get("large") or src.get("original")
    if not preview or not download:
        return None
    return VisualAsset(
        source="pexels", asset_type=VisualAssetType.IMAGE,
        preview_url=preview, download_url=download,
        photographer=str(row.get("photographer") or ""),
        keyword=keyword, slide_number=slide_number,
    )


def _video_asset(row: dict, keyword: str, slide_number: int) -> VisualAsset | None:
    pictures = row.get("video_pictures") or []
    files = [item for item in (row.get("video_files") or []) if item.get("link")]
    preview = str(pictures[0].get("picture") or "") if pictures else ""
    if not preview or not files:
        return None
    preferred = sorted(
        files,
        key=lambda item: (
            item.get("file_type") != "video/mp4",
            abs((item.get("width") or 1920) - 1920),
        ),
    )[0]
    return VisualAsset(
        source="pexels", asset_type=VisualAssetType.VIDEO,
        preview_url=preview, download_url=str(preferred["link"]),
        photographer=str((row.get("user") or {}).get("name") or ""),
        keyword=keyword, slide_number=slide_number,
    )


def search_pexels(
    keyword: str,
    slide_number: int,
    media_type: VisualAssetType = VisualAssetType.IMAGE,
    api_key: str | None = None,
    limit: int = DEFAULT_CANDIDATE_LIMIT,
    timeout_seconds: float = 10.0,
    opener: Callable = urlopen,
) -> MediaSearchResult:
    """Return UI-ready candidates; convert every provider failure to a warning."""
    key = (api_key or os.environ.get(PEXELS_API_KEY_ENV, "")).strip()
    if not key:
        return MediaSearchResult(warning="Pexels API Key 未設定，無法搜尋免費素材。")
    if not keyword.strip():
        return MediaSearchResult(warning="Pexels 搜尋關鍵字不可為空。")
    if slide_number < 1:
        return MediaSearchResult(warning="Pexels 搜尋的投影片頁碼無效。")
    limit = max(1, min(DEFAULT_CANDIDATE_LIMIT, int(limit)))
    endpoint = "videos/search" if media_type is VisualAssetType.VIDEO else "v1/search"
    url = f"{PEXELS_API_BASE}/{endpoint}?{urlencode({'query': keyword, 'per_page': limit})}"
    request = Request(url, headers={"Authorization": key, "User-Agent": "PPT2Course-AI/0.1"})
    try:
        with opener(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        rows = payload.get("videos" if media_type is VisualAssetType.VIDEO else "photos", [])
        if not isinstance(rows, list):
            raise ValueError("Pexels response items must be a list")
        converter = _video_asset if media_type is VisualAssetType.VIDEO else _photo_asset
        assets = tuple(
            asset
            for row in rows[:limit]
            if isinstance(row, dict)
            and (asset := converter(row, keyword, slide_number)) is not None
        )
        if not assets:
            return MediaSearchResult(warning=f"Pexels 找不到「{keyword}」的可用素材。")
        return MediaSearchResult(assets=assets)
    except Exception as exc:
        # This feature is optional: networking, HTTP, timeout and malformed
        # provider data must all leave the ordinary PPT video path available.
        return MediaSearchResult(warning=f"Pexels 搜尋失敗：{exc}")
