#!/usr/bin/env bash
#
# Self-test for guards.sh, run against synthetic git repos.
#
# Every case builds a throwaway repo, commits a base state, commits a head
# state, and asserts the guard's exit code. That is the whole point of guards
# being a script: these run in milliseconds, where testing them through real PRs
# would cost a push and a CI run per case.
#
# Each case asserts the exit code AND, where it matters, that the message names
# the thing that is wrong. A guard that fails for the right reason with the wrong
# explanation is a guard people learn to ignore.
#
# Usage: guards.test.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GUARDS="$HERE/guards.sh"
PASS=0; FAIL=0

# ---------------------------------------------------------------------------

setup() {
  REPO=$(mktemp -d)
  cd "$REPO" || exit 1
  git init -q .
  git config user.email t@t.t; git config user.name t
  git config commit.gpgsign false
  mkdir -p src docs/retros docs/changelog tests .github/scripts
  cp "$HERE/guards.sh" .github/scripts/guards.sh
  cat > .github/scripts/guards.conf <<'CONF'
APP="."
VERSION_FILE="./VERSION"
VERSION_FIELDS=3
CHANGELOG="./CHANGELOG.md"
CHANGELOG_ARCHIVE_DIR="./docs/changelog"
RETRO_DIR="./docs/retros"
RETRO_DEBT_MAX=8
DOCS_CODE_PATTERN="^src/.*\.py$"
DOCS_TARGET_PATTERN="^docs/.*\.md$"
TESTS_CODE_PATTERN="^src/.*\.py$"
TESTS_TARGET_PATTERN="^tests/.*\.py$"
CAPABILITY_ADD_PATTERN="^src/tools/[^/]+\.py$"
VERSION_MIRRORS=""
SCOPE_RULES="app:^src/"
CONF
  printf '1.0.0\n' > VERSION
  printf '# Changelog\n\n## 1.0.0 — 2026-01-01\n\n- base\n' > CHANGELOG.md
  echo base > src/thing.py
  git add -A >/dev/null; git commit -qm "base"
  BASE=$(git rev-parse HEAD)
}

teardown() { cd /; rm -rf "$REPO"; }

# run <guard> [LABELS] -> sets OUT and RC
run() {
  local guard="$1" labels="${2:-}"
  OUT=$(BASE_SHA="$BASE" HEAD_SHA="$(git rev-parse HEAD)" LABELS="$labels" \
        PR_TITLE="${PR_TITLE:-}" \
        GUARDS_CONF="$REPO/.github/scripts/guards.conf" \
        bash .github/scripts/guards.sh "$guard" 2>&1)
  RC=$?
}

ok()   { if [ "$RC" -eq 0 ]; then PASS=$((PASS+1)); echo "  ok   $1"; else FAIL=$((FAIL+1)); echo "  FAIL $1 (expected pass)"; echo "$OUT" | sed 's/^/       /'; fi; }
bad()  { if [ "$RC" -ne 0 ]; then PASS=$((PASS+1)); echo "  ok   $1"; else FAIL=$((FAIL+1)); echo "  FAIL $1 (expected fail)"; echo "$OUT" | sed 's/^/       /'; fi; }
says() { if printf '%s' "$OUT" | grep -qF "$2"; then PASS=$((PASS+1)); echo "  ok   $1"; else FAIL=$((FAIL+1)); echo "  FAIL $1 — message never mentions '$2'"; echo "$OUT" | sed 's/^/       /'; fi; }

commit() { git add -A >/dev/null; git commit -qm "$1"; }
bump()   { printf '%s\n' "$1" > VERSION; }
entry()  { printf '# Changelog\n\n## %s — 2026-01-02\n\n%s\n\n## 1.0.0 — 2026-01-01\n\n- base\n' "$1" "${2:-- change}" > CHANGELOG.md; }

# ---------------------------------------------------------------------------
echo "docs"
setup
  echo more >> src/thing.py; commit "code only"
  run docs;                     bad  "code with no doc fails"
  says "names the offender" "src/thing.py"
  run docs docs-exempt;         ok   "docs-exempt label passes"
teardown

setup
  echo more >> src/thing.py; echo x > docs/note.md; commit "code + doc"
  run docs;                     ok   "code with a doc passes"
