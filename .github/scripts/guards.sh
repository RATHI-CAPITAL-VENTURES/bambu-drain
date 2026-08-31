#!/usr/bin/env bash
#
# Shipping guards — the reusable core, extracted from RIA and Claros.
#
# These decide whether a PR can merge. They exist because both repos ship many
# times a day, mostly agent-authored, and the things that rot first under that
# pace are the things nobody is forced to do: the changelog entry, the doc, the
# test, the retro. Every guard here started as a rule written in a CLAUDE.md
# that was then quietly not followed.
#
# WHY A SCRIPT AND NOT INLINE YAML: inline, none of this can be tested without
# pushing a real PR per case, which is slow and is exactly the CI spend we are
# trying to cut. Here it runs against synthetic git history in milliseconds.
# See guards.test.sh.
#
# WHAT BELONGS HERE: judgements about how to ship, which every project gets.
# Facts about one project go in guards.conf. Guards that only make sense for one
# project go in guards.d/*.sh, which is sourced if present — that is how RIA's
# artifact gate and Claros's AI-access guard stay out of this file.
#
# CONTRACT: each subcommand exits 0 (pass) or 1 (fail) and explains itself on
# stdout. Reads BASE_SHA, HEAD_SHA and LABELS (comma-joined PR labels) from the
# environment. File-content checks read the WORKING TREE, which CI has checked
# out at HEAD.
#
# Usage: guards.sh <name>|all      (guards.sh list  — what's available here)

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
GUARDS_CONF="${GUARDS_CONF:-$HERE/guards.conf}"
if [ ! -f "$GUARDS_CONF" ]; then
  echo "::error::No guards.conf at $GUARDS_CONF. Copy guards.conf.example and edit it, or set GUARDS_CONF." >&2
  exit 1
fi

# Available to the config while it is being sourced.
#
# Git reports paths from the repo root with no leading "./", so a root-level
# project whose APP is "." must produce an EMPTY prefix, not "./". Everything
# here is matched with anchored regexes, where "^./CHANGELOG.md$" quietly fails
# to match "CHANGELOG.md" — and "." matching any character means it does not
# even fail loudly. Every path guard in the first version of this template was
# broken exactly that way for root-level repos.
app_prefix() { case "${1:-.}" in .|""|./) echo "" ;; *) echo "${1%/}/" ;; esac; }

# Strip a leading ./ that a hand-written config may still carry, so a config
# written the obvious way keeps working.
_norm() { printf '%s' "${1#./}"; }

# shellcheck source=/dev/null
. "$GUARDS_CONF"

APP_PREFIX="${APP_PREFIX:-$(app_prefix "${APP:-.}")}"

: "${APP:=.}"
: "${VERSION_FIELDS:=3}"
: "${VERSION_FILE:=$APP/VERSION}"
: "${VERSION_MIRRORS:=}"
: "${CHANGELOG:=$APP/CHANGELOG.md}"
: "${CHANGELOG_ARCHIVE_DIR:=$APP/docs/changelog}"
: "${CHANGELOG_ARCHIVE_CMD:=make changelog-archive}"
: "${RETRO_DIR:=$APP/docs/retros}"
: "${RETRO_DEBT_MAX:=8}"
: "${PR_TITLE_TYPES:=feat|fix|docs|refactor|perf|test|chore|build|ci|revert}"
: "${ALLOW_COMMIT_TAGS:=1}"
: "${DOCS_CODE_PATTERN:=}"
: "${DOCS_TARGET_PATTERN:=}"
: "${DOCS_HINT:=}"
: "${TESTS_CODE_PATTERN:=}"
: "${TESTS_TARGET_PATTERN:=}"
: "${CAPABILITY_ADD_PATTERN:=}"
: "${SCOPE_RULES:=}"

VERSION_FILE=$(_norm "$VERSION_FILE")
CHANGELOG=$(_norm "$CHANGELOG")
CHANGELOG_ARCHIVE_DIR=$(_norm "$CHANGELOG_ARCHIVE_DIR")
RETRO_DIR=$(_norm "$RETRO_DIR")

: "${BASE_SHA:?BASE_SHA is required}"
: "${HEAD_SHA:?HEAD_SHA is required}"
LABELS="${LABELS:-}"

MERGE_BASE="$(git merge-base "$BASE_SHA" "$HEAD_SHA")"
CHANGED="$(git diff --name-only "$MERGE_BASE" "$HEAD_SHA")"

