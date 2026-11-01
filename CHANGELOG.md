# Changelog

## 0.1.0 (unreleased)

- `ntfy.push` notifier: applies `notify.push` intents through the ntfy JSON publish API, with
  the entry parameters `server`, `topic`, `priority` (`min` to `critical`, ntfy 1 to 5),
  `tags`, `auth` and `quiet_hours` (`delay` or `drop`; `critical` always sent).
- Permissions: intent `notify.push`, network `ntfy.sh`, secret `NTFY_TOKEN`.
