"""Allow running as `python3 -m syntra ...` directly from the repo.

No pip install required. From the repo root:

    python3 -m syntra verify
    python3 -m syntra route planner
    python3 -m syntra run "fix this bug"
"""
from syntra.cli.main import main
import sys

if __name__ == "__main__":
    sys.exit(main())
