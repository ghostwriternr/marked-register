"""Development launcher for the local web application."""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from marked_register.web import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Marked Register locally")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    parser.add_argument(
        "--database",
        default=Path(".local/marked-register.sqlite"),
        type=Path,
    )
    args = parser.parse_args()

    database_path = args.database.expanduser().resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    app = create_app(database_url=f"sqlite+pysqlite:///{database_path}")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
