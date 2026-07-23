# Tildagon tarball inventory

Snapshot of what ships to the badge today, ranked by weight, with notes on
what can go. Feeds Phase 2 of the boot / install speed-up work.

## Two install paths

There are two ways this app lands on a badge, and they package differently:

**`deploy.sh` (local dev)** — copies only `app.py`, `metadata.json`,
`uQR.py`, `nocturnation/`, `shows/` into `/apps/nocturnation/`. Strips
`__pycache__` before copy. Roughly what the app should look like at
runtime.

**EMF app store (production install)** — the store crawler pulls the
GitHub tarball for repos tagged `tildagon-app` and installs the **entire
repo tree** to `/apps/nocturnation_tildagon/`. This is where the bloat
lives: tests, README, CHANGELOG, docs all ship even though they're inert
on the badge. Whether the store honours any ignore mechanism is
unconfirmed — needs a check against
`https://tildagon.badge.emfcamp.org/tildagon-apps/publish/`.

## Top-level inventory (git-tracked bytes)

| Entry | Files | Size | Ships via deploy.sh? | Ships via store? | Verdict |
|---|--:|--:|:-:|:-:|---|
| `nocturnation/` | 45 | 259 KB | ✅ | ✅ | Keep. Runtime code. |
| `tests/` | 31 | 247 KB | ❌ | ✅ | **Drop from store install.** Host pytest, useless on badge. |
| `app.py` | 1 | 87 KB | ✅ | ✅ | Keep. Will slim in Phase 3. |
| `uQR.py` | 1 | 36 KB | ✅ | ✅ | **Lazy-import candidate.** Help screen only. |
| `CHANGELOG.md` | 1 | 29 KB | ❌ | ✅ | **Drop from store install.** Reader-facing doc. |
| `shows/` | 4 | 24 KB | ✅ | ✅ | Keep. Small; also lazy-import candidate for Director. |
| `docs/` | 1 | 22 KB | ❌ | ✅ | **Drop from store install.** `tildagon-history.md` etc. |
| `tools/` | 3 | 21 KB | ❌ | ✅ | **Drop from store install.** Bench scripts. |
| `README.md` | 1 | 14 KB | ❌ | ✅ | **Drop from store install.** |
| `RELEASING.md` + `SECURITY.md` + `PRIVACY.md` + `CONTRIBUTING.md` + `LICENSE` | 5 | 10 KB | ❌ | ✅ | LICENSE keep (compliance); rest drop from store install. |
| `deploy.sh` | 1 | 3 KB | ❌ | ✅ | Drop from store install. |
| `pyproject.toml` + `.gitattributes` + `.gitignore` | 3 | 2 KB | ❌ | ✅ | Drop from store install. |
| `tildagon.toml` | 1 | 2 KB | ❌ | ✅ | Store manifest — required. |
| `metadata.json` | 1 | 71 B | ✅ | ✅ | Runtime manifest — required. |

**Total tracked in git**: 748 KB across 99 files.
**Deploy.sh payload** (what the runtime actually uses): ~410 KB across 51 files.
**Store install today** (whole repo tarball): 748 KB across 99 files — ~340 KB of dead weight.

## Big files under `nocturnation/`

Top 10, for future slimming decisions:

| File | Bytes | Path in the load graph |
|---|--:|---|
| `protocol/frame.py` | 22 844 | Lume — always loaded |
| `repeater.py` | 19 041 | Lume — always loaded (FSM small at runtime, source has verbose docstrings) |
| `images/dirid_e1.jpg` | 17 427 | Director only — dead weight for Lume-only badges |
| `render/perimeter.py` | 17 233 | Lume — always loaded |
| `director/render_dispatch.py` | 16 826 | Director only |
| `images/dirid_e0.jpg` | 15 664 | Director only — dead weight for Lume-only badges |
| `render/lcd.py` | 14 843 | Lume + Director |
| `render/lume_text.py` | 13 174 | Lume overlay renderer |
| `director/controller.py` | 10 182 | Director only |
| `director/imu.py` | 10 066 | Director only |

Total Director-only weight (director/ + images/): ~90 KB. Lazy-importing
these gets it out of the boot path but still ships in the tarball.

## Recommendations for Phase 2 PR

**Cheap wins (install-size + boot parse cost):**

1. **`.tildagonignore` or repo restructure** — get `tests/`, `docs/`,
   `CHANGELOG.md`, `README.md`, `RELEASING.md`, `SECURITY.md`,
   `PRIVACY.md`, `CONTRIBUTING.md`, `deploy.sh`, `pyproject.toml`,
   `tools/`, `.gitattributes` out of the store install. Cuts install
   payload from 748 KB to ~410 KB (~45 %).

2. **Lazy-import `uQR`** — 36 KB parse cost off the Lume boot path.
   Only imported when the help screen is opened.

3. **Lazy-import `nocturnation.director.*`** — 90 KB (director source +
   images) off the Lume boot path. Only imported on Director-mode entry.

4. **Lazy-import `nocturnation.shows.*` and `shows/*`** — 30 KB off the
   Lume boot path. Only needed by Director.

**Deferred (needs measurement first):**

- `mpy-cross` build step — big potential win, needs store compatibility
  check.
- Slimming `protocol/frame.py` (22 KB, largest single Lume file) — need
  to see if it dominates the boot budget before touching it.

## What "install" cost we don't yet know

- Store install time is presumably dominated by network transfer +
  extract + write-to-flash. 748 → 410 KB should roughly halve it, but
  the constant factor matters. Worth timing an install before + after.
- Store crawler ignore semantics — do we need a repo restructure or is
  there an ignore file? Confirmed by reading the publish docs, not
  guessed.
