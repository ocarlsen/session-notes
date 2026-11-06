.PHONY: test leak check hooks drift

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
