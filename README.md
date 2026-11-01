<p align="center"><img src="https://raw.githubusercontent.com/horae-dev/.github/main/brand/horae-logo-500.png" width="96" alt="Horae logo"></p>

# plugin-ntfy

Horae plugin: push notifications through ntfy, with digests, reminders, quiet hours and action buttons.

> 🚧 **In progress (M3).** `ntfy.push` sends notifications, once per reminder and day; deep links and action buttons (#4) are not built yet. Follow progress on the [Horae roadmap project](https://github.com/orgs/horae-dev/projects/1).

## What it does

`ntfy.push` is a **notifier**: a pipeline's `notify` step names it, the core routes each `notify.push` intent to it, and after the run the core applies the intent through the plugin's `ntfy.apply` sink operation. The plugin publishes it to an [ntfy](https://ntfy.sh) server with the entry's topic, priority and tags, the access token, and the intent's link as the click action. It proposes nothing; the only file it writes is its reminder state (below).

```yaml
# pipelines/remind.yaml (excerpt)
- notify:
    - ntfy.push:
        server: https://ntfy.example.org        # required with auth: token
        topic: horae-reminders                  # required: A-Z a-z 0-9 _ -, at most 64
        priority: high                          # min | low | normal (default) | high | critical
        tags: [alarm_clock]                     # ntfy tags or emoji short codes, at most 8
        auth: token                             # token (default) | none
        quiet_hours: { start: "22:00", end: "07:00", during: delay }   # during: delay (default) | drop
        link_prefix: https://notes.example.org/daily/                  # links routed here (core)
        dedupe: daily                           # daily (default): once per reminder and day | off
```

| Parameter | Meaning |
|---|---|
| `server` | **Required with `auth: token`** (the token is never sent to a default host nobody wrote down); only `auth: none` defaults to `https://ntfy.sh`. `https://<host>[:port]`, no path, query, fragment or user info; the host is a lowercase DNS name (no IP literal, no `localhost`). `http://<host>[:port]` is accepted for a server **on the LAN only**: the host is resolved when connecting and refused unless every address is loopback, RFC 1918 or IPv6 ULA (the rule the core applies to an `http://` LLM endpoint). The token then travels unencrypted on that network: prefer `https`. |
| `topic` | The ntfy topic. It is not a secret: it is stored in run records. **On a public server such as ntfy.sh, a topic name acts like a weak password**: anyone who guesses it can subscribe to it (read your notifications) or publish to it, unless it is reserved and protected with access control (an account and ACLs, or a self-hosted server with `auth-default-access: deny-all`). Choose a long, unguessable name if you cannot protect it. |
| `priority` | Mapped to ntfy's 1 to 5: `min` 1, `low` 2, `normal` 3, `high` 4, `critical` 5. Only `critical` bypasses quiet hours. The intent has no priority: it is the entry's (trusted) choice, so use a separate pipeline for critical alerts (two `notify` entries that both take `notify.push` make every push ambiguous for the core). |
| `tags` | ntfy tags; a tag that is an emoji short code shows as that emoji. |
| `auth` | `token`: send `NTFY_TOKEN` as `Authorization: Bearer`; missing is an error, never a silent anonymous publish. `none`: send no credentials. |
| `quiet_hours` | `start` and `end` as `"HH:MM"`, local times in the instance time zone (`horae.yaml` `timezone`, which the host passes as `TZ`); `start > end` crosses midnight. See below. |
| `dedupe` | `daily` (default): a push is sent **once per rule and local day** ([Reminder state](#reminder-state)); needs the state directory approved in `horae.lock`. `off`: every apply is pushed and no state is kept (the core still sends each `dedupe_key` once). |
| `link_prefix` | Read by the core: a proposed `link` is routed to this entry only if it falls under it. The plugin validates it with the core's rule (an `https://` or `obsidian://` URL ending in `/`, no user info, query or fragment, a clean path: no empty, `.` or `..` segment, no `%2e %2f %5c %25`) and checks the link against it again, parsed: same scheme and host, the path under the prefix's (so `https://notes.example.org.evil.com/x` is not under `https://notes.example.org/x/`). |

Any other key (a typo such as `quiet_hour`) or an invalid value fails the apply with `config_invalid`, naming the parameter, so a feature is never switched off silently.

### Quiet hours

During the window, a push that is not `critical` is:

- **`during: delay`** (default): published now with ntfy's **scheduled delivery**, so the phone receives it when quiet hours end. `delay` is sent as a **duration** (`"27000s"`, at least 30 s), not an absolute time: the ntfy server counts it on its own clock, so a clock skew between Horae and the server shifts the delivery by the skew instead of making the server refuse a time it thinks is too close or already past. If the server still refuses the scheduled publish (a 400), the apply fails with `delay_refused`; the push is **never re-sent at once**, which would wake the phone during quiet hours. The server holds the scheduled message.
- **`during: drop`**: not sent. The apply still succeeds (the core records its `dedupe_key`), with `details.delivery: dropped`. The reminder state does **not** record it: nothing reached the phone, so the same reminder proposed by a run after quiet hours is sent then, once.

**Scheduled delivery needs a persistent message cache on a self-hosted server.** ntfy keeps a scheduled message in its message cache until it is due. ntfy.sh does; a self-hosted server must set `cache-file` (SQLite) or a database (`database-url`), otherwise the cache is in memory and **a restart of the server loses every scheduled message** (the notification is never delivered, and Horae has already recorded it as handled).

**Unverified: iOS with `upstream-base-url`.** A self-hosted server reaches iPhones through `upstream-base-url` (it forwards a poll request to ntfy.sh, which wakes the iOS app). Whether that forward happens when a scheduled message becomes due, rather than when it was published, has not been tested; on iOS, a delayed push may only show up when the app next polls. Android (and the web app) receive it from the server directly.

In both cases `apply` succeeds, so the core records the notification's `dedupe_key` as handled and will not send it again. `critical` is always sent at once. PLAN.md does not say which behaviour quiet hours have; deferring is the default because a reminder that fires at 23:00 is still useful at 07:00, and `drop` is there for pipelines whose pushes go stale (a "now" alert).

The window is read on the **local wall clock**, so daylight saving time does what you expect: the night is an hour shorter in spring and longer in autumn, a repeated autumn hour is quiet on both passes, and an `end` that falls in the spring gap ends when the gap does. An `end` that falls **inside the repeated autumn hour** ends at its **later** occurrence (`end: "02:30"` in Paris on the last Sunday of October is 02:30 CET, not 02:30 CEST), since between the two the clock comes back into the window.

### Duplicates

The core records a notification's `dedupe_key` as sent when `apply` returns, and never sends a recorded key again. When a publish may have reached the server but `apply` failed (a timeout after the request was sent, or the host cancelling the call), the key is not recorded and a retried or re-run apply publishes it again. So every publish carries ntfy's **`sequence_id`**, derived from the `dedupe_key` (its first 32 hex characters; a key that is not hex is hashed with SHA-256 first): ntfy **replaces** a scheduled message that has the same sequence id, and a delivered one is updated in place by clients that support updating notifications, instead of showing a second copy (not verified on every client: an older app may still show both). Limits:

- it needs **ntfy server v2.16 or later** (updating notifications); an older server ignores the field and the duplicate shows;
- it is per topic: the same key on two topics is two notifications;
- a reminder proposed again by a fresh run gets another `dedupe_key` (and so another `sequence_id`): the [reminder state](#reminder-state) is what keeps it from being pushed again.

### Reminder state

The core sends a `dedupe_key` once, but a pipeline without an LLM step (the `remind` pipeline, every 5 minutes) gets **a new key every run** (the core scopes it to the run when there is no LLM `input_hash`), so the same reminder proposed again by the next run would be pushed again. With `dedupe: daily` (the default) the plugin records each push it hands to ntfy as a **(rule, local day)** pair and skips a push whose pair is already recorded: `details.delivery: duplicate`, still a success (nothing is sent, so the core records its key and moves on).

- **Rule**: the entry's server, topic and priority, and the intent's title, body and link (a SHA-256 of them: no text is stored). A reminder whose text changes (another time, another title) is another rule.
- **Day**: the local date in the instance time zone (`TZ`, set by the host from `horae.yaml`) of **delivery**: now, or when quiet hours end for a delayed push. A reminder delayed at 23:30 to 07:00 counts for the next day, so the runs of the night and of the morning after do not push it again. A reminder pushed at noon and proposed again at 23:30 is pushed for the next day (delivered at 07:00). Daylight saving time needs nothing special: the day is the local date.
- **`critical`** pushes are sent at once and counted for today; **dropped** pushes (quiet hours, `during: drop`) are not recorded.
- **Concurrent runs** take an exclusive lock (`flock`) around the check, the publish and the write: a second run waits (at most 30 s, longer than one publish, else `state_busy`) and then finds the first run's record. A failed publish records nothing, so the next run tries again.
- **Storage**: `ntfy-sent.json` (mode 0600) and `ntfy-sent.lock` in the plugin's single `paths.write` directory. Written atomically (a temporary file in the same directory, flushed, renamed); opened without following symbolic links. A missing file is empty; an unreadable or malformed one starts over (`details.state: reset`: at worst a reminder is pushed once more), bad entries are dropped one by one. Entries older than 7 days are dropped on every write and at most 4096 are kept. If the push went out but the file could not be written, the apply still succeeds with `details.state: not_saved`.

The directory is approved per instance in `horae.lock`, like a self-hosted server: the manifest cannot name an absolute path that depends on where the instance lives. Give the plugin a directory of its own, for example under the instance's `state/`:

```yaml
# horae.lock (excerpt)
plugins:
  ntfy:
    permissions:
      network: [ntfy.example.org]
      secrets: [NTFY_TOKEN]
      paths: { write: [/instance/state/ntfy] }
      intents: [notify.push]
```

Without exactly one approved `paths.write` directory, `dedupe: daily` fails with `state_unavailable` (nothing is sent): a missing grant never silently turns into a push every 5 minutes. `dedupe: off` needs no directory.

### Text and links

The intent's title and body are untrusted text, sent as plain text in the JSON publish API:

- Markdown is never turned on, so `**`, `[x](javascript:...)` or images show as typed;
- control characters and Unicode format characters (bidi overrides, zero-width and invisible tag characters) are removed, except ZWJ and ZWNJ; the title is one line;
- the tag characters of a subdivision flag (🏴 followed by tag letters, e.g. the flag of Scotland) are kept; tag characters anywhere else are removed;
- the body is cut to 4096 bytes of UTF-8 (ntfy's default message limit) and the title to **250 characters**, with `…`. ntfy (from v2.28) refuses a title over 1 KB of UTF-8, and 250 characters are at most 1000 bytes; a phone shows only a line or two of a title anyway. The cut never splits a character from its combining marks, variation selector or skin tone, a ZWJ sequence (no dangling joiner), the two regional indicators of a flag, or a subdivision flag.

The click action is only the intent's `https://` link, which the core validated and routed under the entry's `link_prefix` (checked again here). No other URL, from the text or anywhere else, becomes a click target. An `obsidian://` link is not used as the click action yet (story #4); the push is sent without one, and `details.click` says `dropped`.

**JSON API, not headers.** The plugin posts `{"topic", "message", "title", "priority", "tags", "click", "delay", "sequence_id"}` to `https://<server>/` instead of `POST /<topic>` with `Title:` / `Tags:` headers: HTTP headers carry Latin-1 at best and a line break in them is header injection, while a JSON body carries any UTF-8 text as data.

### Your own server

`plugin.yaml` declares one host, `ntfy.sh`, the default server. `permissions.network` is static in the manifest, so a self-hosted server is approved **per instance**, in `horae.lock`: add its host to this plugin's `permissions.network` there. The core's plugin host runs a plugin with the permissions approved in the lock, which may be a superset of the manifest's (`HostRegistry`), so no fork is needed:

```yaml
# horae.lock (excerpt)
plugins:
  ntfy:
    permissions:
      network: [ntfy.example.org, ntfy.sh]
      secrets: [NTFY_TOKEN]
      intents: [notify.push]
```

**The plugin cannot check `server` against the approved hosts**: a sink is not given the lock's `permissions.network`. Until the core enforces egress (M5), the `server` is trusted pipeline configuration like the rest of the entry: whoever edits the pipeline decides where the notification, and the token, go. (A core follow-up would pass the locked network list in the apply request so a plugin can refuse an undeclared host.) Until M5 nothing enforces network permissions (first-party plugins only). From M5 the egress proxy will enforce them and is planned to refuse names that resolve to private addresses (core threat row T07, #50), which a server on your LAN or Docker network would hit; the reference deployment (self-hosted ntfy beside Horae) will need an answer there.

### Failures

Every failure is a `SinkError` with a fixed message (never the server's answer, a URL with credentials, or the token); the core then fails the apply at that notification (`horae.apply`), which stops the rest of the run's changes.

| Kind | When |
|---|---|
| `config_invalid` | a bad or unknown entry parameter, or quiet hours or `dedupe: daily` without a usable `TZ` |
| `state_unavailable` | `dedupe: daily` without exactly one approved `paths.write` directory, or one the plugin cannot write |
| `state_busy` | another run held the reminder state for more than 30 s |
| `secret_missing`, `secret_invalid` | `auth: token` and `NTFY_TOKEN` is unset, or not a single printable token |
| `intent_invalid` | the link is not an `https://` URL under `link_prefix` (or the intent is malformed) |
| `auth_failed` | 401 or 403 |
| `message_too_large` | 413 |
| `rate_limited` | 429 (not retried) |
| `delay_refused` | 400 to a scheduled publish (during quiet hours); never re-sent without the delay |
| `server_unavailable` | 5xx (not retried) |
| `publish_refused` | another 4xx (for example 400 for a publish that is not scheduled) |
| `redirect_refused` | a 3xx: redirects are never followed |
| `server_timeout` | no answer within 20 s (connect 5 s, read 10 s), under the host's 30 s call timeout |
| `internal` | the `http://` LAN restriction cannot be set up with the installed `httpx2` (the plugin also refuses to start) |
| `network_error` | the connection failed (retried **once**, only when it could not be made, so a push is never sent twice) |
| `host_refused` | an `http://` server that resolves outside the LAN |

**Time.** One push takes at most **20 s** (connect, the one retry when the connection could not be made, sending and reading, all under one deadline). The core applies a run's changes one after another, so a slow or unreachable server holds the rest of the apply for up to 20 s per push (head-of-line blocking). Keep `plugin_defaults.call_timeout_s` at 20 s or more (the default is 30 s): below that the host cancels a publish mid-request, the push is "maybe applied", and only the `sequence_id` keeps a re-run from duplicating it.

The `http://` LAN restriction reaches into `httpx2` internals (the connection pool's network backend), so `httpx2` and `httpcore2` are pinned to one minor version (`2.16.x`); the plugin checks at startup that the internals are there and exits with `internal` otherwise. An `http://` host that resolves to several LAN addresses is tried on each, in order. CGNAT (`100.64.0.0/10`, e.g. Tailscale) is not LAN, as in the core's rule.

The HTTP client ignores the environment (`trust_env=False`: no proxy, `.netrc` or `SSL_CERT_*` variables), verifies TLS, reads at most 64 KiB of the answer (only the message id is kept), and its `httpx2` / `httpcore2` loggers are pinned at `WARNING`.

### What `apply` returns

`{"target": "<server>/<topic>", "after_sha256": null, "details": {...}}`, with `details.delivery` (`sent`, `delayed`, `dropped` or `duplicate`), `details.priority`, the ntfy message `id` when the server returns one, `until` (UTC, by Horae's clock) for a delayed push, `click: "dropped"` when an `obsidian://` link was not sent, and `state` (`reset` or `not_saved`) when the reminder state file was started over or could not be written. The core journals the notification (not undoable) and records its `dedupe_key`.

## Permissions

| Permission | Value | Why |
|---|---|---|
| intent | `notify.push` | the intent type this notifier applies (it proposes none) |
| network | `ntfy.sh` | the default server; a self-hosted host is approved in `horae.lock` (above) |
| secret | `NTFY_TOKEN` | the ntfy access token, sent as `Authorization: Bearer` (`auth: token`); one token for every `ntfy.push` entry for now (per-entry tokens need core support) |
| paths | none in the manifest; **one `paths.write` directory approved in `horae.lock`** | the reminder state (`dedupe: daily`); the path is the instance's, so the lock grants it ([Reminder state](#reminder-state)) |

## Install

Until M4's `horae plugin add`, pin the commit in `horae.lock` with the permissions above, install the package into an interpreter, and set `runtime.command` to that absolute interpreter (`["/opt/horae-plugins/ntfy/bin/python", "-m", "horae_ntfy"]`, horae-sdk README, "The command"). Give the instance an `NTFY_TOKEN` secret (an ntfy access token, `tk_…`), a `timezone` in `horae.yaml`, and approve a state directory for the plugin in `horae.lock` (`paths.write`, see [Reminder state](#reminder-state)).

## Planned work

| Milestone | Epic |
|---|---|
| M3 Dogfood | **ntfy notifier** — Digests, reminders, quiet hours, deep links and action buttons. |

Stories, estimates and acceptance criteria are in this repository's [issues](../../issues) and on the [roadmap project](https://github.com/orgs/horae-dev/projects/1).

## Development

The repository was scaffolded from [`template-plugin`](https://github.com/horae-dev/template-plugin):
an MCP stdio server built on [`horae-sdk`](https://github.com/horae-dev/horae/tree/main/sdk), a
`plugin.yaml` generated from the code, and CI (lockfile, ruff, mypy strict, pytest with an 85%
coverage gate, gitleaks, and the aggregate **CI OK** check). `horae-sdk` is not on PyPI yet:
`pyproject.toml` takes it from the core repository's `main` and `uv.lock` pins the commit.

```
uv sync
uv lock --check && uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest --cov
uv run python -m horae_ntfy --manifest > plugin.yaml   # after changing tools or permissions
```

## Contributing

This project is developed mostly by AI coding agents supervised by a human maintainer. Read [AGENTS.md](AGENTS.md) for the workflow (it applies to humans too), and the organization's [contributing guide](https://github.com/horae-dev/.github/blob/main/CONTRIBUTING.md) and [security policy](https://github.com/horae-dev/.github/blob/main/SECURITY.md).

## License

[Apache-2.0](LICENSE)
