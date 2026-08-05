"""Ported from the original prototype's own self-test scenarios (its
`if __name__ == "__main__"` block), adapted to pytest and to this module's
TimedChunk-based generate_cues interface instead of operating on raw text
or a parsed SRT string.
"""

from ppt2course.subtitle import (
    MIN_GAP_MS,
    PROTECTED_PHRASES,
    SubtitleCue,
    TimedChunk,
    cues_to_srt,
    generate_cues,
    split_text_into_chunks,
    strip_cue_punctuation,
)


def _uniform_chunks(text: str, ms_per_char: int = 100, start_ms: int = 0) -> list[TimedChunk]:
    chunks = []
    t = start_ms
    for ch in text:
        chunks.append(TimedChunk(ch, t, t + ms_per_char))
        t += ms_per_char
    return chunks


# ---------- split_text_into_chunks: tiered "Smart Sentence Split" ----------


def test_tiered_split_keeps_punctuation_at_segment_end_not_start():
    text = "各位同仁大家好，歡迎參加本次教育訓練課程。今天我要來討論職場健康的定義，常見樣態，以及公司申訴機制辦法，希望大家都能建立正確的認知。"
    chunks = split_text_into_chunks(text, max_chars=20)
    assert "".join(chunks) == text
    for c in chunks:
        assert not c or c[0] not in "，。,、；;:.!?", f"punctuation must not open a segment: {c!r}"


def test_split_respects_target_length_when_no_punctuation_nearby():
    text = "如果未進行帳戶核對，為保障個人資料隱私，將無法查詢個人相關履歷"
    chunks = split_text_into_chunks(text, max_chars=18)
    assert "".join(chunks) == text
    assert all(len(c) <= 18 * 2 for c in chunks)


def test_extended_fallback_prefers_nearer_lower_tier_break_over_farther_tier1():
    # comma split position 11 (nearer) and the only period's split position
    # 18 (farther) are both outside the max_chars=10 window but inside the
    # 2x extended search. Strictly trying tier1 before tier2 in the extended
    # search picks the farther period and produces a needlessly long first
    # chunk; picking whichever break is nearest, tier order aside, is the
    # "smarter" choice.
    text = "各位同仁大家好呀好呀，歡迎光臨這裡。謝謝"
    chunks = split_text_into_chunks(text, max_chars=10)
    assert "".join(chunks) == text
    assert chunks[0] == "各位同仁大家好呀好呀，"


# ---------- protected phrases: never split down the middle ----------


def test_protected_phrases_never_split_across_chunks():
    text = "今天要說明公司制度以及職場健康相關的申訴機制與案例分析內容"
    chunks = split_text_into_chunks(text, max_chars=12)
    assert "".join(chunks) == text
    for phrase in ["公司制度", "職場健康", "申訴機制", "案例分析"]:
        assert any(phrase in c for c in chunks), f"protected phrase should stay intact: {phrase!r}"


def test_protected_phrase_avoided_even_with_no_punctuation_nearby():
    text = "若未進行帳戶核對，為保障個人資料隱私，將無法查詢個人相關履歷，請務必使用員工保卡進行帳戶核對"
    chunks = split_text_into_chunks(text, max_chars=18)
    assert "".join(chunks) == text
    for i in range(len(chunks) - 1):
        assert not chunks[i].endswith("帳戶"), f"'帳戶核對' split apart: {chunks[i]!r}"
        assert not chunks[i + 1].startswith("核對"), f"'帳戶核對' split apart: {chunks[i + 1]!r}"


# ---------- quotation marks: never split a quoted phrase across chunks ----------


def test_quoted_phrase_never_split_across_chunks():
    text = "主管在會議上特別強調「這件事情大家都要遵守規定」，請大家務必配合辦理"
    chunks = split_text_into_chunks(text, max_chars=12)
    assert "".join(chunks) == text
    quote = "「這件事情大家都要遵守規定」"
    assert any(quote in c for c in chunks), f"quoted phrase should stay intact: {quote!r}"


def test_curly_double_quoted_phrase_never_split_across_chunks():
    text = "老師在課堂上說“每個人都要對自己負責”，這句話讓大家印象深刻"
    chunks = split_text_into_chunks(text, max_chars=12)
    assert "".join(chunks) == text
    quote = "“每個人都要對自己負責”"
    assert any(quote in c for c in chunks), f"quoted phrase should stay intact: {quote!r}"


# ---------- orphan-merge: no tiny trailing fragments ----------


def test_tiny_trailing_fragments_get_merged_into_neighbor():
    text = "也可能因為案例分析和常見錯誤說明有問題"
    chunks = split_text_into_chunks(text, max_chars=14)
    assert "".join(chunks) == text
    assert all(len(c) >= 4 for c in chunks)


# ---------- wrap_subtitle_text: in-cue 2-line wrap ----------


# ---------- strip_cue_punctuation ----------


def test_strip_cue_punctuation_removes_leading_and_trailing():
    assert strip_cue_punctuation("，這是句子。") == "這是句子"
    assert strip_cue_punctuation("這是句子") == "這是句子"


# ---------- generate_cues: Phrase Reconstruction from fragmented WordBoundary chunks ----------