# A version regex with the configured number of fields, so a 3-field project
# cannot accidentally accept a 4-field header and vice versa. Getting this wrong
# is silent: the header parses, the comparison is against the wrong thing.
VER_RE="[0-9]+(\.[0-9]+){$((VERSION_FIELDS - 1))}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Exact match against the comma-joined label list, so 'docs-exempt' can never be
# satisfied by a label that merely CONTAINS it as a substring.
has_label() { printf ',%s,' "$LABELS" | grep -q ",$1,"; }

# Commit-message escape hatch, e.g. [skip docs].
#
# The tag must be in a commit SUBJECT, or alone on its own line in a body. A
# plain substring search over the whole message was the first version and it was
# wrong in the most embarrassing way available: the commit that INTRODUCED these
# guards explained the tags in its body and thereby bypassed three of its own
# guards. Prose discussing a tag is always inline mid-sentence; a deliberate
# opt-out is a subject suffix or a line of its own. That separates them, and
# keeps a doc that merely NAMES an escape hatch from silently using it.
has_tag() {
  [ "$ALLOW_COMMIT_TAGS" = "1" ] || return 1
  local tag="$1" esc
  if git log "${MERGE_BASE}..${HEAD_SHA}" --format=%s | grep -qiF "$tag"; then
    return 0
  fi
  esc=$(printf '%s' "$tag" | sed 's/[][\.*^$\/]/\\&/g')
  git log "${MERGE_BASE}..${HEAD_SHA}" --format=%b \
    | grep -qiE "^[[:space:]]*${esc}[[:space:]]*$"
}

# `exempt <label> <tag>` — true if either form opted out. Prints WHICH one, so a
# passing guard always says why it passed. A guard that prints only "✓" when it
# was skipped is how an exemption becomes permanent without anyone deciding to.
exempt() {
  if has_label "$1"; then echo "✓ '$1' label present — skipping."; return 0; fi
  if has_tag "$2"; then echo "✓ '$2' found in a commit message — skipping."; return 0; fi
  return 1
}

# Emit a step output under Actions; print it otherwise, so the test suite and a
# human running this locally both see the value.
emit() {
  if [ -n "${GITHUB_OUTPUT:-}" ]; then echo "$1=$2" >> "$GITHUB_OUTPUT"; fi
  echo "output: $1=$2"
}

# Version reads. The BASE side comes from git (the merge-base commit); the HEAD
# side comes from the WORKING TREE, which CI has checked out at HEAD.
#
# Reading the head side from `git show "$HEAD_SHA:..."` would be equivalent under
# CI and silently wrong when a human runs the guards with uncommitted changes:
# a working-tree VERSION compared against a committed MINOR reports a real MINOR
# bump as a PATCH. One source per side, consistently.
_head_version() { tr -d '[:space:]' < "$VERSION_FILE"; }
# `|| true` is load-bearing: under `set -o pipefail` a missing file at the base
# commit makes git exit non-zero, which takes the pipeline down with it and kills
# the whole script inside a command substitution — silently, with no output. An
# absent base version is a normal state (the PR that adds the file), not an
# error, so it must read as empty rather than as a crash.
_base_version() { { git show "$MERGE_BASE:$VERSION_FILE" 2>/dev/null || true; } | tr -d '[:space:]'; }
_ver_field() { printf '%s' "$1" | cut -d. -f"$2"; }
_head_major() { _ver_field "$(_head_version)" 1; }
_head_minor() { _ver_field "$(_head_version)" 2; }
_base_major() { _ver_field "$(_base_version)" 1; }
_base_minor() { _ver_field "$(_base_version)" 2; }

# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

# Which expensive jobs does this diff justify? Pure output, never fails.
cmd_scope() {
  echo "Changed files:"; echo "$CHANGED"
  [ -n "$SCOPE_RULES" ] || { echo "(no SCOPE_RULES configured)"; return 0; }
  local line name pat
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    name="${line%%:*}"; pat="${line#*:}"
    if echo "$CHANGED" | grep -qE "$pat"; then
      emit "$name" true
    else
      emit "$name" false
    fi
  done <<< "$SCOPE_RULES"
}

