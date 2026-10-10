#!/usr/bin/env python3
"""
fetch_hotspots —— 从最新一次容器抓取里捞热点，输出 JSON。

用法:
  python3 scripts/fetch_hotspots.py [--date 2026-06-30] [--limit 15] [--platforms toutiao,weibo]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hotspot_lib import latest_db, load_hotspots  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="fetch latest hotspots as JSON")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD, defaults to latest db")
    ap.add_argument("--limit", type=int, default=15, help="max items per platform")
    ap.add_argument("--platforms", default="", help="comma-separated platform_ids to keep")
    ap.add_argument("--pretty", action="store_true")
    args = ap.parse_args()

    db = latest_db(args.date)
    items = load_hotspots(db, limit_per_platform=args.limit)

    if args.platforms:
        wanted = {p.strip() for p in args.platforms.split(",") if p.strip()}
        items = [i for i in items if i["platform_id"] in wanted]

    payload = {
        "source_db": str(db),
        "count": len(items),
        "items": items,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
