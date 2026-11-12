#!/usr/bin/env python3
"""Fetch Telegram Bot API sources and write data/index.json."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from pathlib import Path

from docsbot.builder import (
    API_JSON_URL,
    ERRORS_JSON_URL,
    build_index,
    default_index_path,
    save_index,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the local Telegram Bot API docs index")
    parser.add_argument("-o", "--out", type=Path, default=None, help="Output JSON path")
    parser.add_argument("--api-json", type=Path, help="Use a local api.json instead of fetching")
    parser.add_argument("--errors-json", type=Path, help="Use a local errors.json instead of fetching")
    parser.add_argument("--api-url", default=API_JSON_URL)
    parser.add_argument("--errors-url", default=ERRORS_JSON_URL)
    parser.add_argument(
        "--html-dir",
        type=Path,
        help="Directory containing bots.html, features.html, webapps.html, faq.html",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    html_paths = None
    if args.html_dir:
        html_paths = {}
        mapping = {
            "fact": "bots.html",
            "feature": "features.html",
            "webapp": "webapps.html",
            "faq": "faq.html",
        }
        for kind, name in mapping.items():
            p = args.html_dir / name
            if p.exists():
                html_paths[kind] = p

    index = build_index(
        api_json_url=args.api_url,
        errors_json_url=args.errors_url,
        api_path=args.api_json,
        errors_path=args.errors_json,
        html_paths=html_paths,
    )
    path = save_index(index, args.out or default_index_path())
    kinds = Counter(e["kind"] for e in index["entries"])
    print(f"Wrote {path} ({path.stat().st_size} bytes)")
    print(f"Built at: {index['built_at']}")
    print("Entries by kind:")
    for k, n in sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {k:10} {n}")
    print(f"  {'total':10} {index['entry_count']}")
    if index.get("warnings"):
        print("Warnings:", file=sys.stderr)
        for w in index["warnings"]:
            print(f"  - {w}", file=sys.stderr)
    # Compact source summary
    print("Sources:")
    print(json.dumps(index.get("sources") or {}, indent=2))
    return 1 if index.get("warnings") else 0


if __name__ == "__main__":
    raise SystemExit(main())
