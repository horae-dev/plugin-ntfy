# SPDX-License-Identifier: Apache-2.0
"""The publish payload: hostile titles and bodies neutralised, only checked links clicked."""

import hashlib
import re
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
    sequence_id,
    title_text,
)
from horae_ntfy.settings import parse

PREFIX = "https://notes.example.org/daily/"
SETTINGS = parse(
    {
        "server": "https://ntfy.example.org",
        "topic": "reminders",
        "priority": "high",
        "tags": ["alarm_clock"],
        "link_prefix": PREFIX,
    }
)


def push(**fields: Any) -> dict[str, Any]:
    return {"type": "notify.push", "title": None, "body": "Hello", "link": None, **fields}


def test_a_plain_push() -> None:
    assert payload(push(title="Today", body="3 tasks due"), SETTINGS, delay_s=None) == {
        "topic": "reminders",
        "message": "3 tasks due",
        "title": "Today",
        "priority": 4,
        "tags": ["alarm_clock"],
    }


def test_markdown_is_never_turned_on() -> None:
    hostile = "**bold** [tap](javascript:alert(1)) ![x](https://tracker.example.net/p.png)"
    message = payload(push(body=hostile), SETTINGS, delay_s=None)
    assert message["message"] == hostile  # shown as typed: plain text
    assert "markdown" not in message
    assert "click" not in message  # a URL in the text never becomes the click action
    assert "actions" not in message
    assert "attach" not in message


def test_a_delay_is_a_duration_in_seconds() -> None:
    """A duration, counted on the server's clock: no absolute time to be skewed."""
    assert payload(push(), SETTINGS, delay_s=27000)["delay"] == "27000s"
    assert "delay" not in payload(push(), SETTINGS, delay_s=None)


def test_the_sequence_id_comes_from_the_dedupe_key() -> None:
    key = hashlib.sha256(b"a notification").hexdigest()  # the core's keys: SHA-256 hex
    message = payload(push(), SETTINGS, delay_s=None, dedupe_key=key)
    assert message["sequence_id"] == key[:32]
    assert "sequence_id" not in payload(push(), SETTINGS, delay_s=None)


@pytest.mark.parametrize("key", ["k" * 64, "ABCDEF" * 6, "short", "é/x y", "0" * 31])
def test_a_sequence_id_is_stable_and_within_ntfys_rule(key: str) -> None:
    first, again = sequence_id(key), sequence_id(key)
    assert first == again
    assert first is not None
    assert re.fullmatch(r"[0-9a-f]{32}", first)  # ntfy allows [-_A-Za-z0-9]{1,64}
    assert sequence_id(key + "x") != first
    assert sequence_id(None) is None


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
    assert payload(push(link=link), SETTINGS, delay_s=None)["click"] == link


def test_an_obsidian_link_is_left_for_story_4() -> None:
    link = "obsidian://open?file=Daily%2F2026-10-27.assistant.md"
    assert "click" not in payload(push(link=link), SETTINGS, delay_s=None)
    assert click_url(link, SETTINGS) is None


@pytest.mark.parametrize(
    ("link", "prefix"),
    [
        ("https://evil.example.net/daily/x", PREFIX),
        ("https://notes.example.org/other/x", PREFIX),
        ("https://notes.example.org.evil.example.net/daily/x", PREFIX),
        (PREFIX + "x", None),
        ("javascript:alert(1)", PREFIX),
        ("http://notes.example.org/daily/x", PREFIX),
        ("https://user@notes.example.org/daily/x", PREFIX),
        ("https:///daily/x", PREFIX),
        ("https://notes.example.org.evil.com/x", "https://notes.example.org/x/"),
        ("https://notes.example.org.evil.com/x/y", "https://notes.example.org/x/"),
        ("https://notes.example.org.evil.com/daily/x", PREFIX),
        ("https://notes.example.org:8443/daily/x", PREFIX),
        ("https://Notes.example.org/daily/x", PREFIX),
        ("https://notes.example.org/dailyx", PREFIX),
        (PREFIX + "../secret", PREFIX),
        (PREFIX + "a/../../secret", PREFIX),
        (PREFIX + "./x", PREFIX),
        (PREFIX + "a//b", PREFIX),
        (PREFIX + "%2e%2e/secret", PREFIX),
        (PREFIX + "%2E%2E/secret", PREFIX),
        (PREFIX + "a%2fb", PREFIX),
        (PREFIX + "a%5cb", PREFIX),
        (PREFIX + "a%252e", PREFIX),
        (PREFIX + "a b", PREFIX),
        (PREFIX + "a\nb", PREFIX),
        (PREFIX + "a\\b", PREFIX),
        (PREFIX + "x" * 2100, PREFIX),
        (42, PREFIX),
        ("https://[notes.example.org/daily/x", PREFIX),
    ],
)
def test_any_other_link_refuses_the_push(link: Any, prefix: str | None) -> None:
    settings = parse(
        {
            "server": "https://ntfy.example.org",
            "topic": "t",
            **({} if prefix is None else {"link_prefix": prefix}),
        }
    )
    with pytest.raises(SinkError) as raised:
        payload(push(link=link), settings, delay_s=None)
    assert raised.value.kind == "intent_invalid"


@pytest.mark.parametrize("intent", [push(body=None), push(body=3), push(title=["x"])])
def test_a_malformed_intent_is_refused(intent: dict[str, Any]) -> None:
    with pytest.raises(SinkError) as raised:
        payload(intent, SETTINGS, delay_s=None)
    assert raised.value.kind == "intent_invalid"


FLAG_SCOTLAND = "\U0001f3f4\U000e0067\U000e0062\U000e0073\U000e0063\U000e0074\U000e007f"


def test_a_subdivision_flag_keeps_its_tag_characters() -> None:
    assert body_text(f"Go {FLAG_SCOTLAND}!") == f"Go {FLAG_SCOTLAND}!"
    # Tag characters anywhere else are invisible text: removed.
    assert body_text("a\U000e0067\U000e0062b \U0001f600\U000e0067") == "ab \U0001f600"
    # A black flag alone is kept; its tags end at the cancel tag.
    assert body_text("\U0001f3f4x\U000e0067") == "\U0001f3f4x"


@pytest.mark.parametrize(
    "cluster",
    [
        "\U0001f1eb\U0001f1f7",  # a flag: two regional indicators
        "\U0001f468\u200d\U0001f469\u200d\U0001f467",  # a ZWJ family
        "\U0001f44d\U0001f3fd",  # a skin tone
        "e\u0301",  # a combining mark
        FLAG_SCOTLAND,
    ],
)
@pytest.mark.parametrize("pad", range(6))
def test_a_cut_never_splits_a_cluster(cluster: str, pad: int) -> None:
    text = "x" * pad + cluster * 300
    cut = title_text(text)
    assert cut is not None
    assert len(cut) <= MAX_TITLE
    stem = cut.removesuffix("…")
    assert stem == "x" * pad + cluster * ((len(stem) - pad) // len(cluster))
    assert not stem.endswith("\u200d")


def test_a_cut_never_pairs_regional_indicators_wrongly() -> None:
    # "x" + 125 flags is 251 characters; the 250-character limit (with "…") keeps 124 flags.
    flags = "\U0001f1eb\U0001f1f7" * 125
    cut = title_text("x" + flags)
    assert cut == "x" + flags[: 124 * 2] + "…"
    body = body_text("\U0001f1eb\U0001f1f7" * 2000)
    assert body.removesuffix("…") == "\U0001f1eb\U0001f1f7" * ((len(body) - 1) // 2)
