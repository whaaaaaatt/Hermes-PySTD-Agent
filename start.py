"""Convenience entry point: ``python start.py`` boots the web server
on a default port. Mirrors the original project's ``start.sh``.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser(description="Start HermesLite web server.")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=9119)
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--config", help="Config file path")
    p.add_argument("--insecure", action="store_true")
    args = p.parse_args()

    sys.path.insert(0, str(Path(__file__).parent))
    from hermeslite.config import load_config
    from hermeslite.web import start_server

    cfg = load_config(Path(args.config) if args.config else None)
    return start_server(
        cfg=cfg, host=args.host, port=args.port,
        open_browser=not args.no_browser, allow_public=args.insecure,
    )


if __name__ == "__main__":
    raise SystemExit(main())
