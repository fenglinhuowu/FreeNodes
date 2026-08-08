#!/usr/bin/env python
"""FreeNodeSpider CLI — AI-powered proxy node crawler.

Usage:
    python main.py                     # crawl all sites
    python main.py clashmeta           # crawl single site
    python main.py --check             # crawl all, then mihomo url-test & print alive
    python main.py --check-only        # skip crawl; test existing nodes/
    python main.py --check-only --mode tcp
    python main.py --help
"""
import argparse
import asyncio
import sys

sys.path.insert(0, ".")

# Windows consoles often use GBK; prefer UTF-8 for node labels / URIs
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="FreeNodeSpider — crawl free nodes, optionally test and print alive ones",
    )
    parser.add_argument(
        "target",
        nargs="?",
        default=None,
        help="Site name to process (default: all sites in config)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="After crawl, probe nodes and print working ones to console",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Skip crawl; only test existing files under nodes/",
    )
    parser.add_argument(
        "--source",
        default=None,
        help="With --check-only: specific file under nodes/ (e.g. merged.txt)",
    )
    parser.add_argument(
        "--mode",
        choices=["proxy", "tcp"],
        default=None,
        help="Check mode: proxy=mihomo url-test (accurate), tcp=port probe (fast/noisy)",
    )
    parser.add_argument(
        "--subs-only",
        action="store_true",
        help="Skip blog crawlers; only fetch direct subscription URLs",
    )
    parser.add_argument(
        "--sites-only",
        action="store_true",
        help="Skip direct subscriptions; only crawl blog sites",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Probe timeout in seconds",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="Max concurrent probes",
    )
    return parser.parse_args()


def _load_check_settings(args):
    """Build tester CheckConfig from config.yaml + CLI overrides."""
    from src.node_tester import CheckConfig, DEFAULT_MIHOMO

    timeout = 5.0
    concurrency = 20
    save_alive = True
    alive_file = "alive.txt"
    mode = "proxy"
    mihomo_path = DEFAULT_MIHOMO
    test_url = "http://www.gstatic.com/generate_204"
    batch_size = 120
    out_dir = "nodes"
    config = None

    try:
        from src.config import load_config
        config = load_config()
        c = config.check
        timeout = c.timeout
        concurrency = c.concurrency
        save_alive = c.save_alive
        alive_file = c.alive_file
        mode = c.mode
        mihomo_path = c.mihomo_path
        test_url = c.test_url
        batch_size = c.batch_size
        out_dir = config.output.get("dir", "nodes")
    except Exception:
        pass

    if args.timeout is not None:
        timeout = args.timeout
    if args.concurrency is not None:
        concurrency = args.concurrency
    if getattr(args, "mode", None):
        mode = args.mode
    if getattr(args, "mihomo", None):
        mihomo_path = args.mihomo

    return config, out_dir, CheckConfig(
        timeout=timeout,
        concurrency=concurrency,
        save_alive=save_alive,
        alive_file=alive_file,
        mode=mode,
        mihomo_path=mihomo_path,
        test_url=test_url,
        batch_size=batch_size,
    )


async def main():
    args = parse_args()

    if args.check_only:
        _, out_dir, check_cfg = _load_check_settings(args)
        from src.node_tester import run_check
        await run_check(
            out_dir,
            check=check_cfg,
            site=args.target,
            source_file=args.source,
            prefer_merged=not args.target and not args.source,
        )
        sys.exit(0)

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    from src.config import load_config, save_config
    from src.node_tester import run_check
    from src.scheduler import Scheduler

    config, out_dir, check_cfg = _load_check_settings(args)
    if config is None:
        print("ERROR: cannot load config.yaml (is PyYAML installed?)", file=sys.stderr)
        sys.exit(1)

    scheduler = Scheduler(config)
    await scheduler.run(
        target=args.target,
        skip_sites=args.subs_only,
        skip_subscriptions=args.sites_only,
    )
    save_config(config)

    if args.check:
        await run_check(
            out_dir,
            check=check_cfg,
            site=args.target,
            prefer_merged=not args.target,
        )

    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