# A change to capability code must ship a doc update in the SAME change.
#
# Not because docs are virtuous, but because an investigation whose findings
# live only in a chat log gets re-run from scratch in three weeks — by the next
# agent, at full cost, reaching the same conclusion.
cmd_docs() {
  [ -n "$DOCS_CODE_PATTERN" ] || { echo "✓ docs guard not configured."; return 0; }
  if exempt docs-exempt '[skip docs]'; then return 0; fi
  local code docs
  code=$(echo "$CHANGED" | grep -E "$DOCS_CODE_PATTERN" || true)
  docs=$(echo "$CHANGED" | grep -E "$DOCS_TARGET_PATTERN" || true)
  if [ -n "$code" ] && [ -z "$docs" ]; then
    echo "::error::Code changed but no docs were updated."
    [ -n "$DOCS_HINT" ] && echo "$DOCS_HINT"
    echo "Or apply the 'docs-exempt' label${ALLOW_COMMIT_TAGS:+ / add [skip docs] to a commit message}."
    echo "Offending code files:"; echo "$code"
    return 1
  fi
  echo "✓ docs presence guard passed."
}

# Any change to code must add or update a test, so fast iteration cannot quietly
# regress coverage.
cmd_tests() {
  [ -n "$TESTS_CODE_PATTERN" ] || { echo "✓ tests guard not configured."; return 0; }
  if exempt tests-exempt '[skip tests]'; then return 0; fi
  local code tests
  code=$(echo "$CHANGED" | grep -E "$TESTS_CODE_PATTERN" || true)
  tests=$(echo "$CHANGED" | grep -E "$TESTS_TARGET_PATTERN" || true)
  if [ -n "$code" ] && [ -z "$tests" ]; then
    echo "::error::Code changed but no test was added or updated."
    echo "Add or update a test alongside the change, or apply 'tests-exempt' if it genuinely needs none."
    echo "Offending code files:"; echo "$code"
    return 1
  fi
  echo "✓ tests presence guard passed."
}

# Every PR records what changed, so history stays a complete log however fast the
# project ships — and when an agent is shipping, "I'll write it up later" has no
# author to fall back on.
cmd_changelog() {
  if exempt changelog-exempt '[skip changelog]'; then return 0; fi
  if echo "$CHANGED" | grep -qE "^$CHANGELOG$"; then
    echo "✓ $CHANGELOG updated."
  else
    echo "::error::This PR does not update $CHANGELOG. Add a '## X.Y.Z — date' entry (and bump $VERSION_FILE), or apply 'changelog-exempt' for a pure infra/no-op change."
    return 1
  fi

  # Presence is not enough: an entry can ship while VERSION stays stale. Both
  # repos have caught exactly that. Assert the invariant directly.
  local top ver base_top
  top=$(grep -m1 -oE "^## $VER_RE" "$CHANGELOG" | awk '{print $2}' || true)
  ver=$(tr -d '[:space:]' < "$VERSION_FILE")
  if [ -z "$top" ]; then
    echo "::error::$CHANGELOG has no parseable top version header (expected '## ${VERSION_FIELDS}-field version — date')."
    return 1
  fi
  if [ "$top" != "$ver" ]; then
    echo "::error::$VERSION_FILE ($ver) does not match the top $CHANGELOG header ($top). Bump VERSION together with the changelog entry."
    return 1
  fi
  echo "✓ VERSION ($ver) matches top CHANGELOG header."

  # The top header must be NEW relative to base. Appending to an already-released
  # entry is the failure this catches: a PR adding two pages, an API change and a
  # single bullet under a shipped version, with every guard green in six seconds.
  # That happened. An unbumped PR now either edits no changelog (failing the
  # presence check above) or carries changelog-exempt and ships nothing.
  base_top=$(git show "$MERGE_BASE:$CHANGELOG" 2>/dev/null | grep -m1 -oE "^## $VER_RE" | awk '{print $2}' || true)
  if [ -n "$base_top" ]; then
    if [ "$top" = "$base_top" ]; then
      echo "::error::The top $CHANGELOG entry ($top) already exists on the base branch. Open a NEW '## X.Y.Z' entry and bump $VERSION_FILE — a released entry is closed."
      return 1
    fi
    if [ "$(printf '%s\n%s\n' "$base_top" "$top" | sort -V | tail -1)" != "$top" ]; then
      echo "::error::New CHANGELOG version ($top) is not greater than the previous one ($base_top)."
      return 1
    fi
    echo "✓ Version bump is monotonic ($base_top → $top)."
  fi
}

