# Changelog

## 0.2.0 (unreleased)

**Breaking:** `dedupe: daily` is the default, and it needs a state directory approved for the
plugin in `horae.lock`. An instance upgraded without one fails every `ntfy.push` apply with
`state_unavailable` (nothing is sent: it never falls back to a push every 5 minutes).

**Upgrade note:** before upgrading, either approve a directory of the plugin's own, on a local
file system, owned by the plugin's user and not writable by group or others
(`mkdir -m 0700 /instance/state/ntfy`, then
`plugins.ntfy.permissions.paths.write: [/instance/state/ntfy]` in `horae.lock`), or set
`dedupe: off` on each `ntfy.push` entry to keep 0.1.0's behaviour.

- Reminder state (`dedupe: daily`, the default): a rule (entry and text) is pushed once a day
  across runs: not again until a day after its last delivery on the local wall clock, which
  covers the same local day, midnight and the morning after a delayed push. A locked,
  atomic, bounded state file in the plugin's approved `paths.write` directory; `delivery:
  duplicate` otherwise. Pushes dropped during quiet hours are recorded too. `dedupe: off`
  keeps no state.
- `sequence_id` is derived from the rule with `dedupe: daily` (from the core's `dedupe_key`
  with `off`), so a push sent again after an interrupted run replaces the first one on ntfy.
  Delivery is at least once.
- New error kinds `state_unavailable` and `state_busy`; `details.state` (`reset`, `evicted`,
  `not_saved`).
- Permissions: one `paths.write` directory approved per instance in `horae.lock`.

## 0.1.0 (unreleased)

- `ntfy.push` notifier: applies `notify.push` intents through the ntfy JSON publish API, with
  the entry parameters `server`, `topic`, `priority` (`min` to `critical`, ntfy 1 to 5),
  `tags`, `auth` and `quiet_hours` (`delay` or `drop`; `critical` always sent).
- Permissions: intent `notify.push`, network `ntfy.sh`, secret `NTFY_TOKEN`.
