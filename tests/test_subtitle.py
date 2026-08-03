from ppt2course.subtitle import (
    SegmentationConfig,
    SubtitleCue,
    TimedChunk,
    _display_width,
    _segment_run,
    cues_to_srt,
    find_protected_spans,
    find_transition_word_offsets,
    generate_cues,
)


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


def make_variable_chunks(pieces, durations_ms, start_ms=0):
    chunks = []
    t = start_ms
    for piece, dur in zip(pieces, durations_ms):
        chunks.append(TimedChunk(text=piece, start_ms=t, end_ms=t + dur))
        t += dur
    return chunks


# ---- _display_width ----

def test_display_width_cjk_chars_are_2():
    assert _display_width("中文字") == 6


def test_display_width_ascii_chars_are_1():
    assert _display_width("abc123") == 6


def test_display_width_mixed():
    assert _display_width("中ab文") == 2 + 1 + 1 + 2


def test_display_width_fullwidth_punctuation_is_2():
    assert _display_width("，。！？") == 8


# ---- find_protected_spans ----

def test_finds_number_with_decimal_and_percent():
    spans = find_protected_spans("圓周率是3.14159約等於50%喔")
    matched = [("圓周率是3.14159約等於50%喔")[s:e] for s, e in spans]
    assert "3.14159" in matched
    assert "50%" in matched


def test_finds_date_and_time():
    spans = find_protected_spans("會議在2026-08-03的14:30開始")
    text = "會議在2026-08-03的14:30開始"
    matched = [text[s:e] for s, e in spans]
    assert "2026-08-03" in matched
    assert "14:30" in matched


def test_finds_chinese_date():
    text = "今天是2026年8月3日"
    spans = find_protected_spans(text)
    matched = [text[s:e] for s, e in spans]
    assert "2026年8月3日" in matched


def test_finds_url_and_email():
    text = "請參考https://example.com/page或寄信到test@example.com謝謝"
    spans = find_protected_spans(text)
    matched = [text[s:e] for s, e in spans]
    assert "https://example.com/page" in matched
    assert "test@example.com" in matched


def test_finds_english_abbreviation():
    text = "請找Dr.陳醫生協助"
    spans = find_protected_spans(text)
    matched = [text[s:e] for s, e in spans]
    assert "Dr." in matched


def test_no_protected_spans_in_plain_text():
    assert find_protected_spans("今天天氣很好我們出去走走") == []


# ---- find_transition_word_offsets ----

def test_finds_transition_word_offset():
    text = "今天很累但是我們還是去了"
    offsets = find_transition_word_offsets(text, ("但是",))
    assert offsets == {4}


def test_no_transition_word_found():
    offsets = find_transition_word_offsets("今天天氣很好", ("但是", "然而"))
    assert offsets == set()


# ---- _segment_run: core DP behavior ----

def test_segment_run_no_split_when_under_max_width():
    chunks = make_chunks("大家好歡迎來到這堂課", unit_ms=300)  # width 20 <= 36
    result = _segment_run(chunks, SegmentationConfig())
    assert len(result) == 1
    assert result[0] == chunks


def test_segment_run_prefers_comma_over_plain_length_fallback():
    # 21 CJK chars, width 42 (> 36). Comma at char index 9 (0-indexed).
    text = "今天的天氣非常晴朗，我們決定出門去公園散步"
    assert len(text) == 21
    chunks = make_chunks(text, unit_ms=200)
    result = _segment_run(chunks, SegmentationConfig())
    assert [("".join(c.text for c in line)) for line in result] == [
        "今天的天氣非常晴朗，",
        "我們決定出門去公園散步",
    ]


def test_segment_run_prefers_transition_word_over_plain_length_fallback():
    # 24 CJK chars, width 48 (> 36), no punctuation at all; "但是" starts at
    # chunk index 10, aligned exactly to a chunk boundary.
    text = "今天早上去爬山看日出但是後來下雨只好提前下山回家"
    assert len(text) == 24
    chunks = make_chunks(text, unit_ms=200)
    result = _segment_run(chunks, SegmentationConfig())
    assert [("".join(c.text for c in line)) for line in result] == [
        "今天早上去爬山看日出",
        "但是後來下雨只好提前下山回家",
    ]


