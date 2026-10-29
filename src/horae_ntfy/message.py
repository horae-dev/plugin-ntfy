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
  invisible tag characters) are removed, except ZWNJ and ZWJ, which emoji sequences and some
  scripts need; so text cannot reorder itself on screen or hide what it says;
- the body is cut to `MAX_BODY_BYTES` of UTF-8 (ntfy's default `message-size-limit`: past it
  ntfy turns the message into an attachment or refuses it) and the title to `MAX_TITLE`
  characters, on a character boundary, never before a combining mark, with `…`;
- a body with nothing left is `(no text)` (ntfy would otherwise show "triggered").

**The click URL** is the intent's `link`, which the core validated and routed only under the
entry's trusted `link_prefix`; the notifier checks it again (an `https://` URL under
`link_prefix`, no user info, spaces or controls) and refuses the push otherwise
(`intent_invalid`): no other URL ever becomes a click target. An `obsidian://` link is not
sent as the click action in this version (deep links and action buttons are story #4): the
notification goes out without one.
"""

import unicodedata
from typing import Any
from urllib.parse import urlsplit

from horae_sdk import SinkError

from horae_ntfy.settings import Settings

MAX_BODY_BYTES = 4096
MAX_TITLE = 250
MAX_LINK = 2048
EMPTY_BODY = "(no text)"
_ELLIPSIS = "…"
_KEEP_FORMAT = {"\u200c", "\u200d"}  # ZWNJ, ZWJ
_LINE_BREAKS = {"\n", "\u2028", "\u2029", "\x85"}


def _clean(text: str, *, multiline: bool) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    kept: list[str] = []
    for char in text:
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


def _cut(text: str, fits: Any) -> str:
    """`text` if `fits(text)`, else its longest prefix ending in `…` that fits, cut before a
    character (never between a character and its combining marks, ZWJ or variation selector)."""
    if fits(text):
        return text
    end = len(text)
    while end > 0:
        end -= 1
        while end > 0 and (unicodedata.combining(text[end]) or text[end] in _JOINERS):
            end -= 1
        candidate = text[:end].rstrip() + _ELLIPSIS
        if fits(candidate):
            return candidate
    return _ELLIPSIS


_JOINERS = {"\u200d", "\ufe0e", "\ufe0f"} | {chr(c) for c in range(0x1F3FB, 0x1F400)}


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


def click_url(link: object, settings: Settings) -> str | None:
    """The click action: the intent's `https://` link, checked again; `None` for no link or an
    `obsidian://` one (story #4)."""
    if link is None:
        return None
    if not isinstance(link, str) or len(link) > MAX_LINK:
        raise SinkError("intent_invalid", "the notification link is not a string URL")
    if link.startswith("obsidian://"):
        return None
    if any(ch <= " " or ch == "\x7f" for ch in link) or not link.startswith("https://"):
        raise SinkError("intent_invalid", "the notification link is not an https:// URL")
    try:
        parts = urlsplit(link)
    except ValueError:
        raise SinkError("intent_invalid", "the notification link is not a valid URL") from None
    if not parts.hostname or "@" in parts.netloc or "\\" in link:
        raise SinkError("intent_invalid", "the notification link has no host or has user info")
    prefix = settings.link_prefix
    if prefix is None or not prefix.startswith("https://") or not link.startswith(prefix):
        raise SinkError("intent_invalid", "the notification link is not under link_prefix")
    return link


def payload(
    intent: dict[str, Any], settings: Settings, *, delay_until: int | None
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
    if delay_until is not None:
        message["delay"] = str(delay_until)
    return message
