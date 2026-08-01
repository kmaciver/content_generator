"""Unit tests for the layered settings (M0-04).

Hermetic by construction: the fixture strips every variable the models read and
points the YAML layer at a nonexistent file, so results cannot depend on the
host's shell, a stray .env, or the tracked config/providers.yaml.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from videoforge_shared.settings import (
    PROVIDERS_CONFIG_FILE_VAR,
    CoreSettings,
    Environment,
    PostgresSettings,
    ProviderKeys,
    ProviderMode,
    ProviderSettings,
    RenderSettings,
    load_app_settings,
    load_worker_settings,
)

_PREFIXES = ("POSTGRES_", "REDIS_", "CELERY_", "MINIO_", "RENDER_", "PROVIDERS__")
_FLAT_VARS = (
    "ENVIRONMENT",
    "LOG_LEVEL",
    "LOG_FORMAT",
    "API_TOKEN",
    "INTERNAL_HMAC_SECRET",
    "DAILY_COST_LIMIT_USD",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "ELEVENLABS_API_KEY",
    "STABILITY_API_KEY",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip everything the settings read; disable the tracked YAML file."""
    import os

    for key in list(os.environ):
        upper = key.upper()
        if upper.startswith(_PREFIXES) or upper in _FLAT_VARS:
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(PROVIDERS_CONFIG_FILE_VAR, "/nonexistent/providers.yaml")


class TestDefaults:
    def test_app_settings_boot_with_empty_environment(self) -> None:
        settings = load_app_settings()
        assert settings.core.environment is Environment.DEVELOPMENT
        assert settings.postgres.host == "postgres"
        assert settings.redis.url == "redis://redis:6379/0"
        assert settings.celery.broker_url == "redis://redis:6379/1"
        assert settings.minio.bucket_artifacts == "artifacts"
        assert settings.render.width == 1080
        assert settings.render.height == 1920

    def test_worker_settings_default_to_mock_and_no_keys(self) -> None:
        settings = load_worker_settings()
        assert settings.providers.mode is ProviderMode.MOCK
        assert settings.providers.llm.adapter == "mock"
        assert settings.provider_keys.openai_api_key is None


class TestEnvLayer:
    def test_prefixed_flat_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POSTGRES_HOST", "db.internal")
        monkeypatch.setenv("POSTGRES_PORT", "6543")
        pg = PostgresSettings()
        assert pg.host == "db.internal"
        assert pg.port == 6543

    def test_nested_delimiter_reaches_leaf(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROVIDERS__MODE", "replay")
        monkeypatch.setenv("PROVIDERS__LLM__ADAPTER", "anthropic")
        monkeypatch.setenv("PROVIDERS__LLM__TIMEOUT_S", "7")
        providers = ProviderSettings()
        assert providers.mode is ProviderMode.REPLAY
        assert providers.llm.adapter == "anthropic"
        assert providers.llm.timeout_s == 7

    def test_provider_key_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
        keys = ProviderKeys()
        assert keys.openai_api_key is not None
        assert keys.openai_api_key.get_secret_value() == "sk-test-123"

    def test_empty_key_normalises_to_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The condition that actually occurs in a container: compose expands
        ${OPENAI_API_KEY:-} to "" when the host has no value. Absent and empty
        must produce the SAME representation, or `is None` lies (M1-00)."""
        monkeypatch.setenv("OPENAI_API_KEY", "")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")
        keys = ProviderKeys()
        assert keys.openai_api_key is None
        assert keys.anthropic_api_key is None

    def test_absent_and_empty_agree(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        absent = ProviderKeys().openai_api_key
        monkeypatch.setenv("OPENAI_API_KEY", "")
        empty = ProviderKeys().openai_api_key
        assert absent is empty is None


class TestYamlLayer:
    def _write_yaml(self, path: Path, body: str) -> None:
        path.write_text(body, encoding="utf-8")

    def test_yaml_beats_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        yaml_file = tmp_path / "providers.yaml"
        self._write_yaml(
            yaml_file,
            "mode: record\nllm:\n  adapter: anthropic\n  timeout_s: 33\n",
        )
        monkeypatch.setenv(PROVIDERS_CONFIG_FILE_VAR, str(yaml_file))
        providers = ProviderSettings()
        assert providers.mode is ProviderMode.RECORD
        assert providers.llm.adapter == "anthropic"
        assert providers.llm.timeout_s == 33
        # Sections the file omits keep their code defaults.
        assert providers.image.adapter == "mock"

    def test_env_beats_yaml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        yaml_file = tmp_path / "providers.yaml"
        self._write_yaml(yaml_file, "mode: record\nllm:\n  adapter: anthropic\n")
        monkeypatch.setenv(PROVIDERS_CONFIG_FILE_VAR, str(yaml_file))
        monkeypatch.setenv("PROVIDERS__MODE", "real")
        providers = ProviderSettings()
        assert providers.mode is ProviderMode.REAL, "env must outrank yaml"
        assert providers.llm.adapter == "anthropic", "yaml still fills env gaps"

    def test_missing_yaml_is_not_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(PROVIDERS_CONFIG_FILE_VAR, "/definitely/not/there.yaml")
        assert ProviderSettings().mode is ProviderMode.MOCK


class TestSecretHygiene:
    def test_secrets_do_not_leak_via_repr(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("API_TOKEN", "super-secret-token")
        monkeypatch.setenv("POSTGRES_PASSWORD", "super-secret-pass")
        core = CoreSettings()
        pg = PostgresSettings()
        for rendered in (repr(core), str(core), repr(pg), str(pg)):
            assert "super-secret" not in rendered

    def test_dsn_carries_the_real_password(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("POSTGRES_PASSWORD", "pw123")
        assert PostgresSettings().dsn == (
            "postgresql://videoforge:pw123@postgres:5432/videoforge"
        )


class TestValidation:
    def test_bad_enum_value_fails_loudly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PROVIDERS__MODE", "yolo")
        with pytest.raises(Exception, match="mode"):
            ProviderSettings()

    def test_bad_int_fails_loudly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RENDER_FPS", "thirty")
        with pytest.raises(Exception, match="fps"):
            RenderSettings()
