---
name: commit-message
description: "Synthetic example skill for the skill_feedback.py quickstart. Writes a git commit message for the staged diff: an imperative subject line, and a body only when the diff needs one sentence of context."
---

# /commit-message (example skill)

Write one commit message for the currently staged diff.

1. Read the staged diff.
2. Write a subject line in the imperative mood ("Add retry logic", not "Added" or
   "Adds"), under about 70 characters, naming the change, not the file.
3. Add a body only when the diff's purpose isn't obvious from the subject alone. Keep
   it to the reasoning a reviewer would otherwise have to reconstruct from the diff.

This is a synthetic skill shipped only to demonstrate `bin/skill_feedback.py propose`
against a real-shaped ledger (`feedback.jsonl`, beside this file). It is not installed
by `install.sh` and does nothing in a real session.