# CHANGELOG.md holds the current MINOR series only; older series move to
# CHANGELOG_ARCHIVE_DIR. Per-minor rather than a line budget, because a line
# budget splits mid-series and "where is 2.0.4?" stops having one answer.
#
# This matters more than it looks when agents read the repo: the changelog is
# injected context, so its length is a per-turn cost on every session.
cmd_changelog_archive() {
  local series count
  if [ ! -f "$CHANGELOG" ]; then
    echo "::error::$CHANGELOG not found."; return 1
  fi
  series=$(grep -oE '^## [0-9]+\.[0-9]+' "$CHANGELOG" | awk '{print $2}' | sort -u -V)
  count=$(printf '%s\n' "$series" | grep -c '[0-9]' || true)
  if [ "$count" -le 1 ]; then
    echo "✓ $CHANGELOG holds a single minor series."; return 0
  fi
  echo "::error::$CHANGELOG holds $count minor series ($(printf '%s' "$series" | tr '\n' ' ')). Run '$CHANGELOG_ARCHIVE_CMD' to move the older ones into $CHANGELOG_ARCHIVE_DIR/."
  return 1
}

# The canonical version file and any file that mirrors it must agree. No exempt
# label: they either agree or one of them is a lie.
cmd_version_sync() {
  [ -n "$VERSION_MIRRORS" ] || { echo "✓ no version mirrors configured."; return 0; }
  local ver rc=0 f found
  ver=$(_head_version)
  for f in $VERSION_MIRRORS; do
    if [ ! -f "$f" ]; then
      echo "::error::VERSION_MIRRORS names $f, which does not exist."; rc=1; continue
    fi
    found=$(grep -oE "$VER_RE" "$f" | head -1 || true)
    if [ "$found" != "$ver" ]; then
      echo "::error::$f says '$found' but $VERSION_FILE says '$ver'. They must agree."
      rc=1
    fi
  done
  [ "$rc" -eq 0 ] && echo "✓ every version mirror matches $VERSION_FILE ($ver)."
  return $rc
}

# The bump LEVEL is a semantic call — "is this new capability, and does it break
# anything?" — so this guard deliberately does not make it. It challenges a
# level that looks wrong against structural evidence, and makes a MAJOR justify
# itself in writing.
cmd_bump_level() {
  local base_ver head_ver base_major head_major base_minor head_minor signals
  base_ver=$(_base_version); head_ver=$(_head_version)
  if [ -z "$head_ver" ]; then
    echo "::error::Could not read $VERSION_FILE at HEAD."; return 1
  fi
  # No version on the base side means this PR is ADDING it — the first release,
  # or the PR that adopts these guards. There is nothing to compare a bump
  # against, and failing here would mean the template's own adoption PR could
  # never go green, which is a good way to have the guards deleted on day one.
  if [ -z "$base_ver" ]; then
    echo "✓ $VERSION_FILE is new in this PR ($head_ver) — nothing to compare."
    return 0
  fi
  echo "VERSION: $base_ver → $head_ver"
  if [ "$base_ver" = "$head_ver" ]; then
    echo "✓ No version change (the changelog guard covers whether one was required)."
    return 0
  fi

  base_major=$(_base_major); head_major=$(_head_major)
  base_minor=$(_base_minor); head_minor=$(_head_minor)

  # A MAJOR must say what breaks, in the entry itself — the counterpart to the
  # retro requirement on MINOR. Enforcement that the call was considered, not a
  # human approval gate.
  if [ "$head_major" -gt "$base_major" ]; then
    if awk '/^## /{n++} n==1' "$CHANGELOG" | grep -qiE '^### (Breaking|Removed)'; then
      echo "✓ MAJOR bump ($base_major → $head_major) documents what breaks."
      return 0
    fi
    echo "::error::This PR bumps MAJOR ($base_major → $head_major). The top $CHANGELOG entry must carry a '### Breaking' (or '### Removed') section saying what breaks."
    return 1
  fi

  if [ "$head_minor" -gt "$base_minor" ]; then
    echo "✓ MINOR bump — the retro guard covers this one."
    return 0
  fi

  [ -n "$CAPABILITY_ADD_PATTERN" ] || { echo "✓ PATCH bump (no capability signals configured)."; return 0; }
  if has_label patch-intentional; then
    echo "✓ 'patch-intentional' label present — skipping the milestone challenge."
    return 0
  fi
  signals=$(git diff --name-status "$MERGE_BASE" "$HEAD_SHA" | awk '$1=="A" {print $2}' \
    | grep -E "$CAPABILITY_ADD_PATTERN" || true)
  if [ -n "$signals" ]; then
    echo "::error::This PR bumps only PATCH ($base_ver → $head_ver) but adds capability-shaped surfaces. Bump MINOR (and add a retro), or apply 'patch-intentional' if these genuinely aren't new user-visible capability."
    echo "Added:"; echo "$signals"
    return 1
  fi
  echo "✓ PATCH bump with no new capability surface."
}

