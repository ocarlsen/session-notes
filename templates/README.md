# templates

- `environment.md`: a blank description of your environment. Copy it to
  `~/.config/session-notes/environment.md`, fill it in, and render it with
  `bin/environment_render.py`.
- `codex-auto-review-policy.md`: an exact, unmodified copy of Codex's bundled guardian
  policy (`codex-rs/prompts/templates/guardian/policy.md` at tag `rust-v0.171.0`,
  Copyright OpenAI, Apache-2.0; see `NOTICE`). `environment_render.py` inserts your
  environment into its Environment Profile section to build the Codex `[auto_review]`
  policy, and refuses to run if this file's sha256 is not the pinned one. Do not edit it:
  to move to a newer policy, follow "Updating the pin" in `docs/codex.md`.
