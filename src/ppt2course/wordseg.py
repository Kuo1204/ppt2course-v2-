"""Word-segmentation support for subtitle line-wrap protection.

Wraps jieba behind a narrow interface so the rest of the app doesn't need
to import or configure jieba directly. subtitle.py's cue splitter already
protects a short hardcoded list of domain phrases (PROTECTED_PHRASES) from
being split down the middle when it has no nearby punctuation and has to
hard-cut a cue; word_spans() generalizes that same protection to *any* word
jieba recognizes — using its bundled dictionary, plus an optional
user-supplied custom dictionary (proper nouns, jargon) layered on top for
whatever jieba's default dictionary doesn't already know.

A fresh jieba.Tokenizer is used instead of jieba's process-wide default
tokenizer so a per-job custom dictionary never leaks into another job's
segmentation. Jobs already run one at a time (see jobs.py), but the
isolation still matters: jieba.Tokenizer.load_userdict() otherwise mutates
its dictionary permanently for the rest of the process's lifetime, so an
unrelated later job would silently inherit an earlier job's custom terms.
"""

import jieba

# jieba tokenizes individual punctuation marks and most function words as
# their own one-character "words". There's no position strictly *inside* a
# 1-character span to protect, so these are filtered out rather than
# padding every span list with no-op entries.
MIN_PROTECTED_WORD_LEN = 2


class WordSegError(Exception):
    pass


class WordSegmenter:
    """Wraps a jieba.Tokenizer, optionally augmented with a custom dictionary."""

    def __init__(self, custom_dict_path: str | None = None):
        self._tokenizer = jieba.Tokenizer()
        if custom_dict_path:
            try:
                self._tokenizer.load_userdict(custom_dict_path)
            except OSError as exc:
                raise WordSegError(f"failed to load custom dictionary: {exc}") from exc

    def word_spans(self, text: str) -> list[tuple[int, int]]:
        """Returns (start, end) character-index spans, into `text`, of every
        word jieba recognizes that is 2+ characters long."""
        spans = []
        cursor = 0
        for word in self._tokenizer.cut(text):
            end = cursor + len(word)
            if end - cursor >= MIN_PROTECTED_WORD_LEN:
                spans.append((cursor, end))
            cursor = end
        return spans


_default_segmenter: WordSegmenter | None = None


def get_default_segmenter() -> WordSegmenter:
    """A shared, no-custom-dictionary WordSegmenter.

    jieba's dictionary trie is expensive enough to build (real file I/O plus
    parsing, even with jieba's own on-disk cache) that rebuilding it per
    slide — or per test — would meaningfully slow down both real jobs and
    the test suite. It holds no per-job state, so every caller without a
    custom dictionary can safely share this one instance.
    """
    global _default_segmenter
    if _default_segmenter is None:
        _default_segmenter = WordSegmenter()
    return _default_segmenter