def test_segment_run_avoids_splitting_inside_protected_number_span():
    # Long run with "2024" embedded; the width-optimal cut point (28) falls
    # on the 2nd digit if unprotected. Verify no resulting line breaks the
    # number apart (each line either fully contains "2024" or not at all).
    preamble = "甲乙丙丁戊己庚辛壬癸乙丙丁"  # 13 chars, width 26
    suffix = "子丑寅卯辰巳午未申"  # 9 chars, width 18
    text = preamble + "2024" + suffix
    chunks = make_chunks(text, unit_ms=150)
    result = _segment_run(chunks, SegmentationConfig())

    lines_text = ["".join(c.text for c in line) for line in result]
    assert "".join(lines_text) == text  # no chunk lost or duplicated
    digit_fragment_lines = [t for t in lines_text if any(d in t for d in "2024")]
    assert len(digit_fragment_lines) == 1
    assert "2024" in digit_fragment_lines[0]


def test_segment_run_cps_penalty_overrides_naive_comma_preference():
    # A comma sits right after 2 very fast chunks; cutting there yields a
    # tiny, absurdly-fast (cps>>12) segment. A plain length-fallback cut
    # further in (no punctuation bonus) should win instead because the
    # comma option is punished on both length-fit and CPS.
    pieces = list("甲乙，") + list("子丑寅卯辰巳午未申庚辛壬癸酉戉亥")
    durations = [50, 50, 50] + [300] * 17
    chunks = make_variable_chunks(pieces, durations)
    result = _segment_run(chunks, SegmentationConfig())

    first_line_text = "".join(c.text for c in result[0])
    assert first_line_text != "甲乙，"


def test_segment_run_single_oversized_atomic_chunk_kept_whole():
    chunks = [
        TimedChunk("一二三四五六七八九十甲乙丙丁戊己庚辛壬癸", 0, 1000),  # width 40 > 36
        TimedChunk("下一段文字", 1000, 2000),
    ]
    result = _segment_run(chunks, SegmentationConfig())
    assert len(result) == 2
    assert result[0][0].text == "一二三四五六七八九十甲乙丙丁戊己庚辛壬癸"
    assert result[1][0].text == "下一段文字"


# ---- generate_cues: end-to-end (hard breaks, merge, gap-trim, offset, empty) ----

def test_generate_cues_empty_input_returns_empty_list():
    assert generate_cues([]) == []


def test_hard_break_strips_period_keeps_question_mark_and_trims_gap():
    chunks = make_chunks("你好嗎？我很好。", unit_ms=300)
    cues = generate_cues(chunks)
    assert len(cues) == 2
    assert (cues[0].start_ms, cues[0].end_ms, cues[0].text) == (0, 1100, "你好嗎？")
    assert (cues[1].start_ms, cues[1].end_ms, cues[1].text) == (1200, 2400, "我很好")


def test_short_cue_merges_with_next_across_hard_break_when_under_min_duration():
    chunks = make_chunks("你好。謝謝。", unit_ms=200)
    cues = generate_cues(chunks)
    assert len(cues) == 1
    assert (cues[0].start_ms, cues[0].end_ms, cues[0].text) == (0, 1200, "你好。謝謝")


def test_short_cue_does_not_merge_when_merge_would_exceed_max_width():
    # Each line is 17 chars (width 34), under 36 alone, but merged would be
    # width 68 > 36, so must stay separate even though both are short.
    line1 = "一二三四五六七八九十甲乙丙丁戊己庚。"
    line2 = "子丑寅卯辰巳午未申酉戌亥壬癸辛庚己。"
    chunks = make_chunks(line1 + line2, unit_ms=50)
    cues = generate_cues(chunks)
    assert len(cues) == 2


def test_kept_hard_break_immediately_after_width_limit_may_overshoot():
    # 18 CJK chars (width 36, exactly at the limit), followed by a kept
    # terminator (？) -> resulting line is width 38, one unit over.
    text = "一二三四五六七八九十甲乙丙丁戊己庚辛？"
    chunks = make_chunks(text, unit_ms=100)
    cues = generate_cues(chunks)
    assert len(cues) == 1
    assert cues[0].text == text


def test_start_offset_shifts_all_timestamps():
    chunks = make_chunks("大家好歡迎來到這堂課", unit_ms=300)
    cues = generate_cues(chunks, start_offset_ms=5000)
    assert cues[0].start_ms == 5000
    assert cues[0].end_ms == 8000


# ---- cues_to_srt (unaffected by segmentation changes) ----

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
