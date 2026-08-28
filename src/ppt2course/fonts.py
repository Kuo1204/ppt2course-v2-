"""Subtitle CJK font bootstrap.

The runtime image (see ``Dockerfile``) is ``python:3.12-slim`` with *no*
`apt-get install fonts-noto-cjk` step -- this platform's build servers can't
reach `deb.debian.org` at all, the same reachability gap that already made
``ffmpeg`` itself get fetched via ``static-ffmpeg`` instead of apt. The image
therefore ships with zero font files, not just the wrong one: without an
actual file backing the subtitle style's ``FontName=Noto Sans CJK TC`` (see
``video.SUBTITLE_FONT_NAME``), libass has nothing to substitute and burns in
*no* glyphs at all. ffmpeg still exits 0 and the video still renders --
there's just never any subtitle text on screen, in every deployed video,
which is easy to mistake for a per-job bug rather than an environment gap.

This mirrors the fix already in place for ffmpeg: the build server can't
reach arbitrary hosts, but the *running* container can (see
``static_ffmpeg.add_paths()`` in server.py), so the font is fetched once,
lazily, at first real use instead of during the image build. ffmpeg's
``subtitles`` filter takes a ``fontsdir=<dir>`` option (alongside
``force_style=``) that makes libass scan a directory directly by each font
file's own embedded family name -- independent of system fontconfig, which
this image never installs either.
"""

import logging
import os
import tempfile
import urllib.request

logger = logging.getLogger(__name__)

# Static (non-variable-weight) OTF builds of exactly the family named in
# video.SUBTITLE_FONT_NAME, from the Noto CJK project's own release repo.
# Deliberately not the variable-font build fonts.google.com/GitHub's
# `google/fonts` repo serves for "Noto Sans TC" -- whether the FreeType
# linked into static-ffmpeg's libass build supports OpenType variable fonts
# is unverified, while these are plain, unambiguous static faces. Both
# Regular and Bold are fetched so the existing subtitle_bold toggle renders
# an actual bold face rather than depending on libass's own synthetic-bold
# fallback.
_FONT_BASE_URL = (
    "https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans/OTF/TraditionalChinese/"
)
_FONT_FILES = ("NotoSansCJKtc-Regular.otf", "NotoSansCJKtc-Bold.otf")

# A fresh temp dir per container boot means one ~30MB re-download the first
# time a video is composed after each restart -- acceptable (same shape as
# static-ffmpeg's own first-boot fetch) and avoidable by pointing this at
# the platform's persistent volume instead, e.g.
# PPT2COURSE_FONT_CACHE_DIR=/opt/appdata/fonts.
DEFAULT_FONT_CACHE_DIR = os.environ.get(
    "PPT2COURSE_FONT_CACHE_DIR", os.path.join(tempfile.gettempdir(), "ppt2course_fonts")
)

# Memoizes the outcome (including failure) for the life of the process, so a
# network-less environment (e.g. a sandboxed test run) pays the connection
# failure once per process rather than once per compose_video() call.
_resolved_dir_cache: dict[str, str | None] = {}


def ensure_subtitle_font_dir(cache_dir: str = DEFAULT_FONT_CACHE_DIR) -> str | None:
    """Make sure the subtitle CJK font files exist under `cache_dir`,
    downloading whichever are missing. Returns `cache_dir` if at least one
    font file ended up present there, or None if none could be obtained
    (e.g. no network reachability) -- callers should render without a
    `fontsdir` option in that case (today's pre-existing behavior) rather
    than fail the whole job over a missing subtitle font.
    """
    if cache_dir in _resolved_dir_cache:
        return _resolved_dir_cache[cache_dir]

    result = _ensure_subtitle_font_dir_uncached(cache_dir)
    _resolved_dir_cache[cache_dir] = result
    return result


def _ensure_subtitle_font_dir_uncached(cache_dir: str) -> str | None:
    try:
        os.makedirs(cache_dir, exist_ok=True)
    except OSError:
        logger.warning("could not create subtitle font cache dir %s", cache_dir, exc_info=True)
        return None

    have_any = False
    for filename in _FONT_FILES:
        dest = os.path.join(cache_dir, filename)
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            have_any = True
            continue
        tmp_dest = dest + ".part"
        try:
            with urllib.request.urlopen(_FONT_BASE_URL + filename, timeout=10) as response:
                with open(tmp_dest, "wb") as f:
                    f.write(response.read())
            os.replace(tmp_dest, dest)
            have_any = True
        except OSError:
            logger.warning(
                "could not download subtitle font %s -- subtitles may render with no "
                "glyphs at all if this environment has no other CJK font available",
                filename,
                exc_info=True,
            )
            if os.path.exists(tmp_dest):
                os.remove(tmp_dest)

    return cache_dir if have_any else None