def test_generate_cues_reconstructs_text_split_across_multiple_timed_chunks():
    chunks = [
        TimedChunk("今天", 0, 400),
        TimedChunk("介紹", 400, 900),
        TimedChunk("公司", 900, 1400),
        TimedChunk("制度", 1400, 1800),
        TimedChunk("。", 1800, 1800),
    ]
    cues = generate_cues(chunks, max_chars=30)
    assert len(cues) == 1
    assert cues[0].text == "今天介紹公司制度"
    assert cues[0].start_ms == 0
    assert cues[0].end_ms == 1800


def test_generate_cues_splits_long_narration_into_multiple_cues():
    text = "各位同仁大家好，歡迎參加本次教育訓練課程，今天我要來討論職場健康與工作環境的定義。"
    chunks = _uniform_chunks(text, ms_per_char=100)
    cues = generate_cues(chunks)
    assert len(cues) > 1
    assert "".join(c.text.replace("\n", "") for c in cues) != ""
    for i in range(1, len(cues)):
        assert cues[i].start_ms >= cues[i - 1].end_ms


def test_generate_cues_strips_trailing_punctuation_from_every_cue():
    text = "各位同仁大家好，歡迎參加本次教育訓練課程。今天我們要說明常見樣態，包含語言歧視，身體歧視，還有薪資管理相關規範。"
    chunks = _uniform_chunks(text, ms_per_char=80)
    cues = generate_cues(chunks)
    trailing_punct = "，。,、；;:.!?"
    for c in cues:
        last_char = c.text[-1]
        assert last_char not in trailing_punct, f"cue should not end with punctuation: {c.text!r}"


def test_generate_cues_never_wraps_to_a_second_line():
    # at max_chars=14 this text's protected-phrase avoidance genuinely
    # produces a 16-char chunk (over budget) — previously this triggered
    # wrap_subtitle_text's 2-line wrap. It must now stay one line.
    text = "職場健康的相關議題非常值得我們深入探討與研究內容包含很多面向"
    chunks = _uniform_chunks(text, ms_per_char=100)
    cues = generate_cues(chunks, max_chars=14)
    assert any(len(c.text) > 14 for c in cues), "test setup should still produce an over-budget cue"
    for c in cues:
        assert "\n" not in c.text, f"cue must stay a single line: {c.text!r}"


def test_generate_cues_splits_a_single_overlong_sentence_into_several_cues():
    text = (
        "這個基本觀念，包含新制度細節，哪些行為可能屬於職場健康的常見樣態，"
        "以及申訴機制與教育訓練說明。"
    )
    chunks = _uniform_chunks(text, ms_per_char=90)
    cues = generate_cues(chunks, max_chars=18)
    assert len(cues) > 1
    for i in range(1, len(cues)):
        assert cues[i].start_ms >= cues[i - 1].end_ms - 1  # contiguous, never overlapping
    assert cues[0].start_ms == 0
    assert cues[-1].end_ms == len(text) * 90


def test_generate_cues_applies_start_offset():
    chunks = _uniform_chunks("大家好", ms_per_char=100)
    cues = generate_cues(chunks, start_offset_ms=5000)
    assert cues[0].start_ms == 5000


def test_generate_cues_empty_chunks_returns_empty_list():
    assert generate_cues([]) == []


def test_generate_cues_each_cue_is_a_standalone_segment_not_cumulative():
    text = "今天要介紹職場健康推動政策，包含常見案例分析與申訴機制說明，最後開放大家提問討論。"
    chunks = _uniform_chunks(text, ms_per_char=70)
    cues = generate_cues(chunks, max_chars=18)
    for i in range(1, len(cues)):
        assert not cues[i].text.replace("\n", "").startswith(cues[i - 1].text.replace("\n", ""))


# ---------- cues_to_srt ----------


def test_cues_to_srt_formats_sequential_blocks_with_crlf():
    cues = [
        SubtitleCue(1, 0, 1500, "大家好"),
        SubtitleCue(2, 1500, 3200, "歡迎收看"),
    ]
    srt = cues_to_srt(cues)
    assert srt == (
        "1\r\n00:00:00,000 --> 00:00:01,500\r\n大家好\r\n\r\n"
        "2\r\n00:00:01,500 --> 00:00:03,200\r\n歡迎收看"
    )


def test_cues_to_srt_preserves_embedded_newline_for_two_line_cues():
    cues = [SubtitleCue(1, 0, 2000, "第一行\n第二行")]
    srt = cues_to_srt(cues)
    assert "第一行\n第二行" in srt


def test_cues_to_srt_empty_list_returns_empty_string():
    assert cues_to_srt([]) == ""


# ---------- module-level constants video.py depends on ----------


def test_min_gap_ms_exported_for_cross_slide_reconciliation():
    assert isinstance(MIN_GAP_MS, int)
    assert MIN_GAP_MS > 0


def test_protected_phrases_list_matches_the_original_prototype():
    assert PROTECTED_PHRASES == [
        "職場健康", "工作環境", "案例分析", "身心健康", "申訴機制",
        "薪資管理", "教育訓練", "公司制度", "工作要求",
    ]
