# session-notes: design notes

These are the reasons behind choices that look odd at first.

- **Delivery is at least once, so a message can arrive twice.** The bus hook
  has to mark a message delivered when the prompt is submitted, before it can
  know whether the turn completes. An interrupted turn drops its injected
  context entirely, so a mark-once design silently loses mail on a routine
  keystroke. Each message is offered on two prompts instead, the repeat
  tagged as a re-delivery.
- **Emit first, record second.** The hook prints the injection, then records
  the offer. A crash in between costs a duplicate, never a lost message.
- **The empty-bus path does almost nothing.** The hook runs on every
  prompt in every session, so while no unexpired message exists anywhere on
  the bus it reads one small file and returns without shelling out to tmux.
  A hung tmux server would otherwise add seconds to every prompt you type.
- **Hooks never block a prompt.** Every exception is caught so a broken hook
  cannot stop you working. The bus hook also writes the traceback to the
  state dir, where `session_bus.py peers` and `send` surface it, because a
  silently swallowed error makes a broken bus look identical to an empty
  one. The other hooks fail silently today.
- **Attribution is structural.** Before the bus, the user relayed text between
  panes by hand, and a relayed error once got recorded in memory as the
  user's own words. Every message carries a `from`, and every body is
  flattened to one line and fenced, so a message cannot forge a second
  sender line or a system notice.
- **Observing is not driving.** `session_peers.py` only reads. The one tool
  that types into panes, `bcast`, is run by the human, prefixes prose with an
  attribution marker and logs every send. `bus_nudge` types a single fixed
  string (never message content) into idle panes with unread mail, only when
  the input box is empty, and at most ten times per session per hour.
- **Ghost text is not typed text.** Claude Code shows a dimmed suggestion in
  the input box. A plain `capture-pane` drops styling, so the suggestion
  reads like a half-typed reply. `peek` keeps the escapes, labels the dim
  tail as a suggestion, then strips styling.
- **A session name is not an identity.** tmux session numbers restart at 0
  with the server, so bus state is keyed on (name, creation time).
- **Prevent, don't just report.** Index lines grow as corrections get
  appended. The only writer, `append_memory_index.py`, refuses an over-cap
  line before writing, rather than leaving a lint for someone to skip.
- **Config fails loudly.** An unknown key or a wrong type in `config.toml` is
  an error naming the key, never a silent default.

## Keeping a fork in step with its upstream

If you extract your own copy from a Claude Code config you keep in git, as this repo was,
`tools/upstream_drift.py` tells you when upstream changes a file you share. `tools/upstream.toml`
maps each file here to its upstream path; a private watermark per file (outside the repo)
records the last upstream commit you decided about. `make drift` lists every upstream commit
since, and each one ends as `mark FILE ported` or `mark FILE local-only`. A new file
that is in neither the map nor `repo_only` fails the check, so it is a decision too.
