"""NF8 / SADD §21.3: provider credentials reach worker containers only.

Two static guards, both runnable offline:

1. The compose topology never hands a provider key variable to anything but the
   provider-calling workers (PyYAML resolves the ``<<:`` merge anchors, so this
   inspects what each service actually receives).
2. Backend source never names the worker-side settings types — the code-level
   twin of the container-level rule.

The runtime version of guard 1 — booting the actual containers and reading
their environment — is ``scripts/verify-secret-isolation.sh`` (`make
verify-secrets`), which CI runs with Docker available. These tests are the
fast, always-on layer.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_DIR = REPO_ROOT / "docker" / "compose"

PROVIDER_KEY_VARS = {
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "ELEVENLABS_API_KEY",
    "STABILITY_API_KEY",
}

# The only services that call providers, and therefore the only ones that may
# hold credentials. worker-render is deliberately NOT here: rendering is pure
# FFmpeg (D4) and needs no provider, so it gets no keys.
SERVICES_ALLOWED_KEYS = {"worker-llm", "worker-media", "worker-core"}

# Names the backend must never reference (see videoforge/config/__init__.py).
FORBIDDEN_IN_BACKEND = {"WorkerSettings", "ProviderKeys", "get_worker_settings"}


def _env_keys(service_spec: dict[str, Any]) -> set[str]:
    """Environment variable names a compose service receives, both syntaxes."""
    env = service_spec.get("environment") or {}
    if isinstance(env, dict):
        return set(env)
    # List form: ["KEY=value", "KEY"]
    return {entry.split("=", 1)[0] for entry in env}


def _services(compose_file: Path) -> dict[str, dict[str, Any]]:
    doc = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    services = doc.get("services") or {}
    assert isinstance(services, dict)
    return services


def test_provider_keys_reach_only_provider_workers() -> None:
    services = _services(COMPOSE_DIR / "docker-compose.yml")
    assert set(services) >= SERVICES_ALLOWED_KEYS, "topology changed under the test"

    violations: list[str] = []
    for name, spec in services.items():
        keys_present = PROVIDER_KEY_VARS & _env_keys(spec)
        if name in SERVICES_ALLOWED_KEYS:
            missing = PROVIDER_KEY_VARS - keys_present
            if missing:
                violations.append(f"{name}: expected keys missing: {sorted(missing)}")
        elif keys_present:
            violations.append(f"{name}: MUST NOT hold keys, has {sorted(keys_present)}")

    assert not violations, "NF8 broken:\n" + "\n".join(violations)


def test_override_files_do_not_smuggle_keys() -> None:
    """Profile overrides may tune ports and mounts, never credentials."""
    for override in ("compose.dev.yml", "compose.prod.yml"):
        for name, spec in _services(COMPOSE_DIR / override).items():
            leaked = PROVIDER_KEY_VARS & _env_keys(spec)
            assert not leaked, f"{override}:{name} adds provider keys {sorted(leaked)}"


def test_backend_source_never_names_worker_settings() -> None:
    backend_root = REPO_ROOT / "apps" / "backend" / "src" / "videoforge"
    violations: list[str] = []
    for path in sorted(backend_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(REPO_ROOT)
        hits: list[tuple[int, str]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in FORBIDDEN_IN_BACKEND:
                hits.append((node.lineno, node.id))
            elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_IN_BACKEND:
                hits.append((node.lineno, node.attr))
            elif isinstance(node, ast.ImportFrom):
                hits.extend(
                    (node.lineno, alias.name)
                    for alias in node.names
                    if alias.name in FORBIDDEN_IN_BACKEND
                )
        violations.extend(f"{rel}:{line} references {name}" for line, name in hits)

    assert (
        not violations
    ), "backend must never touch worker-side settings (NF8):\n" + "\n".join(violations)
