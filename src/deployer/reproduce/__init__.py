"""CI-failure reproduction (spec 2026-09-22 rev 5): findings, not causes.

Restores a failed run's tree at ``head_sha``, runs a closed list of
deterministic checks, rebuilds the failed build step on a confirmed-local
endpoint and compares it with CI. Nothing here carries a ``FailureKind``.
"""
