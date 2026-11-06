# Global context (example)

This plays the role of ~/.claude/CLAUDE.md. It loads in every session, so it
holds only an index and pointers; detail lives in the memory files.

@../../store/global/MEMORY.md
@../../store/global/RESUME.md

- RESUME.md is "where we left off". /reflect keeps it current.
- Project-specific memory lives in that project's own store, imported by the
  project's CLAUDE.md, never here.
