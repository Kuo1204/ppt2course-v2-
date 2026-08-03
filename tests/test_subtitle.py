from ppt2course.subtitle import TimedChunk, SubtitleCue, generate_cues, cues_to_srt


def make_chunks(pieces, unit_ms=300, start_ms=0):
    """pieces: a string (each char its own chunk) or a list of strings
    (each element, possibly multi-char, becomes one atomic chunk)."""
    return [
        TimedChunk(
            text=piece,
            start_ms=start_ms + i * unit_ms,
            end_ms=start_ms + (i + 1) * unit_ms,
        )
        for i, piece in enumerate(pieces)
    ]


def test_generate_cues_empty_input_returns_empty_list():
    assert generate_cues([]) == []


def test_single_line_under_limit_no_punctuation():
    chunks = make_chunks("大家好歡迎來到這堂課", unit_ms=300)
    cues = generate_cues(chunks)
    assert len(cues) == 1
    assert cues[0].index == 1
    assert cues[0].start_ms == 0
    assert cues[0].end_ms == 3000
    assert cues[0].text == "大家好歡迎來到這堂課"


def test_hard_break_strips_period_keeps_question_mark_and_trims_gap():
    chunks = make_chunks("你好嗎？我很好。", unit_ms=300)
    cues = generate_cues(chunks)
    assert len(cues) == 2
    assert (cues[0].start_ms, cues[0].end_ms, cues[0].text) == (0, 1100, "你好嗎？")
    assert (cues[1].start_ms, cues[1].end_ms, cues[1].text) == (1200, 2400, "我很好")


def test_soft_break_punctuation_does_not_split_when_under_char_limit():
    text = "今天天氣很好，我們出去走走吧"
    assert len(text) == 14
    chunks = make_chunks(text, unit_ms=100)
    cues = generate_cues(chunks)
    assert len(cues) == 1
    assert cues[0].text == text


def test_soft_break_used_as_wrap_point_when_exceeding_char_limit():
    text = "一二三四五六七八九，十百千萬億兆"
    assert len(text) == 16
    chunks = make_chunks(text, unit_ms=100)
    cues = generate_cues(chunks)
    assert len(cues) == 2
    assert (cues[0].start_ms, cues[0].end_ms, cues[0].text) == (0, 900, "一二三四五六七八九")
    assert (cues[1].start_ms, cues[1].end_ms, cues[1].text) == (1000, 1600, "十百千萬億兆")


def test_hard_overflow_with_no_soft_break_cuts_cleanly_at_char_limit():
    text = "12345678901234567890"
    assert len(text) == 20
    chunks = make_chunks(text, unit_ms=100)
    cues = generate_cues(chunks)
    assert len(cues) == 2
    assert (cues[0].start_ms, cues[0].end_ms, cues[0].text) == (0, 1400, "123456789012345")
    assert (cues[1].start_ms, cues[1].end_ms, cues[1].text) == (1500, 2000, "67890")


def test_multi_char_chunk_is_kept_atomic_and_pushed_to_next_line_rather_than_split():
    pieces = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十", "甲", "乙", "丙", "丁", "世界"]
    chunks = make_chunks(pieces, unit_ms=100)
    cues = generate_cues(chunks)
    assert len(cues) == 2
    assert (cues[0].start_ms, cues[0].end_ms, cues[0].text) == (0, 1300, "一二三四五六七八九十甲乙丙丁")
    assert (cues[1].start_ms, cues[1].end_ms, cues[1].text) == (1400, 1500, "世界")


def test_single_oversized_atomic_chunk_alone_may_exceed_char_limit():
    pieces = ["一二三四五六七八九十甲乙丙丁戊己庚", "另一段"]
    assert len(pieces[0]) == 17
    chunks = make_chunks(pieces, unit_ms=1000)
    cues = generate_cues(chunks)
    assert len(cues) == 2
    assert (cues[0].start_ms, cues[0].end_ms, cues[0].text) == (0, 900, "一二三四五六七八九十甲乙丙丁戊己庚")
    assert (cues[1].start_ms, cues[1].end_ms, cues[1].text) == (1000, 2000, "另一段")


def test_short_cue_merges_with_next_across_hard_break_when_under_min_duration():
    chunks = make_chunks("你好。謝謝。", unit_ms=200)
    cues = generate_cues(chunks)
    assert len(cues) == 1
    assert (cues[0].start_ms, cues[0].end_ms, cues[0].text) == (0, 1200, "你好。謝謝")


def test_short_cue_does_not_merge_when_merge_would_exceed_char_limit():
    chunks = make_chunks("一二三四五六七。八九十甲乙丙丁戊", unit_ms=100)
    cues = generate_cues(chunks)
    assert len(cues) == 2
    assert (cues[0].start_ms, cues[0].end_ms, cues[0].text) == (0, 700, "一二三四五六七")
    assert (cues[1].start_ms, cues[1].end_ms, cues[1].text) == (800, 1600, "八九十甲乙丙丁戊")


def test_kept_hard_break_immediately_after_char_limit_may_yield_16_chars():
    # Accepted edge case: when the buffer is already at the 15-char limit and the
    # very next unit is a kept terminator (？/！), the resulting line is 16 chars.
    # ？/！ are never stripped (unlike。，、；), so this one-char overshoot is
    # intentionally allowed rather than pushing the terminator onto its own line.
    text = "一二三四五六七八九十甲乙丙丁戊？"
    assert len(text) == 16
    chunks = make_chunks(text, unit_ms=100)
    cues = generate_cues(chunks)
    assert len(cues) == 1
    assert cues[0].text == text
    assert len(cues[0].text) == 16


def test_start_offset_shifts_all_timestamps():
    chunks = make_chunks("大家好歡迎來到這堂課", unit_ms=300)
    cues = generate_cues(chunks, start_offset_ms=5000)
    assert cues[0].start_ms == 5000
    assert cues[0].end_ms == 8000


def test_cues_to_srt_empty_list_returns_empty_string():
    assert cues_to_srt([]) == ""


def test_cues_to_srt_basic_single_cue_format():
    cues = [SubtitleCue(index=1, start_ms=0, end_ms=3000, text="大家好")]
    srt = cues_to_srt(cues)
    assert srt == "1\r\n00:00:00,000 --> 00:00:03,000\r\n大家好"


def test_cues_to_srt_multiple_cues_sequential_index_and_blank_line_separator():
    cues = [
        SubtitleCue(index=1, start_ms=0, end_ms=1000, text="第一句"),
        SubtitleCue(index=2, start_ms=1100, end_ms=2500, text="第二句"),
    ]
    srt = cues_to_srt(cues)
    assert srt == (
        "1\r\n00:00:00,000 --> 00:00:01,000\r\n第一句\r\n\r\n"
        "2\r\n00:00:01,100 --> 00:00:02,500\r\n第二句"
    )


def test_cues_to_srt_timestamp_formatting_includes_hours():
    cues = [SubtitleCue(index=1, start_ms=3661234, end_ms=3661999, text="test")]
    srt = cues_to_srt(cues)
    assert "01:01:01,234 --> 01:01:01,999" in srt
