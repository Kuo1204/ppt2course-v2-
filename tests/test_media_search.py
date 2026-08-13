import json
from unittest.mock import patch

from ppt2course.media_search import search_pexels
from ppt2course.timeline_models import VisualAssetType


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def test_6_missing_key_is_nonfatal_and_does_not_call_network():
    with patch.dict("os.environ", {}, clear=True):
        result = search_pexels("office", 1, opener=lambda *args, **kwargs: 1 / 0)
    assert result.assets == ()
    assert "API Key" in result.warning


def test_photo_results_are_normalized_and_limited_to_six():
    rows = [
        {
            "photographer": f"Person {i}",
            "src": {"medium": f"https://preview/{i}", "large2x": f"https://full/{i}"},
        }
        for i in range(8)
    ]
    captured = {}

    def opener(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse({"photos": rows})

    result = search_pexels("employee stress", 5, api_key="secret", limit=99, opener=opener)
    assert len(result.assets) == 6
    assert result.assets[0].source == "pexels"
    assert result.assets[0].slide_number == 5
    assert result.assets[0].keyword == "employee stress"
    assert captured["request"].headers["Authorization"] == "secret"
    assert "secret" not in captured["request"].full_url


def test_video_results_choose_an_mp4_and_preserve_preview():
    payload = {
        "videos": [{
            "user": {"name": "Creator"},
            "video_pictures": [{"picture": "https://preview/video"}],
            "video_files": [
                {"file_type": "video/webm", "width": 1920, "link": "https://video/webm"},
                {"file_type": "video/mp4", "width": 1280, "link": "https://video/mp4"},
            ],
        }]
    }
    result = search_pexels(
        "office", 2, VisualAssetType.VIDEO, api_key="key",
        opener=lambda *args, **kwargs: FakeResponse(payload),
    )
    assert result.assets[0].asset_type is VisualAssetType.VIDEO
    assert result.assets[0].download_url == "https://video/mp4"
    assert result.assets[0].photographer == "Creator"


def test_provider_failure_returns_warning_and_no_assets():
    def failed(*args, **kwargs):
        raise TimeoutError("timed out")

    result = search_pexels("office", 1, api_key="key", opener=failed)
    assert result.assets == ()
    assert "timed out" in result.warning


def test_malformed_or_empty_provider_results_are_nonfatal():
    malformed = search_pexels(
        "office", 1, api_key="key",
        opener=lambda *args, **kwargs: FakeResponse({"photos": "not-a-list"}),
    )
    empty = search_pexels(
        "office", 1, api_key="key",
        opener=lambda *args, **kwargs: FakeResponse({"photos": []}),
    )
    assert malformed.warning
    assert empty.warning
