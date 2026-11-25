"""`sellorita-migrate`: run Alembic with the config that ships inside this package.

    sellorita-migrate                    # = alembic upgrade head (the K8s migration Job)
    sellorita-migrate current            # which revision is the database at?
    sellorita-migrate revision --autogenerate -m "add x"   # new migration (dev only)

Works from any directory and inside any image that installed sellorita-common, so the
migration Job does not need the repo checked out.
"""

from __future__ import annotations

import sys
from pathlib import Path

from alembic.config import main as alembic_main

ALEMBIC_INI = Path(__file__).with_name("alembic.ini")


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    alembic_main(argv=["-c", str(ALEMBIC_INI), *(args or ["upgrade", "head"])])


if __name__ == "__main__":
    main()
