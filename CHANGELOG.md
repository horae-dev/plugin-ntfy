# Changelog

## 0.1.0 (unreleased)

- `ntfy.push` notifier: applies `notify.push` intents through the ntfy JSON publish API, with
  the entry parameters `server`, `topic`, `priority` (`min` to `critical`, ntfy 1 to 5),
  `tags`, `auth` and `quiet_hours` (`delay` or `drop`; `critical` always sent).
- Reminder state (`dedupe: daily`, the default): a push is sent once per rule (entry and
  text) and local day of delivery, across runs, with a locked, atomic, bounded state file in
  the plugin's `paths.write` directory approved in `horae.lock`; `delivery: duplicate`
  otherwise. `dedupe: off` keeps no state.
- Permissions: intent `notify.push`, network `ntfy.sh`, secret `NTFY_TOKEN`; one `paths.write`
  directory approved per instance in `horae.lock` for the reminder state.
