.PHONY: test leak check hooks drift selftest-live selftest-watch

# Everything CI runs, plus the private-term leak scan.
check: test leak

test:
	sh tests/run_selftests.sh
	python3 tests/test_git_env.py

leak:
	python3 tests/leak_scan.py

# Upstream changes not yet ported or marked local-only (maintainers with an upstream config).
drift:
	python3 tools/upstream_drift.py check

# Point git at the repo's hooks (pre-commit leak scan + commit-msg scan).
hooks:
	git config core.hooksPath .githooks

# Real Claude Code and Codex CLI sessions in a sandbox (costs API credits, needs Keychain keys; not
# part of check). See tests/live/selftest_live.py and docs/tools.md.
selftest-live:
	python3 tests/live/selftest_live.py

# The same live run built to be watched and filmed: `tmux -L sn-watch attach` while it runs.
# Pass options through ARGS, e.g. make selftest-watch ARGS="--pace 2 --step".
selftest-watch:
	python3 tests/live/selftest_live.py --watch $(ARGS)
