## session-notes memory

This agent's memory lives in ~/.codex/session-notes and is written only by $reflect (or $rnr).
When asked to "remember" something, do not write files there by hand: say it will be saved at
the next $reflect, or run $reflect if the user asks for it now.
