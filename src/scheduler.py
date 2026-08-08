"""Scheduler — parallel site dispatching with shared resource management."""
import asyncio
import logging
from datetime import date

from src.config import Config, SiteConfig
from src.llm_router import LLMRouter
from src.site_processor import SiteProcessor, SiteResult
from src.merger import Merger
from src.readme_updater import write_readme
from src.subscription_fetcher import fetch_all_subscriptions

logger = logging.getLogger(__name__)


class Scheduler:
    """Dispatch subscriptions + blog sites, then merge outputs."""

    def __init__(self, config: Config):
        self.config = config
        self.llm = LLMRouter(config)

    async def run(
        self,
        target: str | None = None,
        *,
        skip_sites: bool = False,
        skip_subscriptions: bool = False,
    ) -> list[SiteResult]:
        """Run subscriptions and/or blog sites.

        Args:
            target: Optional site/subscription name filter.
            skip_sites: Only fetch direct subscriptions.
            skip_subscriptions: Only crawl blog sites.
        """
        out_dir = self.config.output.get("dir", "nodes")
        final: list[SiteResult] = []

        # 1) Direct subscription feeds (no browser / no LLM)
        if not skip_subscriptions:
            subs = self.config.subscriptions
            if target:
                subs = [s for s in subs if s.name == target]
            if subs:
                results = await fetch_all_subscriptions(
                    subs,
                    out_dir=out_dir,
                    concurrency=max(4, self.config.crawl.concurrency * 2),
                    timeout=float(self.config.crawl.timeout),
                )
                today = date.today().isoformat()
                for r in results:
                    sr = SiteResult(site_name=f"sub:{r.name}")
                    if r.ok:
                        sr.txt_count = 1 if r.name else 0
                        sr.total_bytes = r.bytes
                        sr.articles_processed = 1
                        # Reflect into SiteConfig if same name exists
                        for site in self.config.sites:
                            if site.name == r.name:
                                site.up_date = today
                                site.node_count = r.lines
                                break
                    else:
                        sr.errors.append(r.error or "fetch failed")
                    final.append(sr)
            elif target and skip_sites:
                logger.warning(f"Unknown subscription '{target}'")

        # 2) Blog crawlers
        if not skip_sites:
            sites = self._resolve_sites(target)
            # If target matched a subscription only, don't also require sites
            if target and not sites and any(s.name == target for s in self.config.subscriptions):
                sites = []
            if sites:
                semaphore = asyncio.Semaphore(self.config.crawl.concurrency)

                async def _run_one(site: SiteConfig) -> SiteResult:
                    async with semaphore:
                        processor = SiteProcessor(site, self.config, self.llm)
                        return await processor.run()

                tasks = [_run_one(s) for s in sites]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                for site, result in zip(sites, results):
                    if isinstance(result, Exception):
                        err = SiteResult(
                            site_name=site.name,
                            errors=[f"unhandled exception: {result}"],
                        )
                        final.append(err)
                        logger.error(f"Site {site.name} crashed: {result}")
                    else:
                        final.append(result)
            elif target and not any(s.name == target for s in self.config.subscriptions):
                logger.error("No sites to process")

        self._print_summary(final)

        # 3) Merge + README on full runs
        if not target:
            merger = Merger(nodes_dir=out_dir)
            merge_result = merger.run()
            print(f"\n  merge: {merge_result.total_nodes} total nodes across "
                  f"{merge_result.txt_sources} txt + {merge_result.yaml_sources} yaml sources")
            print(f"  files: {merge_result.merged_txt or '(skip)'}, "
                  f"{merge_result.merged_yaml or '(skip)'}, "
                  f"{merge_result.provider_yaml or '(skip)'}")
            write_readme(self.config)

        return final

    def _resolve_sites(self, target: str | None) -> list[SiteConfig]:
        """Resolve target string to a list of SiteConfig."""
        if target:
            matches = [s for s in self.config.sites if s.name == target]
            if not matches:
                logger.warning(f"Unknown blog target '{target}', ignoring")
            return matches
        return self.config.sites

    @staticmethod
    def _print_summary(results: list[SiteResult]):
        """Print a summary table of all site results."""
        print(f"\n{'='*70}")
        print(f"{'SUMMARY':^70}")
        print(f"{'='*70}")
        print(f"{'SITE':16s} {'ARTICLES':10s} {'TXT':6s} {'YAML':6s} {'BYTES':12s} {'PATTERN':12s}")
        print("-" * 70)
        total = SiteResult(site_name="TOTAL")
        for r in results:
            lp = r.link_pattern
            pattern = "✓ self-healed" if r.pattern_saved else (lp[:20] if lp else "—")
            print(f"{r.site_name:16s} {r.articles_processed:4d}        {r.txt_count:4d}   {r.yaml_count:4d}  {r.total_bytes:8d}B  {pattern:12s}")
            total.articles_processed += r.articles_processed
            total.txt_count += r.txt_count
            total.yaml_count += r.yaml_count
            total.total_bytes += r.total_bytes
        print("-" * 70)
        print(f"{total.site_name:16s} {total.articles_processed:4d}        {total.txt_count:4d}   {total.yaml_count:4d}  {total.total_bytes:8d}B")
        print(f"{'='*70}")

        errors = [r for r in results if r.errors]
        if errors:
            print(f"\n⚠ {len(errors)} source(s) had errors:")
            for r in errors:
                for e in r.errors:
                    print(f"  [{r.site_name}] {e}")