teardown

setup
  echo more >> src/thing.py; commit "code [skip docs]"
  run docs;                     ok   "[skip docs] in the subject passes"
teardown

setup
  # The failure this exists for: a commit BODY that merely explains the tag.
  echo more >> src/thing.py
  git add -A >/dev/null
  git commit -q -m "code" -m "You can bypass this with [skip docs] mid-sentence."
  run docs;                     bad  "a tag discussed in prose does NOT exempt"
teardown

echo "tests"
setup
  echo more >> src/thing.py; commit "code only"
  run tests;                    bad  "code with no test fails"
  echo t > tests/test_thing.py; commit "add test"
  run tests;                    ok   "code with a test passes"
teardown

echo "changelog"
setup
  echo more >> src/thing.py; commit "no changelog"
  run changelog;                bad  "untouched changelog fails"
teardown

setup
  bump 1.0.1; entry 1.0.1; commit "bump"
  run changelog;                ok   "matching VERSION and entry passes"
teardown

setup
  bump 1.0.1; entry 1.0.2; commit "mismatch"
  run changelog;                bad  "VERSION disagreeing with the header fails"
  says "explains the mismatch" "does not match"
teardown

setup
  # Appending a bullet to an already-released entry.
  printf '# Changelog\n\n## 1.0.0 — 2026-01-01\n\n- base\n- snuck in\n' > CHANGELOG.md
  commit "append to a shipped entry"
  run changelog;                bad  "editing a released entry fails"
  says "says the entry is closed" "already exists on the base branch"
teardown

setup
  bump 0.9.0; entry 0.9.0; commit "go backwards"
  run changelog;                bad  "a non-monotonic version fails"
teardown

echo "changelog-archive"
setup
  printf '# Changelog\n\n## 1.1.0 — d\n\n- a\n\n## 1.0.0 — d\n\n- b\n' > CHANGELOG.md
  commit "two series"
  run changelog-archive;        bad  "two minor series fails"
teardown

echo "version-sync"
setup
  printf '{"version": "1.0.0"}\n' > package.json
  sed -i.bak 's|VERSION_MIRRORS=""|VERSION_MIRRORS="./package.json"|' .github/scripts/guards.conf
  commit "mirror in sync"
  run version-sync;             ok   "matching mirror passes"
  printf '{"version": "0.4.1"}\n' > package.json; commit "drift"
  run version-sync;             bad  "a drifted mirror fails"
  says "names both values" "0.4.1"
teardown

echo "bump-level"
setup
  mkdir -p src/tools; echo new > src/tools/brand_new.py
  bump 1.0.1; entry 1.0.1; commit "patch + new tool"
  run bump-level;               bad  "PATCH adding a capability surface is challenged"
  run bump-level patch-intentional; ok "patch-intentional passes"
teardown

setup
  bump 2.0.0; entry 2.0.0; commit "major, no breaking section"
  run bump-level;               bad  "MAJOR with no Breaking section fails"
  entry 2.0.0 "### Breaking

- the thing"; commit "add breaking"
  run bump-level;               ok   "MAJOR documenting what breaks passes"
teardown

echo "adoption (no VERSION on the base side)"
setup
  # The PR that ADOPTS these guards adds VERSION and CHANGELOG.md. If that PR
  # cannot go green, the guards get deleted on day one.
  git rm -q --cached VERSION >/dev/null; rm VERSION
  git commit -qm "pretend VERSION never existed"
  BASE=$(git rev-parse HEAD)
  printf '1.0.0\n' > VERSION; commit "add VERSION"
  run bump-level;               ok   "bump-level survives a brand-new VERSION"
  run retro;                    ok   "retro survives a brand-new VERSION"
  says "says why" "new in this PR"
teardown

echo "retro"
setup
  bump 1.1.0; entry 1.1.0; commit "minor, no retro"
  run retro;                    bad  "MINOR with no retro fails"
  echo r > docs/retros/2026-01-02-thing.md; commit "add retro"
  run retro;                    ok   "MINOR with a retro passes"
teardown

