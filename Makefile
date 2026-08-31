# The guards' error messages name these targets. A hint that points at a
# command which does not exist is worse than no hint.

PY ?= python3
BASE ?= origin/main

.PHONY: test guards changelog-archive

test:
	$(PY) -m unittest discover -s tests -t . -v

guards:
	BASE_SHA=$$(git merge-base HEAD $(BASE)) \
	HEAD_SHA=$$(git rev-parse HEAD) \
	bash .github/scripts/guards.sh all

# Move every closed minor series out of CHANGELOG.md into docs/changelog/<X.Y>.md,
# keeping only the newest series in the root file.
changelog-archive:
	$(PY) tools/changelog_archive.py
