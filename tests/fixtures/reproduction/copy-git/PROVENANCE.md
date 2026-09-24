# Provenance: `copy-git` (negative case, spec §8.A)

Base: `run-1` — see `../run-1/PROVENANCE.md` for how every unchanged file was obtained. Derived on 2026-09-24 by a throwaway script (not committed).

Changes from the base (every input file that differs):

- `tree/Dockerfile`: `COPY .git/HEAD /head` appended as line 21 (after `CMD`, so line 11 still fails first); `tree-listing.json` entry sha recomputed. No `.dockerignore`.
- `local.*` unchanged: the recorded build fails at `STEP 7/12` (line 11) before the new line; a real rebuild would print `/13` totals, which the replay does not read.
- `local.stderr`: host paths anonymised to `/Users/example`.

- 2026-09-24: expected unmet text follows the narrowed spec §1.3 (d) — `.git exclusion not proven: …` (the ignore file is no longer consulted).
