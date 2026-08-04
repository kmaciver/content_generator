"""Application settings — the single configuration surface for every service.

Layering (lowest precedence first):

    code defaults  →  config/providers.yaml (provider sections only)  →  environment

There is deliberately no in-process ``.env`` handling: the repo-root ``.env`` is
consumed by *docker compose*, which materialises it as real environment for each
container. By the time Python starts, the environment IS the configuration.
Keeping one door means the layering above is the whole story.

Two aggregates exist, and the split is a security boundary, not taste (NF8,
SADD §21.3):

* :class:`AppSettings` — what every service may read. The backend uses only this.
* :class:`WorkerSettings` — adds provider selection and provider API keys.
  Only worker processes may instantiate it; compose only supplies the key
  variables to worker containers, and ``tests/test_secret_isolation.py`` guards
  both directions statically.

Naming maps 1:1 to the variables documented in ``docs/env-reference.md``:
flat sections use an env prefix (``POSTGRES_HOST`` → ``postgres.host``), the
provider tree uses the double-underscore delimiter
(``PROVIDERS__LLM__ADAPTER`` → ``providers.llm.adapter``).
"""

from __future__ import annotations

import os
from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    BeforeValidator,
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
)
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

#: Env var naming the providers YAML file; relative paths resolve against the
#: process CWD (/app in containers, the repo root in the tooling image).
PROVIDERS_CONFIG_FILE_VAR = "PROVIDERS_CONFIG_FILE"
DEFAULT_PROVIDERS_CONFIG_FILE = "config/providers.yaml"


class Environment(StrEnum):
    DEVELOPMENT = "development"
    PRODUCTION_LOCAL = "production-local"


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class LogFormat(StrEnum):
    JSON = "json"
    PRETTY = "pretty"


class ProviderMode(StrEnum):
    """Whether provider calls touch the network at all (SADD §15.3)."""

    MOCK = "mock"
    REAL = "real"
    RECORD = "record"
    REPLAY = "replay"


#: ISO 4217, three uppercase letters. Validated rather than free text so
#: "usd", "dollars" and "US$" fail at settings load instead of appearing in a
#: cost report that nobody can compare across deployments.
def _normalise_currency(value: Any) -> Any:
    """Case and whitespace before validation, not as part of it.

    ``StringConstraints`` applies its pattern to the *input*, so pairing
    ``to_upper=True`` with ``^[A-Z]{3}$`` rejects ``cad`` before it can be
    upper-cased — the normalisation looks present and never runs. Splitting the
    two makes the order explicit rather than a property of pydantic internals.
    """
    return value.strip().upper() if isinstance(value, str) else value


CurrencyCode = Annotated[
    str,
    BeforeValidator(_normalise_currency),
    StringConstraints(pattern=r"^[A-Z]{3}$"),
]


class CoreSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    environment: Environment = Environment.DEVELOPMENT
    log_level: LogLevel = LogLevel.INFO
    log_format: LogFormat = LogFormat.JSON
    api_token: SecretStr = SecretStr("dev-token-change-me")
    internal_hmac_secret: SecretStr = SecretStr("dev-hmac-change-me")
    #: Estimated provider spend allowed per workspace-day (SADD §21.4, S10).
    #:
    #: The unit lives in :attr:`cost_currency`, not in this field's name. It
    #: was ``daily_cost_limit_usd`` until M2-06, and baking the unit into the
    #: identifier turned out to invite exactly the wrong question — an operator
    #: whose Anthropic credits were bought in CAD reasonably asked whether the
    #: number meant their card statement. It does not, and no name ending in
    #: ``_usd`` can say so.
    daily_cost_limit: Decimal = Decimal("10.00")
    #: What the limit above, and every ``Usage.unit_cost_estimate``, are
    #: denominated in.
    #:
    #: **This is a label, not a conversion.** Estimates come from price tables
    #: inside each adapter, and vendors publish those in USD — so changing this
    #: without changing the tables relabels the numbers and makes them wrong.
    #: It exists so the numbers can *say* what they are, which is what the
    #: `Money` value object of §11 was always for.
    cost_currency: CurrencyCode = "USD"


class PostgresSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="POSTGRES_", extra="ignore")

    host: str = "postgres"
    port: int = 5432
    user: str = "videoforge"
    password: SecretStr = SecretStr("videoforge-dev")
    db: str = "videoforge"

    @property
    def dsn(self) -> str:
        """Plain DSN, driver-agnostic. Contains the password — never log it."""
        return (
            f"postgresql://{self.user}:{self.password.get_secret_value()}"
            f"@{self.host}:{self.port}/{self.db}"
        )

    @property
    def sqlalchemy_url(self) -> str:
        """SQLAlchemy URL with the psycopg3 driver pinned in the scheme.

        A string, not an import: settings stays free of SQLAlchemy so the
        packages that don't touch the database never inherit the dependency.
        """
        return (
            f"postgresql+psycopg://{self.user}:{self.password.get_secret_value()}"
            f"@{self.host}:{self.port}/{self.db}"
        )


class RedisSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="REDIS_", extra="ignore")

    url: str = "redis://redis:6379/0"


class CelerySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CELERY_", extra="ignore")

    broker_url: str = "redis://redis:6379/1"
    result_backend: str = "redis://redis:6379/2"


class MinioSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MINIO_", extra="ignore")

    endpoint: str = "http://minio:9000"
    root_user: str = "videoforge"
    root_password: SecretStr = SecretStr("videoforge-dev")
    bucket_artifacts: str = "artifacts"
    bucket_assets: str = "assets"
    bucket_packages: str = "packages"
    bucket_tmp_render: str = "tmp-render"


class RenderSettings(BaseSettings):
    """FFmpeg output parameters (D4). 9:16 vertical at 30fps by default."""

    model_config = SettingsConfigDict(env_prefix="RENDER_", extra="ignore")

    width: int = 1080
    height: int = 1920
    fps: int = 30
    crf: int = 20
    preset: str = "medium"


class LLMProviderConfig(BaseModel):
    adapter: str = "mock"
    model: str = ""
    timeout_s: int = 120


class ImageProviderConfig(BaseModel):
    adapter: str = "mock"
    model: str = ""


class VoiceProviderConfig(BaseModel):
    """Voice adapter selection.

    Reminder for M3: word-level timestamps are a hard capability requirement
    (B3/S5) — the adapter contract test disqualifies providers without them.
    """

    adapter: str = "mock"
    voice_id: str = ""


class ProviderSettings(BaseSettings):
    """Provider *selection*. Keys live in :class:`ProviderKeys`, never here,
    and never in YAML — a config file must not be a place secrets can hide.
    """

    model_config = SettingsConfigDict(
        env_prefix="PROVIDERS__",
        env_nested_delimiter="__",
        extra="ignore",
    )

    mode: ProviderMode = ProviderMode.MOCK
    llm: LLMProviderConfig = Field(default_factory=LLMProviderConfig)
    image: ImageProviderConfig = Field(default_factory=ImageProviderConfig)
    voice: VoiceProviderConfig = Field(default_factory=VoiceProviderConfig)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Insert the YAML layer *below* the environment.

        Order here is priority order: init kwargs beat env, env beats YAML,
        YAML beats code defaults. A missing file is not an error — the file is
        one optional layer, not required configuration.
        """
        yaml_path = Path(
            os.environ.get(PROVIDERS_CONFIG_FILE_VAR, DEFAULT_PROVIDERS_CONFIG_FILE)
        )
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings]
        if yaml_path.is_file():
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=yaml_path))
        sources.append(file_secret_settings)
        return tuple(sources)


class ProviderKeys(BaseSettings):
    """Provider API credentials. Environment only, worker containers only."""

    model_config = SettingsConfigDict(extra="ignore")

    openai_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    elevenlabs_api_key: SecretStr | None = None
    stability_api_key: SecretStr | None = None

    @field_validator("*", mode="before")
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        """Normalise "no key" to exactly one representation: ``None``.

        Compose interpolates ``${OPENAI_API_KEY:-}`` to an **empty string**
        when the host has no value, so in a running container an absent key
        arrives as ``""`` rather than as a missing variable. Without this,
        "unset" is ``None`` outside compose but ``SecretStr('')`` inside it —
        truthiness agrees, but ``is None`` does not, and the registry (M1-06)
        is exactly where someone writes ``is None``.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value


class AppSettings(BaseModel):
    """Everything a service is allowed to know without being a worker."""

    core: CoreSettings
    postgres: PostgresSettings
    redis: RedisSettings
    celery: CelerySettings
    minio: MinioSettings
    render: RenderSettings


class WorkerSettings(AppSettings):
    """AppSettings plus the provider plane. Workers only — see module docstring."""

    providers: ProviderSettings
    provider_keys: ProviderKeys


def load_app_settings() -> AppSettings:
    """Build AppSettings from the current environment. Uncached — see getters."""
    return AppSettings(
        core=CoreSettings(),
        postgres=PostgresSettings(),
        redis=RedisSettings(),
        celery=CelerySettings(),
        minio=MinioSettings(),
        render=RenderSettings(),
    )


def load_worker_settings() -> WorkerSettings:
    """Build WorkerSettings from the current environment. Workers only."""
    return WorkerSettings(
        **dict(load_app_settings()),
        providers=ProviderSettings(),
        provider_keys=ProviderKeys(),
    )


@lru_cache(maxsize=1)
def get_app_settings() -> AppSettings:
    """Process-wide AppSettings. Cached: config is immutable for a process's
    lifetime, and re-reading the environment mid-flight would make behaviour
    depend on *when* a module first ran. Tests use the ``load_*`` functions.
    """
    return load_app_settings()


@lru_cache(maxsize=1)
def get_worker_settings() -> WorkerSettings:
    """Process-wide WorkerSettings. Worker processes only (NF8)."""
    return load_worker_settings()
