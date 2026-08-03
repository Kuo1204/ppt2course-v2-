from ppt2course.script_cleaner import clean_script


def test_empty_input_returns_empty_string():
    assert clean_script("") == ""


def test_removes_blank_lines_and_joins_with_single_newline():
    text = "第一行\n\n\n第二行\n\n第三行"
    assert clean_script(text) == "第一行\n第二行\n第三行"


def test_removes_divider_line_of_dashes():
    text = "重點一\n----------\n重點二"
    assert clean_script(text) == "重點一\n重點二"


def test_removes_divider_lines_of_various_chars():
    text = "A\n___\nB\n===\nC\n***\nD\n~~~~\nE"
    assert clean_script(text) == "A\nB\nC\nD\nE"


def test_divider_requires_at_least_3_repeats():
    text = "重點\n--\n下一段"
    assert clean_script(text) == "重點\n--\n下一段"


def test_divider_requires_uniform_single_char():
    text = "重點\n-=-\n下一段"
    assert clean_script(text) == "重點\n-=-\n下一段"


def test_does_not_touch_inline_dash_within_content_line():
    text = "小結 - 重點"
    assert clean_script(text) == "小結 - 重點"


def test_strips_leading_bullet_symbols():
    text = "• 重點一\n- 重點二\n● 重點三\n○ 重點四"
    assert clean_script(text) == "重點一\n重點二\n重點三\n重點四"


def test_strips_numbered_list_formats():
    text = "1. 第一點\n2. 第二點\n1) 第三點\n(1) 第四點\n12. 第十二點"
    assert clean_script(text) == "第一點\n第二點\n第三點\n第四點\n第十二點"


def test_strips_markdown_bold_double_star_and_double_underscore():
    text = "**重要事項**\n__另一個重點__"
    assert clean_script(text) == "重要事項\n另一個重點"


def test_strips_markdown_italic_single_star_and_single_underscore():
    text = "*強調文字*\n_另一個強調_"
    assert clean_script(text) == "強調文字\n另一個強調"


def test_bold_and_italic_together_do_not_interfere():
    text = "**粗體**和*斜體*"
    assert clean_script(text) == "粗體和斜體"


def test_strips_inline_code_backticks():
    text = "請執行 `pip install`"
    assert clean_script(text) == "請執行 pip install"


def test_strips_markdown_headers():
    text = "# 標題一\n## 標題二\n### 標題三"
    assert clean_script(text) == "標題一\n標題二\n標題三"


def test_pure_divider_line_not_corrupted_by_markdown_stripping():
    text = "重點一\n___\n重點二"
    assert clean_script(text) == "重點一\n重點二"
    text2 = "重點一\n***\n重點二"
    assert clean_script(text2) == "重點一\n重點二"


def test_collapses_internal_whitespace_and_trims_line_edges():
    text = "  你好　　世界  \n\t重點\t內容\t"
    assert clean_script(text) == "你好 世界\n重點 內容"


def test_line_becomes_blank_after_stripping_is_removed():
    text = "- \n重點內容\n# \n另一行"
    assert clean_script(text) == "重點內容\n另一行"


def test_only_blank_and_divider_lines_returns_empty_string():
    text = "\n\n---\n___\n\n"
    assert clean_script(text) == ""
