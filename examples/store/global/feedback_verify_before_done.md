---
name: Run tests before claiming done
description: a green exit code is not a check; read the output
metadata:
  type: feedback
---
Before saying a change works, run the check and read what it printed. A
script can exit 0 after skipping every test.

**Why:** "done" was claimed twice on a suite that collected zero tests.
**How to apply:** quote the pass count in the reply.
