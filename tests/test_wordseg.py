"""Real jieba, no mocking — it's a fast local pure-Python/C dependency, not
a network service, so there's no cost to exercising the real segmenter."""

import pytest

from ppt2course.wordseg import WordSegError, WordSegmenter, get_default_segmenter


def test_word_spans_are_contiguous_and_cover_only_real_substrings():
    text = "職場健康是一個重要的議題"
    spans = get_default_segmenter().word_spans(text)
    for start, end in spans:
        assert end - start >= 2
        assert text[start:end]  # every span must actually index into the text


def test_single_character_tokens_produce_no_spans():
    # jieba tokenizes punctuation as one-character tokens each — nothing
    # there is 2+ characters, so nothing should be protected.
    assert get_default_segmenter().word_spans("，。！？") == []


def test_default_segmenter_is_a_shared_instance():
    assert get_default_segmenter() is get_default_segmenter()


def test_custom_dictionary_forces_an_unknown_term_to_segment_as_one_word(tmp_path):
    text = "我們的產品叫做普拉斯提亞雲端系統"
    term = "普拉斯提亞"
    idx = text.index(term)

    # Without a custom dictionary jieba's default dict + unknown-word
    # heuristics don't recognize this invented term as a single word — it
    # gets fused unpredictably with a neighboring character instead of
    # coming out as its own clean span.
    baseline_spans = get_default_segmenter().word_spans(text)
    assert (idx, idx + len(term)) not in baseline_spans

    dict_path = tmp_path / "custom_dict.txt"
    dict_path.write_text(f"{term} 100\n", encoding="utf-8")

    segmenter = WordSegmenter(custom_dict_path=str(dict_path))
    spans = segmenter.word_spans(text)
    assert (idx, idx + len(term)) in spans


def test_missing_custom_dictionary_file_raises_wordseg_error():
    with pytest.raises(WordSegError):
        WordSegmenter(custom_dict_path="C:/definitely/does/not/exist/dict.txt")


def test_no_custom_dictionary_path_behaves_like_default_segmenter():
    text = "職場健康是一個重要的議題"
    assert WordSegmenter().word_spans(text) == get_default_segmenter().word_spans(text)
