from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = Path(__file__).resolve().parents[1]
ROOT_ENV = PROJECT_ROOT / ".env"
LOCAL_ENV = BACKEND_ROOT / ".env"


class Settings(BaseSettings):
    app_env: str = "development"
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    frontend_origins: str = "http://localhost:5173"
    database_url: str = "sqlite:///./seo_platform.db"

    job_execution_mode: str = "worker"
    worker_poll_seconds: float = 2.0
    run_max_urls: int = 1000
    cache_ttl_hours: int = 12

    crawl_timeout_ms: int = 45000
    crawl_max_retries: int = 3
    crawl_backoff_base_seconds: float = 2.0
    crawl_concurrency: int = 3
    crawl_user_agent: str = "Mozilla/5.0 (compatible; SEOAnalysisBot/1.0)"
    block_private_networks: bool = True

    # AI_MODE:
    #   off     -> deterministic SEO only
    #   mock    -> deterministic fake AI response for local tests
    #   vertex  -> Vertex AI using GCP project + service account/ADC
    #   api_key -> Developer API using GEMINI_API_KEY (optional fallback)
    ai_mode: str = "off"
    vertex_auth_mode: str = "auto"  # auto | service_account | adc

    google_cloud_project: str | None = Field(
        default=None,
        validation_alias=AliasChoices("GOOGLE_CLOUD_PROJECT", "GCP_PROJECT_ID"),
    )
    google_cloud_location: str = Field(
        default="global",
        validation_alias=AliasChoices("GOOGLE_CLOUD_LOCATION", "GCP_LOCATION"),
    )
    google_application_credentials: str | None = Field(
        default=None,
        validation_alias="GOOGLE_APPLICATION_CREDENTIALS",
    )
    gemini_api_key: str | None = Field(default=None, validation_alias="GEMINI_API_KEY")

    # Backward-compatible aliases support the user's existing .env names.
    gemini_model: str = Field(
        default="gemini-3.8-flash",
        validation_alias=AliasChoices("GEMINI_MODEL", "MODEL_TEXT"),
    )
    embedding_model: str = Field(
        default="gemini-embedding-2",
        validation_alias=AliasChoices("EMBEDDING_MODEL", "MODEL_EMBEDDING"),
    )
    embedding_dimension: int = 768
    ai_max_opportunities: int = 20

    # Gemini 3.8 Flash thinking controls. Low = fast classification/labeling;
    # medium = deeper opportunity/section reasoning.
    ai_thinking_level_fast: str = Field(
        default="low",
        validation_alias="AI_THINKING_LEVEL_FAST",
    )
    ai_thinking_level_deep: str = Field(
        default="medium",
        validation_alias="AI_THINKING_LEVEL_DEEP",
    )

    # Version these whenever prompt semantics / structured schemas materially change.
    # They make run metadata and future durable caches auditable.
    ai_prompt_version: str = "seo-analysis-v4"
    ai_schema_version: str = "seo-analysis-v4"

    # Generative reasoning should be reserved for material/ambiguous findings.
    ai_review_priorities: str = "HIGH,MEDIUM"
    ai_review_ambiguous_similarity_low: float = 0.43
    ai_review_ambiguous_similarity_high: float = 0.82

    # ------------------------------------------------------------------
    # Sitemap Intelligence — generic / enterprise-safe settings
    # ------------------------------------------------------------------
    sitemap_max_urls_per_source: int = 500000

    # Safety ceiling only. Discovery otherwise continues breadth-first until
    # there are no more unseen sitemap/XML documents left to explore.
    sitemap_max_child_sitemaps: int = 5000
    sitemap_max_recursion_depth: int = 64
    sitemap_follow_xml_links_in_urlset: bool = True

    # Discovery is authoritative; resolution scope is applied later and is visible
    # in the run funnel. Recommended default keeps the root registrable domain and
    # its subdomains while preserving external sitemap URLs as discovery evidence.
    sitemap_resolution_scope: str = "registrable_domain"
    sitemap_exclude_obvious_non_pages: bool = True

    # Large runs must not write SQLite progress/cache state once per URL.
    sitemap_progress_update_every: int = 100
    sitemap_checkpoint_every: int = 100
    sitemap_db_batch_size: int = 1000

    sitemap_max_xml_bytes: int = 20 * 1024 * 1024
    sitemap_max_html_bytes: int = 8 * 1024 * 1024
    sitemap_request_timeout_seconds: float = 20.0
    sitemap_http_user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/152.0.0.0 Safari/537.36"
    )

    # Sitemap-fetch retry/backoff before browser fallback.
    sitemap_fetch_max_retries: int = 3
    sitemap_fetch_backoff_base_seconds: float = 1.0

    # A child sitemap that returns HTTP 2xx with a truly empty body is rechecked
    # a small, bounded number of times before being recorded as the auditable
    # EMPTY_CHILD_SITEMAP warning. Root sitemaps are never downgraded this way.
    sitemap_empty_child_verify_attempts: int = 2
    sitemap_empty_child_verify_delay_seconds: float = 0.5

    # Corporate-DNS-aware SSRF validation. No domain allowlist is required.
    # If system DNS returns private/proxy addresses for a hostname, the security
    # layer independently verifies the hostname through public DNS-over-HTTPS.
    # Literal private IPs, localhost, reserved names and unsafe redirects remain
    # blocked.
    sitemap_public_dns_fallback_enabled: bool = True
    sitemap_allow_corporate_dns_proxy: bool = True
    sitemap_public_dns_timeout_seconds: float = 4.0
    sitemap_public_dns_cache_ttl_seconds: int = 300

    # Per-page HTTP retry policy. Transient transport/5xx/429 failures are
    # retried here before browser fallback. Remaining recoverable failures are
    # persisted and handled by automatic background recovery after the report-
    # producing main analysis is complete. 404/410 stay stale, not endless retry.
    sitemap_page_max_retries: int = 2

    # The HTTP stage is asyncio based; increasing this is the equivalent of adding
    # more I/O workers without spawning multiple competing app.worker processes.
    # A per-host cap prevents one large competitor from monopolising all sockets or
    # tripping rate limits while still allowing many independent hosts in parallel.
    sitemap_resolve_concurrency: int = 48
    sitemap_http_per_host_concurrency: int = 8
    # Start below the hard per-host ceiling and ramp only after sustained success.
    # This prevents a 100k+ URL host from immediately receiving an 8-request burst.
    sitemap_http_initial_per_host_concurrency: int = 4
    sitemap_http_min_per_host_concurrency: int = 1
    sitemap_http_successes_to_ramp: int = 24
    sitemap_http_rate_limit_shrink_factor: float = 0.5
    sitemap_http_default_rate_limit_cooldown_seconds: float = 4.0
    sitemap_http_max_retry_after_seconds: float = 120.0

    # 401/403/406 can represent a host-level WAF/bot-control response rather than
    # 100k independent bad URLs. Once a strong rolling-window signal is observed,
    # stop launching more requests to that host and mark the remaining queue as
    # recoverable/deferred instead of hammering the site.
    sitemap_access_block_window_size: int = 12
    sitemap_access_block_min_samples: int = 8
    sitemap_access_block_ratio: float = 0.75
    sitemap_host_access_probe_per_host: int = 12
    # Round-robin chunk size used before the resolver. It preserves source fairness
    # even when one competitor contributes hundreds of thousands of URLs.
    sitemap_source_round_robin_batch_size: int = 200
    sitemap_max_redirects: int = 5

    # Lightweight TLS/browser-fingerprint fallback. This runs before Chromium and
    # is much cheaper for 401/403/406/429 or transport failures caused by WAF/TLS
    # fingerprinting. It is optional and requires curl-cffi.
    sitemap_impersonation_fallback_enabled: bool = True
    sitemap_impersonation_concurrency: int = 12
    # Browser-fingerprint HTTP is deliberately conservative per host. The global
    # pool can still serve many independent hosts concurrently.
    sitemap_impersonation_per_host_concurrency: int = 2
    sitemap_impersonation_initial_per_host_concurrency: int = 1
    sitemap_impersonation_successes_to_ramp: int = 100
    sitemap_impersonation_fallback_max_pages: int = 150000
    sitemap_impersonation_browser: str = "chrome"

    # Persisted-failure recovery is host-wise and batch-adaptive. A healthy host
    # grows from a tiny probe to bounded batches; a renewed access block stops only
    # that host while preserving every unresolved row for a later recovery pass.
    sitemap_recovery_hard_probe_batch_size: int = 8
    sitemap_recovery_hard_batch_size: int = 200
    sitemap_recovery_hard_max_batch_size: int = 500
    sitemap_recovery_hard_concurrency: int = 2
    sitemap_recovery_hard_batch_pause_seconds: float = 2.0
    sitemap_recovery_hard_min_success_ratio: float = 0.80
    sitemap_recovery_hard_block_ratio: float = 0.50

    # Recovery pacing is intentionally separate from concurrency. A host can reject
    # rapid sequential requests even when only one request is in flight, so health
    # probes are slow and bulk recovery ramps its request-start interval carefully.
    sitemap_recovery_probe_interval_seconds: float = 5.0
    sitemap_recovery_bulk_initial_interval_seconds: float = 1.0
    sitemap_recovery_bulk_min_interval_seconds: float = 0.50
    sitemap_recovery_bulk_per_host_concurrency: int = 2
    sitemap_recovery_rate_ramp_factor: float = 0.90
    sitemap_recovery_rate_backoff_factor: float = 2.0
    # Do not accelerate after a single good batch. A host must prove that the
    # current request-start interval is sustainable across several complete
    # healthy bulk batches before the controller experiments with a faster rate.
    sitemap_recovery_healthy_batches_before_ramp: int = 3

    # Safety is based on CONSECUTIVE pressure/no-progress events, not lifetime WAF
    # events. A 100k+ URL host that repeatedly makes meaningful healthy progress
    # can therefore continue unattended instead of stopping after eight total
    # circuit openings spread across many hours.
    sitemap_recovery_cooldown_initial_seconds: float = 60.0
    sitemap_recovery_cooldown_max_seconds: float = 600.0
    sitemap_recovery_cooldown_multiplier: float = 2.0
    sitemap_recovery_max_consecutive_pressure_events: int = 8
    # Backward-compatible setting retained for existing .env files; controller v2
    # no longer uses this as a lifetime stop condition.
    sitemap_recovery_max_reprobes_per_host: int = 8
    sitemap_recovery_max_cycles_per_host: int = 3
    sitemap_recovery_max_attempts_per_url: int = 5
    sitemap_recovery_heartbeat_seconds: float = 15.0

    # Recovery is scheduled automatically after the report-producing main run
    # reaches a terminal state. The worker revisits due recovery slices without
    # requiring the user to click the manual recovery endpoint.
    sitemap_background_recovery_enabled: bool = True
    sitemap_background_recovery_initial_delay_seconds: float = 5.0
    sitemap_background_recovery_retry_delay_seconds: float = 60.0

    # Chromium is now a last-resort recovery path, not a second crawler.
    # Failed/ambiguous URLs are status-aware and sampled by host + URL family.
    sitemap_playwright_concurrency: int = 6
    sitemap_playwright_per_host_concurrency: int = 2
    sitemap_browser_fallback_max_pages: int = 1200
    sitemap_browser_fallback_per_host_cap: int = 250
    sitemap_browser_family_sample_size: int = 3
    sitemap_browser_stale_sample_max: int = 25
    sitemap_browser_checkpoint_every: int = 10
    sitemap_verify_stale_with_browser: bool = False
    sitemap_browser_navigation_timeout_ms: int = 12000
    sitemap_browser_domcontentloaded_timeout_ms: int = 7000
    sitemap_browser_networkidle_timeout_ms: int = 1500
    sitemap_browser_fallback_threshold: float = 0.70

    # Backward-compatible knobs retained for existing .env files. Inline final
    # recovery is no longer used; automatic background recovery owns this work.
    sitemap_failed_recovery_rounds: int = 1
    sitemap_final_recovery_max_pages: int = 5000

    sitemap_text_excerpt_chars: int = 12000

    sitemap_ai_page_batch_size: int = 20
    sitemap_ai_max_page_classifications: int = 300
    sitemap_ai_max_opportunities: int = 60
    sitemap_ai_topic_cluster_batch_size: int = 20
    sitemap_ai_max_topic_clusters: int = 250

    sitemap_embedding_batch_size: int = 32
    # Hybrid topic clustering uses embeddings plus lexical/core-topic evidence.
    # 0.76 is intentionally more consolidating than the previous 0.80-only cosine gate,
    # while still requiring semantic corroboration in sitemap_topics.py.
    sitemap_semantic_topic_threshold: float = 0.76
    sitemap_semantic_direct_merge_threshold: float = 0.86
    sitemap_topic_min_embedding_for_hybrid_merge: float = 0.72
    # Semantic clustering is intentionally bounded. Exact normalized topic labels
    # continue to work beyond this limit; the cap prevents quadratic work on very
    # large inventories.
    sitemap_semantic_cluster_max_labels: int = 5000

    sitemap_inventory_page_size_max: int = 500
    sitemap_failure_page_size_max: int = 500
    sitemap_cache_ttl_hours: int = 72

    cloud_run_project: str | None = None
    cloud_run_region: str = "us-central1"
    cloud_run_job_name: str | None = None

    model_config = SettingsConfigDict(
        env_file=(str(ROOT_ENV), str(LOCAL_ENV)),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @property
    def cors_origins(self) -> list[str]:
        return [x.strip() for x in self.frontend_origins.split(",") if x.strip()]

    def resolve_credentials_path(self) -> Path | None:
        """Resolve GOOGLE_APPLICATION_CREDENTIALS robustly for local execution."""
        raw = (self.google_application_credentials or "").strip()
        if not raw:
            return None
        candidate = Path(raw).expanduser()
        if candidate.is_absolute():
            return candidate
        for base in (Path.cwd(), PROJECT_ROOT, BACKEND_ROOT):
            path = (base / candidate).resolve()
            if path.exists():
                return path
        return (Path.cwd() / candidate).resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()


