# SPDX-License-Identifier: Apache-2.0
"""The publish payload: hostile titles and bodies neutralised, only checked links clicked."""

from typing import Any

import pytest
from horae_sdk import SinkError

from horae_ntfy.message import (
    EMPTY_BODY,
    MAX_BODY_BYTES,
    MAX_TITLE,
    body_text,
    click_url,
    payload,
    title_text,
)
from horae_ntfy.settings import parse

PREFIX = "https://notes.example.org/daily/"
SETTINGS = parse(
    {"topic": "reminders", "priority": "high", "tags": ["alarm_clock"], "link_prefix": PREFIX}
)


def push(**fields: Any) -> dict[str, Any]:
    return {"type": "notify.push", "title": None, "body": "Hello", "link": None, **fields}


def test_a_plain_push() -> None:
    assert payload(push(title="Today", body="3 tasks due"), SETTINGS, delay_until=None) == {
        "topic": "reminders",
        "message": "3 tasks due",
        "title": "Today",
        "priority": 4,
        "tags": ["alarm_clock"],
    }


def test_markdown_is_never_turned_on() -> None:
    hostile = "**bold** [tap](javascript:alert(1)) ![x](https://tracker.example.net/p.png)"
    message = payload(push(body=hostile), SETTINGS, delay_until=None)
    assert message["message"] == hostile  # shown as typed: plain text
    assert "markdown" not in message
    assert "click" not in message  # a URL in the text never becomes the click action
    assert "actions" not in message
    assert "attach" not in message


def test_a_delay_is_a_unix_timestamp_string() -> None:
    assert payload(push(), SETTINGS, delay_until=1793602800)["delay"] == "1793602800"


def test_controls_and_invisible_characters_are_removed_from_the_body() -> None:
    hostile = "a\x00b\x1b[31mc\x7fd\x85e\u202ef\u2066g\u200bh\ufeffi\U000e0041j\r\nk\rl\tm"
    assert body_text(hostile) == "ab[31mcd\nefghij\nk\nl\tm"


def test_emoji_sequences_and_joiners_are_kept() -> None:
    family = "\U0001f468\u200d\U0001f469\u200d\U0001f467 n\u200cx \u2764\ufe0f"
    assert body_text(family) == family


def test_a_title_is_one_line() -> None:
    assert title_text("Line one\nLine two\r\n\tthree\u2028four") == "Line one Line two three four"
    assert title_text(" \u202e\u200b ") is None
    assert title_text(None) is None


def test_an_empty_body_still_says_something() -> None:
    assert body_text(" \x00\u200b\n ") == EMPTY_BODY


@pytest.mark.parametrize("unit", ["a", "é", "€", "\U0001f600", "é"])
def test_an_oversized_body_is_cut_to_ntfys_limit(unit: str) -> None:
    cut = body_text(unit * 5000)
    assert len(cut.encode("utf-8")) <= MAX_BODY_BYTES
    assert cut.endswith("…")
    assert len(cut.encode("utf-8")) > MAX_BODY_BYTES - 16
    stem = cut.removesuffix("…")
    assert stem == unit * (len(stem) // len(unit))  # never splits a character from its mark


def test_a_long_title_is_cut() -> None:
    cut = title_text("x" * 600)
    assert cut is not None
    assert len(cut) == MAX_TITLE
    assert cut.endswith("…")


def test_a_body_within_the_limit_is_untouched() -> None:
    exact = "é" * (MAX_BODY_BYTES // 2)
    assert body_text(exact) == exact


def test_a_lone_surrogate_cannot_break_the_encoding() -> None:
    assert body_text("a\udc80b") == "a�b"


def test_an_https_link_under_the_prefix_is_the_click_action() -> None:
    link = PREFIX + "2026-10-29?view=today#top"
    assert payload(push(link=link), SETTINGS, delay_until=None)["click"] == link


def test_an_obsidian_link_is_left_for_story_4() -> None:
    link = "obsidian://open?file=Daily%2F2026-10-27.assistant.md"
    assert "click" not in payload(push(link=link), SETTINGS, delay_until=None)
    assert click_url(link, SETTINGS) is None


@pytest.mark.parametrize(
    ("link", "prefix"),
    [
        ("https://evil.example.net/daily/x", PREFIX),
        ("https://notes.example.org/other/x", PREFIX),
        ("https://notes.example.org.evil.example.net/daily/x", PREFIX),
        (PREFIX + "x", None),
        ("javascript:alert(1)", PREFIX),
        ("http://notes.example.org/daily/x", "http://notes.example.org/daily/"),
        ("https://user@notes.example.org/daily/x", "https://user@notes.example.org/daily/"),
        ("https:///daily/x", "https:///daily/"),
        (PREFIX + "a b", PREFIX),
        (PREFIX + "a\nb", PREFIX),
        (PREFIX + "a\\b", PREFIX),
        (PREFIX + "x" * 2100, PREFIX),
        (42, PREFIX),
        ("https://[notes.example.org/daily/x", "https://[notes.example.org/daily/"),
    ],
)
def test_any_other_link_refuses_the_push(link: Any, prefix: str | None) -> None:
    settings = parse({"topic": "t", **({} if prefix is None else {"link_prefix": prefix})})
    with pytest.raises(SinkError) as raised:
        payload(push(link=link), settings, delay_until=None)
    assert raised.value.kind == "intent_invalid"


@pytest.mark.parametrize("intent", [push(body=None), push(body=3), push(title=["x"])])
def test_a_malformed_intent_is_refused(intent: dict[str, Any]) -> None:
    with pytest.raises(SinkError) as raised:
        payload(intent, SETTINGS, delay_until=None)
    assert raised.value.kind == "intent_invalid"
