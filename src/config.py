"""Configuration loading, persistence, and self-healing link_pattern storage."""
import yaml
from dataclasses import dataclass, field

from src.node_tester import CheckConfig


@dataclass
class SubscriptionConfig:
    name: str
    url: str
    kind: str = "auto"  # auto | txt | yaml
    mirrors: list[str] = field(default_factory=list)
    enabled: bool = True


@dataclass
class SiteConfig:
    name: str
    start_url: str
    type: str = "simple"                    # simple | yt_pwd | cloud_drive
    description: str = ""
    link_pattern: str | None = None
    failed_count: int = 0
    up_date: str = ""                           # last crawl date, YYYY-MM-DD
    node_count: int = 0                         # proxies found in last crawl
    exclude_patterns: list[str] | None = None   # href substrings to skip in article listing
    pwd_hint: str | None = None                 # password hint for yt_pwd sites
    yt_hint: str | None = None                  # YouTube hint for yt_pwd sites


@dataclass
class ProviderConfig:
    name: str
    base_url: str
    api_key_env: str
    models: list[str]
    is_reasoning_model: bool = False
    default_weight: int = 10


@dataclass
class LLMConfig:
    providers: list[ProviderConfig] = field(default_factory=list)
    task_routing: dict[str, dict[str, int]] = field(default_factory=dict)


@dataclass
class CrawlConfig:
    max_articles: int = 3
    timeout: int = 30
    concurrency: int = 4                        # max sites in parallel
    article_concurrency: int = 3                # max articles per site in parallel
    download_concurrency: int = 8               # max file downloads in parallel
    proxy: str = ""                             # HTTP proxy for YouTube access


@dataclass
class Config:
    sites: list[SiteConfig]
    crawl: CrawlConfig
    output: dict
    llm: LLMConfig
    check: CheckConfig = field(default_factory=CheckConfig)
    subscriptions: list[SubscriptionConfig] = field(default_factory=list)


def load_config(path: str = "config.yaml") -> Config:
    """Load config from YAML file. Missing llm section yields empty LLMConfig."""
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    sites = [SiteConfig(**s) for s in raw["sites"]]
    crawl = CrawlConfig(**(raw.get("crawl") or {}))
    output = raw.get("output", {})

    llm_raw = raw.get("llm", {})
    providers = [ProviderConfig(**p) for p in llm_raw.get("providers", [])]
    llm = LLMConfig(providers=providers, task_routing=llm_raw.get("task_routing", {}))

    check_raw = raw.get("check") or {}
    check = CheckConfig(**{
        k: v for k, v in check_raw.items() if k in CheckConfig.__dataclass_fields__
    })

    subs = []
    for s in raw.get("subscriptions") or []:
        fields = {k: v for k, v in s.items() if k in SubscriptionConfig.__dataclass_fields__}
        subs.append(SubscriptionConfig(**fields))

    return Config(
        sites=sites,
        crawl=crawl,
        output=output,
        llm=llm,
        check=check,
        subscriptions=subs,
    )


def save_config(config: Config, path: str = "config.yaml"):
    """Persist config, preserving link_pattern and llm section."""
    raw_sites = []
    for s in config.sites:
        entry = {
            "name": s.name,
            "start_url": s.start_url,
            "type": s.type,
            "description": s.description,
        }
        if s.link_pattern:
            entry["link_pattern"] = s.link_pattern
        if s.up_date:
            entry["up_date"] = s.up_date
        if s.node_count:
            entry["node_count"] = s.node_count
        if s.exclude_patterns:
            entry["exclude_patterns"] = s.exclude_patterns
        if s.pwd_hint:
            entry["pwd_hint"] = s.pwd_hint
        if s.yt_hint:
            entry["yt_hint"] = s.yt_hint
        if s.failed_count:
            entry["failed_count"] = s.failed_count
        raw_sites.append(entry)

    raw_llm = {
        "providers": [
            {
                "name": p.name,
                "base_url": p.base_url,
                "api_key_env": p.api_key_env,
                "models": p.models,
                "is_reasoning_model": p.is_reasoning_model,
                "default_weight": p.default_weight,
            }
            for p in config.llm.providers
        ],
        "task_routing": config.llm.task_routing,
    }

    crawl_raw = {
        "max_articles": config.crawl.max_articles,
        "timeout": config.crawl.timeout,
        "concurrency": config.crawl.concurrency,
        "article_concurrency": config.crawl.article_concurrency,
        "download_concurrency": config.crawl.download_concurrency,
    }
    if config.crawl.proxy:
        crawl_raw["proxy"] = config.crawl.proxy

    check_raw = {
        "enabled": config.check.enabled,
        "timeout": config.check.timeout,
        "concurrency": config.check.concurrency,
        "save_alive": config.check.save_alive,
        "alive_file": config.check.alive_file,
        "mode": config.check.mode,
        "mihomo_path": config.check.mihomo_path,
        "test_url": config.check.test_url,
        "batch_size": config.check.batch_size,
        "max_sparkle": config.check.max_sparkle,
        "parallel_batches": config.check.parallel_batches,
        "speed_enabled": config.check.speed_enabled,
        "min_mbps": config.check.min_mbps,
        "speed_bytes": config.check.speed_bytes,
        "speed_timeout": config.check.speed_timeout,
        "speed_concurrency": config.check.speed_concurrency,
        "speed_url": config.check.speed_url,
        "compat_urls": list(config.check.compat_urls or []),
    }

    raw = {
        "crawl": crawl_raw,
        "output": config.output,
        "check": check_raw,
        "sites": raw_sites,
        "subscriptions": [
            {
                "name": s.name,
                "url": s.url,
                "kind": s.kind,
                "enabled": s.enabled,
                **({"mirrors": s.mirrors} if s.mirrors else {}),
            }
            for s in config.subscriptions
        ],
        "llm": raw_llm,
    }

    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f, allow_unicode=True, default_flow_style=False)