# A milestone is a MINOR (or MAJOR) bump. It must ship a retro. PATCH is untaxed.
cmd_retro() {
  if exempt retro-exempt '[skip retro]'; then return 0; fi
  local base_minor head_minor base_major head_major retro level
  base_minor=$(_base_minor); head_minor=$(_head_minor)
  base_major=$(_base_major); head_major=$(_head_major)
  echo "VERSION fields: base=$base_major.$base_minor head=$head_major.$head_minor"
  if [ -z "$head_minor" ] || [ -z "$head_major" ]; then
    echo "::error::Could not read MAJOR/MINOR from $VERSION_FILE at HEAD."; return 1
  fi
  # Same as bump-level: no base version means this PR introduces the file, so
  # there is no bump to call a milestone.
  if [ -z "$base_major" ] || [ -z "$base_minor" ]; then
    echo "✓ $VERSION_FILE is new in this PR — not a milestone bump."
    return 0
  fi

  # MAJOR first: a MAJOR bump resets MINOR to 0, so comparing MINOR alone reads
  # 5 → 0 as "went backwards, not a milestone" and lets the biggest milestone
  # there is ship with no retro.
  if [ "$head_major" -gt "$base_major" ]; then
    level=MAJOR
  elif [ "$head_minor" -gt "$base_minor" ]; then
    level=MINOR
  else
    echo "✓ Not a milestone (no MAJOR or MINOR increase) — no retro required."
    return 0
  fi

  retro=$(echo "$CHANGED" | grep -E "^$RETRO_DIR/.*\.md$" | grep -v -iE '/(README|TEMPLATE)\.md$' || true)
  if [ -z "$retro" ]; then
    echo "::error::This is a $level bump — a milestone — and must ship a retro under $RETRO_DIR/."
    echo "Or apply 'retro-exempt' if it genuinely isn't a milestone."
    return 1
  fi
  echo "✓ $level bump ships a retro:"; echo "$retro"
}

# Patch releases accumulate. Past the cap the next release must be a milestone,
# so a line cannot run forever without anyone asking what it added up to.
cmd_retro_debt() {
  local majmin count
  majmin=$(_head_version | cut -d. -f1,2)
  count=$(grep -cE "^## ${majmin//./\\.}\." "$CHANGELOG" || true)
  echo "Releases in the $majmin line (head): $count (cap $RETRO_DEBT_MAX)"
  if [ "$count" -gt "$RETRO_DEBT_MAX" ]; then
    if exempt retro-exempt '[skip retro]'; then return 0; fi
    echo "::error::Retro debt: the $majmin line already has $count releases. The next release must bump MINOR and ship a retro — or carry 'retro-exempt' if a milestone genuinely isn't due."
    return 1
  fi
  echo "✓ Under the retro-debt cap — a milestone is not yet forced."
}

