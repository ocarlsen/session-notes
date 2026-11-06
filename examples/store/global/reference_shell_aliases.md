---
name: Sandbox shell aliases
description: interactive aliases leak into scripted shells; call `command rm`
metadata:
  type: reference
---
The shell profile aliases `rm` to `rm -i`. In a scripted call with no TTY the
prompt reads EOF and the delete silently does nothing, with exit 0. Use
`command rm` and check the file is gone.
