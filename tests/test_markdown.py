"""Contract for markdown_to_telegram_html.

Only Telegram can decide whether markup parses, so every expectation here was
checked against the live API first. If Telegram's HTML mode changes, re-check
against the API rather than "fixing" the renderer to match a guess.
"""

import pytest

from conftest import load

md = load("telegram-mcp").markdown_to_telegram_html


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("**bold**", "<b>bold</b>"),
        ("__bold__", "<b>bold</b>"),
        ("*italic*", "<i>italic</i>"),
        ("_italic_", "<i>italic</i>"),
        ("~~struck~~", "<s>struck</s>"),
        # Telegram has no heading, list or rule tags: they degrade to text.
        ("# Heading", "<b>Heading</b>"),
        ("### Closed ###", "<b>Closed</b>"),
        ("- one\n* two\n  + three", "• one\n• two\n  • three"),
        ("---", "─" * 12),
        ("***", "─" * 12),
        ("> quoted\n> more\nafter", "<blockquote>quoted\nmore</blockquote>\nafter"),
    ],
)
def test_supported_constructs(source, expected):
    assert md(source) == expected


def test_snake_case_is_not_italic():
    # A bare underscore inside a word must not open emphasis.
    assert md("a_b_c snake_case_name") == "a_b_c snake_case_name"


def test_bare_asterisks_are_not_emphasis():
    assert md("5 * 3 * 2") == "5 * 3 * 2"


def test_raw_html_is_escaped_never_honoured():
    # A caller must never be able to inject markup into a notification.
    assert md("<b>x</b> & <i>") == "&lt;b&gt;x&lt;/b&gt; &amp; &lt;i&gt;"


def test_code_contents_are_never_treated_as_markup():
    out = md("`a<b>c` and ```py\nx = 1 & 2\n```")
    assert out == (
        "<code>a&lt;b&gt;c</code> and "
        '<pre><code class="language-py">x = 1 &amp; 2</code></pre>'
    )


def test_emphasis_inside_a_code_span_stays_literal():
    assert md("`**not bold**`") == "<code>**not bold**</code>"


def test_link_target_survives_escaping_and_emphasis():
    out = md("[link **text**](https://e.com/a?b=1&c=2)")
    assert out == '<a href="https://e.com/a?b=1&amp;c=2">link <b>text</b></a>'


def test_link_label_may_hold_a_code_span():
    assert md("[`code`](https://x.io)") == '<a href="https://x.io"><code>code</code></a>'


def test_link_target_cannot_break_out_of_the_href_quotes():
    out = md('[x](https://e.com/") onclick="evil')
    assert out.startswith('<a href="https://e.com/&quot;">x</a>')


def test_many_code_spans_do_not_collide():
    # Placeholders are \x00<index>\x00; index 1 must not match inside index 11.
    out = md(" ".join(f"`c{i}`" for i in range(14)))
    assert "\x00" not in out
    assert out.count("<code>") == 14
    assert "<code>c13</code>" in out


def test_null_bytes_from_the_caller_cannot_forge_a_placeholder():
    assert "\x00" not in md("a\x000\x00b `x`")


def test_empty_input_stays_empty():
    assert md("") == ""
