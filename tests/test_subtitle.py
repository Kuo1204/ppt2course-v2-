from ppt2course.subtitle import CharTiming, SubtitleCue, generate_cues, cues_to_srt


def make_chars(text, char_ms=300, start_ms=0):
    return [
        CharTiming(
            char=ch,
            start_ms=start_ms + i * char_ms,
            end_ms=start_ms + (i + 1) * char_ms,
        )
        for i, ch in enumerate(text)
    ]


def test_generate_cues_empty_input_returns_empty_list():
    assert generate_cues([]) == []


def test_single_line_under_limit_no_punctuation():
    chars = make_chars("大家好歡迎來到這堂課", char_ms=300)
    cues = generate_cues(chars)
    assert len(cues) == 1
    assert cues[0].index == 1
    assert cues[0].start_ms == 0
    assert cues[0].end_ms == 3000
    assert cues[0].text == "大家好歡迎來到這堂課"


def test_hard_break_strips_period_keeps_question_mark_and_trims_gap():
    chars = make_chars("你好嗎？我很好。", char_ms=300)
    cues = generate_cues(chars)
    assert len(cues) == 2
    assert (cues[0].start_ms, cues[0].end_ms, cues[0].text) == (0, 1100, "你好嗎？")
    assert (cues[1].start_ms, cues[1].end_ms, cues[1].text) == (1200, 2400, "我很好")


def test_soft_break_punctuation_does_not_split_when_under_char_limit():
    text = "今天天氣很好，我們出去走走吧"
    assert len(text) == 14
    chars = make_chars(text, char_ms=100)
    cues = generate_cues(chars)
    assert len(cues) == 1
    assert cues[0].text == text


def test_soft_break_used_as_wrap_point_when_exceeding_char_limit():
    text = "一二三四五六七八九，十百千萬億兆"
    assert len(text) == 16
    chars = make_chars(text, char_ms=100)
    cues = generate_cues(chars)
    assert len(cues) == 2
    assert (cues[0].start_ms, cues[0].end_ms, cues[0].text) == (0, 900, "一二三四五六七八九")
    assert (cues[1].start_ms, cues[1].end_ms, cues[1].text) == (1000, 1600, "十百千萬億兆")


def test_hard_cut_fallback_when_no_continuity_boundary_found():
    text = "12345678901234567890"
    assert len(text) == 20
    chars = make_chars(text, char_ms=100)
    cues = generate_cues(chars)
    assert len(cues) == 2
    assert (cues[0].start_ms, cues[0].end_ms, cues[0].text) == (0, 1400, "123456789012345")
    assert (cues[1].start_ms, cues[1].end_ms, cues[1].text) == (1500, 2000, "67890")


def test_hard_cut_respects_alnum_run_continuity_via_backtrack():
    text = "今天是公元西元二零二四年是2024年"
    assert len(text) == 18
    chars = make_chars(text, char_ms=100)
    cues = generate_cues(chars)
    assert len(cues) == 2
    assert (cues[0].start_ms, cues[0].end_ms, cues[0].text) == (0, 1200, "今天是公元西元二零二四年是")
    assert (cues[1].start_ms, cues[1].end_ms, cues[1].text) == (1300, 1800, "2024年")


def test_short_cue_merges_with_next_across_hard_break_when_under_min_duration():
    chars = make_chars("你好。謝謝。", char_ms=200)
    cues = generate_cues(chars)
    assert len(cues) == 1
    assert (cues[0].start_ms, cues[0].end_ms, cues[0].text) == (0, 1200, "你好。謝謝")


def test_short_cue_does_not_merge_when_merge_would_exceed_char_limit():
    chars = make_chars("一二三四五六七。八九十甲乙丙丁戊", char_ms=100)
    cues = generate_cues(chars)
    assert len(cues) == 2
    assert (cues[0].start_ms, cues[0].end_ms, cues[0].text) == (0, 700, "一二三四五六七")
    assert (cues[1].start_ms, cues[1].end_ms, cues[1].text) == (800, 1600, "八九十甲乙丙丁戊")


def test_start_offset_shifts_all_timestamps():
    chars = make_chars("大家好歡迎來到這堂課", char_ms=300)
    cues = generate_cues(chars, start_offset_ms=5000)
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
