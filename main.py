#!/usr/bin/env python
"""FreeNodeSpider CLI — AI-powered proxy node crawler.

Usage:
    python main.py --subs-only         # fetch GitHub/CDN subs + filter (recommended for v2rayN)
    python main.py                     # blogs + subs, then filter
    python main.py --check-only        # filter existing merged/merged_raw
    python main.py --skip-check        # fetch/crawl without mihomo filter
    python main.py --help
"""
import argparse
import asyncio
import sys
from dotenv import load_dotenv

from src.config import load_config, save_config
from src.scheduler import Scheduler

load_dotenv()
sys.path.insert(0, ".")

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="FreeNodeSpider — crawl/fetch free nodes for v2rayN / Clash",
    )
    parser.add_argument(
        "target",
        nargs="?",
        default=None,
        help="Site or subscription name (default: all)",
    )
    parser.add_argument(
        "--subs-only",
        action="store_true",
        help="Only fetch direct subscription URLs (skip blog crawlers)",
    )
    parser.add_argument(
        "--sites-only",
        action="store_true",
        help="Only crawl blog sites (skip subscriptions)",
    )
    parser.add_argument(
        "--skip-check",
        action="store_true",
        help="Skip mihomo alive-filter after merge",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Only filter existing nodes/merged.txt (no fetch)",
    )
    return parser.parse_args()


async def main():
    args = parse_args()
    config = load_config()

    if args.skip_check:
        config.check.enabled = False

    if args.check_only:
        from pathlib import Path
        from src.alive_filter import filter_merged_alive
        out_dir = config.output.get("dir", "nodes")
        raw = Path(out_dir) / "merged_raw.txt"
        source = "merged_raw.txt" if raw.exists() else "merged.txt"
        print(f"check-only: filtering {out_dir}/{source}...")
        tested, alive, path = await filter_merged_alive(
            out_dir,
            check=config.check,
            source_txt=source,
            max_sparkle=config.check.max_sparkle,
        )
        print(f"done: {alive}/{tested} kept -> {path}")
        sys.exit(0 if alive else 1)

    scheduler = Scheduler(config)
    results = await scheduler.run(
        target=args.target,
        skip_sites=args.subs_only,
        skip_subscriptions=args.sites_only,
    )

    save_config(config)

    if not results:
        sys.exit(1)
    all_failed = all(r.errors and r.articles_processed == 0 for r in results)
    sys.exit(1 if all_failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
