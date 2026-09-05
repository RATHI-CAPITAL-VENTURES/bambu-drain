# The guards' error messages name these targets. A hint that points at a
# command which does not exist is worse than no hint.

PY ?= python3
BASE ?= origin/main

.PHONY: test guards changelog-archive lint

test:
	$(PY) -m unittest discover -s tests -t . -v

# PR_TITLE is passed through so the pr-title guard actually runs locally.
# Without it the guard prints "skipping" and is green here while failing in CI —
# which is how this PR got to merge time with a bad title.
# CI runs shellcheck and it is not otherwise obvious that it will. Two PRs have
# now been rejected for findings that `make lint` would have caught in a second.
lint:
	shellcheck setup/*.sh

guards:
	BASE_SHA=$$(git merge-base HEAD $(BASE)) \
	HEAD_SHA=$$(git rev-parse HEAD) \
	PR_TITLE="$$(gh pr view --json title -q .title 2>/dev/null)" \
	bash .github/scripts/guards.sh all

# Move every closed minor series out of CHANGELOG.md into docs/changelog/<X.Y>.md,
# keeping only the newest series in the root file.
changelog-archive:
	$(PY) tools/changelog_archive.py
