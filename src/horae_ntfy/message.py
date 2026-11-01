# SPDX-License-Identifier: Apache-2.0
"""The ntfy JSON publish payload, built from an intent (untrusted text) and the settings.

**Why the JSON API** (`POST https://<server>/` with `{"topic": ..., "message": ...}`) rather
than headers on `POST /<topic>`: the title and the tags would travel in HTTP headers, which
carry Latin-1 at best (ntfy wants RFC 2047 for anything else) and where a stray line break is
a header injection; a JSON body carries any UTF-8 text as data, and only fixed headers are
sent (`Authorization`, `Content-Type`, `Accept`).

**Untrusted text** (the intent's `title` and `body`) is data, shown as plain text:

- `markdown` is never set, so `**`, `[x](javascript:...)` or `![](https://...)` show as typed
  (ntfy renders Markdown only when asked; the text is not escaped for it, so it must not be);
- C0 and C1 controls are removed, except a tab and a line break in the body (`\\r\\n` and a
  lone `\\r` become `\\n`); in the title every line break or tab becomes a space;
- format characters (Unicode `Cf`: bidi overrides and isolates, zero-width spaces, the BOM,
  invisible tag characters) are removed, so text cannot reorder itself on screen or hide
  what it says. Kept: ZWNJ and ZWJ, which emoji sequences and some scripts need, and the tag
  characters (U+E0020 to U+E007F) of a subdivision flag, only right after a black flag
  (U+1F3F4, e.g. the flag of Scotland), where they are part of the emoji;
- the body is cut to `MAX_BODY_BYTES` of UTF-8 (ntfy's default `message-size-limit`: past it
  ntfy turns the message into an attachment or refuses it) and the title to `MAX_TITLE`
  characters, with `…`. The cut is on a character boundary and never inside a cluster: not
  before a combining mark, variation selector, skin tone or tag character, not on either
  side of a ZWJ (no dangling joiner), and not between the two regional indicators of a flag;
- `MAX_TITLE` is 250 characters: ntfy (from v2.28) refuses a title over 1 KB of UTF-8 with a
  400, and 250 characters are at most 1000 bytes; a phone shows one or two lines of it anyway;
- a body with nothing left is `(no text)` (ntfy would otherwise show "triggered").

**The click URL** is the intent's `link`, which the core validated and routed only under the
entry's trusted `link_prefix`; the notifier checks it again with the core's rule (parsed:
same scheme and host as `link_prefix`, its path under the prefix's, and the rest of the path
clean: no `.` or `..` segment, no `%2e %2f %5c %25`; no user info, spaces or controls) and
refuses the push otherwise (`intent_invalid`): no other URL ever becomes a click target.
An `obsidian://` link is not sent as the click action in this version (deep links and action
buttons are story #4): the notification goes out without one (`details.click: "dropped"`).

**`sequence_id`** (`sequence_id`) is derived from the notification's `dedupe_key`, so a
publish of the same notification replaces the earlier one; **`delay`** is a duration
(`"<n>s"`), counted by the server's own clock.
"""

import hashlib
import re
import unicodedata
from typing import Any
from urllib.parse import urlsplit

from horae_sdk import SinkError

from horae_ntfy.settings import Settings, clean_path

MAX_BODY_BYTES = 4096
MAX_TITLE = 250
MAX_LINK = 2048
SEQUENCE_ID_LENGTH = 32
_HEX = re.compile(r"[0-9a-f]{32,64}")
EMPTY_BODY = "(no text)"
_ELLIPSIS = "…"
_ZWJ = "\u200d"
_KEEP_FORMAT = {"\u200c", _ZWJ}  # ZWNJ, ZWJ
_BLACK_FLAG = "\U0001f3f4"
_CANCEL_TAG = "\U000e007f"
_TAGS = {chr(c) for c in range(0xE0020, 0xE0080)}
_REGIONAL = {chr(c) for c in range(0x1F1E6, 0x1F200)}
_LINE_BREAKS = {"\n", "\u2028", "\u2029", "\x85"}


def _clean(text: str, *, multiline: bool) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    kept: list[str] = []
    in_flag = False  # right after a black flag, or inside its tag sequence
    for char in text:
        if in_flag and char in _TAGS:
            kept.append(char)
            in_flag = char != _CANCEL_TAG
            continue
        in_flag = char == _BLACK_FLAG
        if char in _LINE_BREAKS or char == "\t":
            kept.append(("\n" if char != "\t" else "\t") if multiline else " ")
            continue
        category = unicodedata.category(char)
        if category == "Cc" or (category == "Cf" and char not in _KEEP_FORMAT):
            continue
        if category == "Cs":  # a lone surrogate cannot be sent as UTF-8
            kept.append("�")
            continue
        kept.append(char)
    return "".join(kept)


def _boundary(text: str, end: int) -> bool:
    """Whether `text[:end]` ends between two clusters: `text[end]` neither extends the
    character before it (a combining mark, variation selector, skin tone, tag character, ZWJ
    or the character after a ZWJ) nor is the second regional indicator of a flag."""
    char = text[end]
    if unicodedata.combining(char) or char in _EXTENDERS or text[end - 1] == _ZWJ:
        return False
    if char in _REGIONAL:
        run = 0
        while end - run - 1 >= 0 and text[end - run - 1] in _REGIONAL:
            run += 1
        return run % 2 == 0
    return True


