"""Fail when active tax content lacks agent-verification evidence."""

import sys
from pathlib import Path

from app.domain.content_verification import verify_content


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = Path(args[0]) if args else Path.cwd()
    errors = verify_content(root)
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