setup
  # A MAJOR resets MINOR to 0. Comparing MINOR alone reads 1 -> 0 as "went
  # backwards" and lets the biggest milestone there is ship retro-free.
  bump 1.1.0; entry 1.1.0; echo r > docs/retros/a.md; commit "minor"
  BASE=$(git rev-parse HEAD)
  bump 2.0.0; entry 2.0.0 "### Breaking

- x"; commit "major, no retro"
  run retro;                    bad  "MAJOR with no retro fails (MINOR reset to 0)"
teardown

setup
  bump 1.0.1; entry 1.0.1; commit "patch"
  run retro;                    ok   "PATCH is untaxed"
teardown

setup
  # README/TEMPLATE in the retro dir must not count as shipping a retro.
  bump 1.1.0; entry 1.1.0; echo x > docs/retros/README.md; commit "minor + readme"
  run retro;                    bad  "a README does not count as a retro"
teardown

echo "retro-debt"
setup
  {
    echo "# Changelog"; echo
    for p in 0 1 2 3 4 5 6 7 8 9; do echo "## 1.0.$p — d"; echo; echo "- x"; echo; done
  } > CHANGELOG.md
  bump 1.0.9; commit "long line"
  run retro-debt;               bad  "past the cap forces a milestone"
  run retro-debt retro-exempt;  ok   "retro-exempt passes"
teardown

echo "followups"
setup
  cat > docs/retros/r.md <<'MD'
# Retro

## Gaps found

| Gap | Kind | Follow-up | Status |
| --- | --- | --- | --- |
| a | docs | write it | deferred |
MD
  commit "bare deferred"
  run followups;                bad  "a bare 'deferred' fails"
  says "quotes the offending status" "deferred"
teardown

setup
  cat > docs/retros/r.md <<'MD'
# Retro

## Gaps found

| Gap | Kind | Follow-up | Status |
| --- | --- | --- | --- |
| a | docs | write it | landed here |
| b | test | do it | blocked: waiting on hardware |
| c | proc | file it | tracked: #12 |
MD
  commit "all owned"
  run followups;                ok   "landed / blocked-with-reason / tracked-with-number passes"
teardown

setup
  cat > docs/retros/r.md <<'MD'
# Retro

## Gaps found

| Gap | Kind | Follow-up | Status |
| --- | --- | --- | --- |
| a | docs | x | blocked |
| b | docs | y | tracked |
MD
  commit "empty promises"
  run followups;                bad  "'blocked' with no reason and 'tracked' with no number fail"
teardown

setup
  # The same issue cited by two retros is ONE piece of work.
  for i in 1 2; do
    cat > "docs/retros/r$i.md" <<MD
# Retro $i

## Gaps found

| Gap | Kind | Follow-up | Status |
| --- | --- | --- | --- |
| a | docs | x | tracked: #7 |
MD
  done
  commit "same issue twice"
  run followups;                ok   "a duplicate issue number is counted once"
  says "counts one" "1 open issue"
teardown

setup
  {
    echo "# Retro"; echo; echo "## Gaps found"; echo
    echo "| Gap | Kind | Follow-up | Status |"
    echo "| --- | --- | --- | --- |"
    for i in $(seq 1 9); do echo "| g$i | docs | x | tracked: #$i |"; done
  } > docs/retros/r.md
  commit "nine issues"
  run followups;                bad  "more open issues than the cap fails"
  says "explains the cap" "not the same as doing the work"
teardown

echo "pr-title"
setup
  bump 1.0.1; entry 1.0.1; commit "v1.0.1 fix: a thing"
  PR_TITLE="v1.0.1 fix: a thing" run pr-title;  ok  "matching title and subject pass"
  PR_TITLE="fix: a thing"       run pr-title;   bad "a title with no version fails"
  PR_TITLE="v1.0.1 thing"       run pr-title;   bad "a title with no conventional type fails"
teardown

setup
  bump 1.0.1; entry 1.0.1; commit "fix: forgot the version"
  PR_TITLE="v1.0.1 fix: a thing" run pr-title
  bad  "single-commit PR with an unversioned SUBJECT fails"
  says "explains why the subject matters" "COMMIT SUBJECT"
teardown

echo "scope"
setup
  echo more >> src/thing.py; commit "touch src"
  run scope; ok "scope never fails"
  says "emits the app output" "app=true"
teardown

# ---------------------------------------------------------------------------
echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