def _cut(text: str, fits: Any) -> str:
    """`text` if `fits(text)`, else its longest prefix ending in `…` that fits, cut on a
    cluster boundary (`_boundary`)."""
    if fits(text):
        return text
    end = len(text)
    while end > 0:
        end -= 1
        while end > 0 and not _boundary(text, end):
            end -= 1
        candidate = text[:end].rstrip() + _ELLIPSIS
        if fits(candidate):
            return candidate
    return _ELLIPSIS


_EXTENDERS = {_ZWJ, "\ufe0e", "\ufe0f"} | {chr(c) for c in range(0x1F3FB, 0x1F400)} | _TAGS


def _fits_bytes(limit: int) -> Any:
    return lambda text: len(text.encode("utf-8")) <= limit


def body_text(body: str) -> str:
    cleaned = _clean(body, multiline=True).strip()
    if not cleaned:
        return EMPTY_BODY
    if len(cleaned.encode("utf-8")) <= MAX_BODY_BYTES:
        return cleaned
    # Cut a generous character prefix first, so the backward walk is short.
    return _cut(cleaned[: MAX_BODY_BYTES + 1], _fits_bytes(MAX_BODY_BYTES))


def title_text(title: str | None) -> str | None:
    if title is None:
        return None
    cleaned = " ".join(_clean(title, multiline=False).split())
    if not cleaned:
        return None
    return _cut(cleaned[: MAX_TITLE + 1], lambda text: len(text) <= MAX_TITLE)


def _under(link: str, prefix: str) -> bool:
    """The core's `_link_under`: compared parsed, same scheme and host, path under the
    prefix's, the rest of the path clean."""
    try:
        got, want = urlsplit(link), urlsplit(prefix)
    except ValueError:
        return False
    if got.scheme != want.scheme or got.netloc != want.netloc:
        return False
    if not got.path.startswith(want.path):
        return False
    return clean_path(got.path[len(want.path) :], prefix=False)


def is_obsidian(link: object) -> bool:
    return isinstance(link, str) and link.startswith("obsidian://")


def click_url(link: object, settings: Settings) -> str | None:
    """The click action: the intent's `https://` link, checked again; `None` for no link or an
    `obsidian://` one (story #4)."""
    if link is None or is_obsidian(link):
        return None
    if not isinstance(link, str) or len(link) > MAX_LINK:
        raise SinkError("intent_invalid", "the notification link is not a string URL")
    if any(ch <= " " or ch == "\x7f" for ch in link) or not link.startswith("https://"):
        raise SinkError("intent_invalid", "the notification link is not an https:// URL")
    try:
        parts = urlsplit(link)
    except ValueError:
        raise SinkError("intent_invalid", "the notification link is not a valid URL") from None
    if not parts.hostname or "@" in parts.netloc or "\\" in link:
        raise SinkError("intent_invalid", "the notification link has no host or has user info")
    prefix = settings.link_prefix
    if prefix is None or not prefix.startswith("https://") or not _under(link, prefix):
        raise SinkError("intent_invalid", "the notification link is not under link_prefix")
    return link


def sequence_id(dedupe_key: str | None) -> str | None:
    """ntfy's `sequence_id` for the notification `dedupe_key`: its first 32 characters when it
    is already lowercase hex (the core's keys are SHA-256 hex digests), else the first 32 hex
    characters of its SHA-256. Within ntfy's rule (`[-_A-Za-z0-9]{1,64}`), stable for a key,
    so publishing the same notification again replaces it instead of adding a copy."""
    if dedupe_key is None:
        return None
    if _HEX.fullmatch(dedupe_key):
        return dedupe_key[:SEQUENCE_ID_LENGTH]
    return hashlib.sha256(dedupe_key.encode("utf-8")).hexdigest()[:SEQUENCE_ID_LENGTH]


def payload(
    intent: dict[str, Any],
    settings: Settings,
    *,
    delay_s: int | None,
    dedupe_key: str | None = None,
) -> dict[str, Any]:
    """The JSON publish body for `intent` (already validated by the core: checked again)."""
    body, title = intent.get("body"), intent.get("title")
    if not isinstance(body, str) or not (title is None or isinstance(title, str)):
        raise SinkError("intent_invalid", "a notify.push needs a text body")
    message: dict[str, Any] = {
        "topic": settings.topic,
        "message": body_text(body),
        "priority": settings.ntfy_priority,
    }
    shown = title_text(title)
    if shown is not None:
        message["title"] = shown
    if settings.tags:
        message["tags"] = list(settings.tags)
    click = click_url(intent.get("link"), settings)
    if click is not None:
        message["click"] = click
    if delay_s is not None:
        message["delay"] = f"{delay_s}s"  # a duration: the server's clock, not ours, counts it
    sequence = sequence_id(dedupe_key)
    if sequence is not None:
        message["sequence_id"] = sequence
    return message
