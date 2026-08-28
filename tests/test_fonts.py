import os
from unittest.mock import patch

from ppt2course.fonts import _FONT_FILES, _resolved_dir_cache, ensure_subtitle_font_dir


def setup_function():
    # Each test gets a clean slate regardless of what an earlier test (or an
    # earlier real compose_video() call elsewhere in the suite) already
    # resolved and cached for a given directory.
    _resolved_dir_cache.clear()


def test_ensure_subtitle_font_dir_reuses_already_present_files(tmp_path):
    # Pre-seed the cache dir as if a previous run already downloaded
    # everything -- must not touch the network at all in that case.
    for filename in _FONT_FILES:
        (tmp_path / filename).write_bytes(b"fake-font-bytes")

    with patch("ppt2course.fonts.urllib.request.urlopen") as mock_urlopen:
        result = ensure_subtitle_font_dir(str(tmp_path))

    assert result == str(tmp_path)
    mock_urlopen.assert_not_called()


def test_ensure_subtitle_font_dir_returns_none_when_download_fails(tmp_path):
    cache_dir = str(tmp_path / "fonts")

    with patch("ppt2course.fonts.urllib.request.urlopen", side_effect=OSError("no network")):
        result = ensure_subtitle_font_dir(cache_dir)

    assert result is None
    # A failed attempt shouldn't leave a half-written file behind.
    assert os.listdir(cache_dir) == []


def test_ensure_subtitle_font_dir_memoizes_within_the_process(tmp_path):
    cache_dir = str(tmp_path / "fonts")

    with patch(
        "ppt2course.fonts.urllib.request.urlopen", side_effect=OSError("no network")
    ) as mock_urlopen:
        first = ensure_subtitle_font_dir(cache_dir)
        second = ensure_subtitle_font_dir(cache_dir)

    assert first is None
    assert second is None
    # Second call hit the memoized result, not the network again.
    assert mock_urlopen.call_count == len(_FONT_FILES)
