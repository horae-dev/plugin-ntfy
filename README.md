<p align="center"><img src="https://raw.githubusercontent.com/horae-dev/.github/main/brand/horae-logo-500.png" width="96" alt="Horae logo"></p>

# plugin-ntfy

Horae plugin: push notifications through ntfy, with digests, reminders, quiet hours and action buttons.

> 🚧 **In progress (M3).** `ntfy.push` sends notifications; reminder state (#3) and deep links and action buttons (#4) are not built yet. Follow progress on the [Horae roadmap project](https://github.com/orgs/horae-dev/projects/1).

## What it does

`ntfy.push` is a **notifier**: a pipeline's `notify` step names it, the core routes each `notify.push` intent to it, and after the run the core applies the intent through the plugin's `ntfy.apply` sink operation. The plugin publishes it to an [ntfy](https://ntfy.sh) server with the entry's topic, priority and tags, the access token, and the intent's link as the click action. It proposes nothing and touches no file.

```yaml
# pipelines/remind.yaml (excerpt)
- notify:
    - ntfy.push:
        server: https://ntfy.example.org        # default https://ntfy.sh
        topic: horae-reminders                  # required: A-Z a-z 0-9 _ -, at most 64
        priority: high                          # min | low | normal (default) | high | critical
        tags: [alarm_clock]                     # ntfy tags or emoji short codes, at most 8
        auth: token                             # token (default) | none
        quiet_hours: { start: "22:00", end: "07:00", during: delay }   # during: delay (default) | drop
        link_prefix: https://notes.example.org/daily/                  # links routed here (core)
```

| Parameter | Meaning |
|---|---|
| `server` | `https://<host>[:port]`, no path, query, fragment or user info; the host is a lowercase DNS name (no IP literal, no `localhost`). `http://<host>[:port]` is accepted for a server **on the LAN only**: the host is resolved when connecting and refused unless every address is loopback, RFC 1918 or IPv6 ULA (the rule the core applies to an `http://` LLM endpoint). The token then travels unencrypted on that network: prefer `https`. |
| `topic` | The ntfy topic. It is not a secret: it is stored in run records. On a public server, protect it with an access token and ACLs. |
| `priority` | Mapped to ntfy's 1 to 5: `min` 1, `low` 2, `normal` 3, `high` 4, `critical` 5. Only `critical` bypasses quiet hours. The intent has no priority: it is the entry's (trusted) choice, so use a separate pipeline for critical alerts (two `notify` entries that both take `notify.push` make every push ambiguous for the core). |
| `tags` | ntfy tags; a tag that is an emoji short code shows as that emoji. |
| `auth` | `token`: send `NTFY_TOKEN` as `Authorization: Bearer`; missing is an error, never a silent anonymous publish. `none`: send no credentials. |
| `quiet_hours` | `start` and `end` as `"HH:MM"`, local times in the instance time zone (`horae.yaml` `timezone`, which the host passes as `TZ`); `start > end` crosses midnight. See below. |
| `link_prefix` | Read by the core: a proposed `link` is routed to this entry only if it falls under it. The plugin checks it again. |

Any other key (a typo such as `quiet_hour`) or an invalid value fails the apply with `config_invalid`, naming the parameter, so a feature is never switched off silently.

### Quiet hours

During the window, a push that is not `critical` is:

- **`during: delay`** (default): published now with ntfy's **scheduled delivery** (`delay` set to the Unix time the window ends, at least 10 s ahead), so the phone receives it when quiet hours end. The plugin keeps no state; the server holds the message (this needs ntfy's message cache, on by default).
- **`during: drop`**: not sent.

In both cases `apply` succeeds, so the core records the notification's `dedupe_key` as handled and will not send it again. `critical` is always sent at once. PLAN.md does not say which behaviour quiet hours have; deferring is the default because a reminder that fires at 23:00 is still useful at 07:00, and `drop` is there for pipelines whose pushes go stale (a "now" alert).

The window is read on the **local wall clock**, so daylight saving time does what you expect: the night is an hour shorter in spring and longer in autumn, a repeated autumn hour is quiet on both passes, and an `end` that falls in the spring gap ends when the gap does.

Until #3 (reminder state) lands, a pipeline that proposes the same reminder on every run during quiet hours schedules one delayed copy per run (the core deduplicates within an LLM-reused input, not across fresh runs).

### Text and links

The intent's title and body are untrusted text, sent as plain text in the JSON publish API:

- Markdown is never turned on, so `**`, `[x](javascript:...)` or images show as typed;
- control characters and Unicode format characters (bidi overrides, zero-width and invisible tag characters) are removed, except ZWJ and ZWNJ; the title is one line;
- the body is cut to 4096 bytes of UTF-8 (ntfy's default message limit) and the title to 250 characters, with `…`.

The click action is only the intent's `https://` link, which the core validated and routed under the entry's `link_prefix` (checked again here). No other URL, from the text or anywhere else, becomes a click target. An `obsidian://` link is not used as the click action yet (story #4); the push is sent without one.

**JSON API, not headers.** The plugin posts `{"topic", "message", "title", "priority", "tags", "click", "delay"}` to `https://<server>/` instead of `POST /<topic>` with `Title:` / `Tags:` headers: HTTP headers carry Latin-1 at best and a line break in them is header injection, while a JSON body carries any UTF-8 text as data.

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

Until M5 nothing enforces network permissions (first-party plugins only). From M5 the egress proxy will enforce them and is planned to refuse names that resolve to private addresses (core threat row T07, #50), which a server on your LAN or Docker network would hit; the reference deployment (self-hosted ntfy beside Horae) will need an answer there.

### Failures

Every failure is a `SinkError` with a fixed message (never the server's answer, a URL with credentials, or the token); the core then fails the apply at that notification (`horae.apply`), which stops the rest of the run's changes.

| Kind | When |
|---|---|
| `config_invalid` | a bad or unknown entry parameter, or quiet hours without a usable `TZ` |
| `secret_missing`, `secret_invalid` | `auth: token` and `NTFY_TOKEN` is unset, or not a single printable token |
| `intent_invalid` | the link is not an `https://` URL under `link_prefix` (or the intent is malformed) |
| `auth_failed` | 401 or 403 |
| `message_too_large` | 413 |
| `rate_limited` | 429 (not retried) |
| `server_unavailable` | 5xx (not retried) |
| `publish_refused` | another 4xx |
| `redirect_refused` | a 3xx: redirects are never followed |
| `server_timeout` | no answer within 20 s (connect 5 s, read 10 s), under the host's 30 s call timeout |
| `network_error` | the connection failed (retried **once**, only when it could not be made, so a push is never sent twice) |
| `host_refused` | an `http://` server that resolves outside the LAN |

The HTTP client ignores the environment (`trust_env=False`: no proxy, `.netrc` or `SSL_CERT_*` variables), verifies TLS, reads at most 64 KiB of the answer (only the message id is kept), and its `httpx2` / `httpcore2` loggers are pinned at `WARNING`.

### What `apply` returns

`{"target": "<server>/<topic>", "after_sha256": null, "details": {...}}`, with `details.delivery` (`sent`, `delayed` or `dropped`), `details.priority`, the ntfy message `id` when the server returns one, and `until` (UTC) for a delayed push. The core journals the notification (not undoable) and records its `dedupe_key`.

## Permissions

| Permission | Value | Why |
|---|---|---|
| intent | `notify.push` | the intent type this notifier applies (it proposes none) |
| network | `ntfy.sh` | the default server; a self-hosted host is approved in `horae.lock` (above) |
| secret | `NTFY_TOKEN` | the ntfy access token, sent as `Authorization: Bearer` (`auth: token`) |
| paths | none | no file is read or written |

## Install

Until M4's `horae plugin add`, pin the commit in `horae.lock` with the permissions above, install the package into an interpreter, and set `runtime.command` to that absolute interpreter (`["/opt/horae-plugins/ntfy/bin/python", "-m", "horae_ntfy"]`, horae-sdk README, "The command"). Give the instance an `NTFY_TOKEN` secret (an ntfy access token, `tk_…`) and a `timezone` in `horae.yaml`.

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
