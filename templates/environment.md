<!--
Your environment, described once, for both agents' auto-approval reviewers.
Copy this file to ~/.config/session-notes/environment.md (install.sh offers to), fill it
in, then run:  python3 bin/environment_render.py diff   and then   apply

How it is read (bin/environment_render.py):
  - HTML comments like this one are removed (they may span lines).
  - Every other non-blank line is ONE entry, kept verbatim and in order. Headings
    (### ...) are entries too. Nothing is parsed as Markdown, and wrapped lines are not
    joined, so write each fact as one line.
  - Scaffolding you have not filled in is dropped: any line that still contains the
    placeholder text, and any heading with nothing under it. An untouched copy of this
    file therefore renders to nothing, and `apply` refuses to write nothing.
  - Claude Code gets the entries as autoMode.environment (a list of strings). Codex gets
    them inside the Environment Profile section of its reviewer policy.

Describe facts the reviewer cannot see from the command alone: what is yours, what is
trusted, what is sensitive. Delete any line that does not apply.
-->
### Org-wide
- Organization: <fill in>
- Cloud provider(s): <fill in>
- Repository visibility: <fill in>
- Internal sharing / snippet hosting: <fill in>
- Secrets management: <fill in>
- Default / protected branches: <fill in>
- CI/CD deploy targets: <fill in>
- Network posture: <fill in>
- Host containment: <fill in>
- Source control: <fill in>
- Trusted internal domains: <fill in>
- Sensitive data locations and audiences: <fill in>
- Sensitive remote targets: <fill in>
- Protected IaC scopes: <fill in>

### User-specific
- Primary use: <fill in>
- Trusted repo: <fill in>
- Org-specific CLIs: <fill in>
- Routine command prefixes: <fill in>
