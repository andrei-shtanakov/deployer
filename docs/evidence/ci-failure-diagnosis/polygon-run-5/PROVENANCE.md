# polygon/run-5 — controlled Dockerfile syntax error (2026-09-22)

Owner-authorised single GH run (no new authoring, no benchmark). Subject: the artifact
authored live on 2026-09-22 for the polygon project (`deployer author`, anthropic,
`{"ci": {"trigger_mode": "manual"}}`), with exactly ONE injected change — see
`run-5.injection.diff` (`FROM python:3.12-slim` → `FROM python:3.12-slim extra`).

- branch `polygon/run-5` (orphan), commit `937d465db4fd112ed443d325fcc8019eaacb6cdc`
- SHA checked against open PR heads before dispatch (#72 = 5e3a119, #64 = 683917b): no match
- run https://github.com/andrei-shtanakov/deployer/actions/runs/35706782471 — conclusion failure
- buildkit: `ERROR: failed to build: failed to solve: dockerfile parse error on line 1: FROM
  requires either one or three arguments` (`run-5.log-failed.txt`)
- `deployer diagnose <run-url>` on branch head 5e3a119: CLASSIFIED / authoring, exit 0
  (`run-5.stdout.txt`, `run-5.verdict.json`); anonymised snapshot `run-5.fixture.authoring.json`
  (repo → example/project, checkout paths rewritten; ids, SHAs, log text kept).
