"""Allow running via `python3 -m macmaster`."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