# A retro whose follow-ups are never actioned is an apology with a table in it.
#
# The `retro` guard forces you to WRITE one. This makes its output binding: every
# row in a '## Gaps found' table must end up landed, blocked with a NAMED reason,
# or tracked as a numbered issue. A bare "deferred" fails.
#
# The rule is deliberately NOT "no debt". "Fix everything before shipping
# anything" cannot be satisfied — some gaps wait on a setting someone has to
# click, or hardware that has to exist. A gate nobody can pass gets bypassed, and
# a bypassed gate means nothing. So: no UNNAMED debt.
cmd_followups() {
  if exempt followups-exempt '[skip followups]'; then return 0; fi
  [ -d "$RETRO_DIR" ] || { echo "✓ no retros yet — nothing to enforce."; return 0; }

  local bad=0 tracked file line status
  local -a issues=()
  for file in "$RETRO_DIR"/*.md; do
    [ -f "$file" ] || continue
    case "$(basename "$file")" in README.md|TEMPLATE.md) continue ;; esac
    # Rows in the Gaps table: the LAST cell is the status.
    while IFS= read -r line; do
      status=$(printf '%s' "$line" | awk -F'|' '{print $(NF-1)}' | sed 's/^ *//; s/ *$//')
      [ -n "$status" ] || continue
      case "$status" in
        ---*|Status|status) continue ;;
      esac
      if printf '%s' "$status" | grep -qiE '^landed'; then
        continue
      elif printf '%s' "$status" | grep -qiE '^blocked:[[:space:]]*[^[:space:]]'; then
        continue
      elif printf '%s' "$status" | grep -qoE '#[0-9]+'; then
        issues+=("$(printf '%s' "$status" | grep -oE '#[0-9]+')")
        continue
      else
        echo "::error::$(basename "$file"): follow-up has no owner — '$status'"
        echo "    Must be 'landed …', 'blocked: <reason>', or 'tracked: #N'."
        bad=1
      fi
    done < <(awk '/^## Gaps found/{f=1; next} /^## /{f=0} f && /^\|/' "$file")
  done

  if [ "$bad" -ne 0 ]; then
    echo
    echo "  A bare 'deferred' is the thing being prevented. 'blocked' needs a"
    echo "  reason and 'tracked' needs a number, because a promise with nothing"
    echo "  behind it is indistinguishable from forgetting."
    return 1
  fi

  # Count DISTINCT issue numbers, not rows: the same gap noted by two retros is
  # one piece of work, and charging it twice would punish thoroughness.
  tracked=$(printf '%s\n' "${issues[@]+"${issues[@]}"}" | sort -u | grep -c '#' || true)
  if [ "$tracked" -gt "$RETRO_DEBT_MAX" ]; then
    echo "::error::$tracked open follow-up issues (cap $RETRO_DEBT_MAX). Filing an issue is not the same as doing the work — close some before opening more."
    return 1
  fi
  echo "OK — every follow-up is landed, blocked or tracked ($tracked open issue(s), cap $RETRO_DEBT_MAX)."
}

# The PR title is what a squash merge writes into main's history.
#
# On a SINGLE-commit PR GitHub squashes using the COMMIT SUBJECT, not the title,
# so both are checked. A PR that landed titled correctly and committed carelessly
# still put the wrong line in the log — and it is still there.
cmd_pr_title() {
  local title ver subject n
  title="${PR_TITLE:-}"
  [ -n "$title" ] || { echo "✓ no PR_TITLE in the environment — skipping."; return 0; }
  ver=$(_head_version)
  if ! printf '%s' "$title" | grep -qE "^v${ver//./\\.} ($PR_TITLE_TYPES)(\(.+\))?: .+"; then
    echo "::error::PR title must be 'v$ver <type>: <summary>' (types: $PR_TITLE_TYPES)."
    echo "  got: $title"
    return 1
  fi
  n=$(git rev-list --count "${MERGE_BASE}..${HEAD_SHA}")
  if [ "$n" = "1" ]; then
    subject=$(git log -1 --format=%s "$HEAD_SHA")
    if ! printf '%s' "$subject" | grep -qE "^v${ver//./\\.} "; then
      echo "::error::Single-commit PR: GitHub squashes with the COMMIT SUBJECT, not the title. Put 'v$ver' in the commit subject too, or pass --subject when merging."
      echo "  commit subject: $subject"
      return 1
    fi
  fi
  echo "✓ PR title (and squash subject) carry v$ver and a conventional type."
}

# ---------------------------------------------------------------------------
# Project-specific guards. Each file defines cmd_<name> and appends to EXTRA.
# ---------------------------------------------------------------------------
EXTRA=()
if [ -d "$HERE/guards.d" ]; then
  for f in "$HERE/guards.d"/*.sh; do
    [ -f "$f" ] || continue
    # shellcheck source=/dev/null
    . "$f"
  done
fi

CORE=(scope docs tests changelog changelog-archive version-sync bump-level retro retro-debt followups pr-title)

run_one() {
  local name="$1" fn
  fn="cmd_$(printf '%s' "$name" | tr '-' '_')"
  if ! declare -F "$fn" >/dev/null; then
    echo "::error::unknown guard: $name"; return 1
  fi
  "$fn"
}

case "${1:-all}" in
  list)
    printf 'core:    %s\n' "${CORE[*]}"
    printf 'project: %s\n' "${EXTRA[*]-<none>}"
    ;;
  all)
    rc=0
    for g in "${CORE[@]}" ${EXTRA[@]+"${EXTRA[@]}"}; do
      echo "--- $g ---"
      run_one "$g" || rc=1
    done
    exit $rc
    ;;
  *) run_one "$1" ;;
esac
