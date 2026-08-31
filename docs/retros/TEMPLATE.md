# Retro — <milestone / version> (<date>)

**Scope:** what shipped (PRs, version range).

## What went well

- …

## What was hard to understand

- … (name the files/systems; say _why_ it was hard — missing map, split logic,
  implicit invariant, a stale note that misled)

## Gaps found

Every row's Status must be one of exactly two things, and `ci-guards.sh
followups` fails the build on anything else:

- `landed …` — done. In this PR, or naming the version (`landed v2.8.1`).
- `blocked: <reason>` — genuinely CANNOT be done here, and the reason says why.

A bare `deferred` fails, and so does `blocked` with no reason. `tracked: #N`
used to pass and no longer does — filing an issue is not doing the work. If a
gap can be fixed, the milestone that found it fixes it.

| Gap | Kind (docs / testing / process / observability) | Follow-up  | Status                          |
| --- | ----------------------------------------------- | ---------- | ------------------------------- |
| …   | …                                               | doc / test | landed here / blocked: <reason> |

## Follow-ups landed in this milestone

- …

## Follow-ups blocked (and why)

- … (prose, for humans — the table above is what CI reads)
