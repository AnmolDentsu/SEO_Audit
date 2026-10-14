from __future__ import annotations

import argparse

from app.dedicated_worker import (
    run_worker,
)


def main() -> None:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--once",
        action="store_true",
    )

    parser.add_argument(
        "--run-id",
        type=int,
    )

    args = parser.parse_args()

    run_worker(
        "page",
        once=args.once,
        requested_run_id=args.run_id,
    )


if __name__ == "__main__":
    main()