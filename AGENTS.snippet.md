## session-notes memory

This agent's memory lives in ~/.codex/session-notes and is written only by $reflect (or $rnr).
When asked to "remember" something, do not write files there by hand: say it will be saved at
the next $reflect, or run $reflect if the user asks for it now.

## Session tools (session-notes)
- Hand a task to a new session: handoff_session --name NAME --prompt "the task".
  Never type into a session that already exists.
- Who is running, who launched whom, what a session shows: session_peers [--tree | --peek NAME | --me]
- bcast is the user's command, never yours: it types into other sessions as them.

## Codex and other sessions
- Never run `tmux send-keys` (or anything else that types) into another pane. If
  `session_bus.py send` fails, tell the user what you wanted to send instead of finding
  another way to reach that session.
- Codex's sandbox refuses the tmux socket, so tmux commands (session_peers included) need
  to run outside it: ask for approval, or see docs/codex.md for allow rules.
