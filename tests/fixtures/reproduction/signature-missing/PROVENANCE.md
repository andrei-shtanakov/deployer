# Provenance: `signature-missing` (negative case, spec §8.A)

Base: `run-3` — see `../run-3/PROVENANCE.md` for how every unchanged file was obtained. Derived on 2026-09-24 by a throwaway script (not committed).

Changes from the base (every input file that differs):

- `local.stdout`: every line after `STEP 9/13: RUN uv run --frozen python -m unittest discover -s tests` (the RUN's own output) removed; `local.stderr`'s `Error: building at STEP …` line kept; `local.exit` stays `1`.
