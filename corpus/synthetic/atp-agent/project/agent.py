"""Minimal ATP-compatible agent: one request in, one response out."""

import json
import sys


def main() -> int:
    """Read an ATPRequest on stdin, write an ATPResponse on stdout.

    The response shape is not free-form: ATP validates stdout against its
    `ATPResponse` model, which requires `task_id` (echoed from the request)
    and a `status` from its own enum. There is no `output` field.
    """
    raw = sys.stdin.read()
    if not raw.strip():
        print("empty request on stdin", file=sys.stderr)
        return 1
    request = json.loads(raw)
    json.dump(
        {
            "task_id": request["task_id"],
            "status": "completed",
            "artifacts": [],
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
