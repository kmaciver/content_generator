"""Structural tests for the monorepo itself (M0-01).

These guard the two rules from SADD §8 and §10.1 that are cheap to enforce now
and effectively impossible to retrofit once violated:

1. Every workspace package resolves and imports.
2. The dependency arrow points apps -> packages, and the domain layer stays
   free of frameworks.

They are deliberately static (AST-based) rather than import-based for the layer
checks, so a violation is caught even in a module that is never imported by the
test run.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

WORKSPACE_PACKAGES = [
    "videoforge",
    "videoforge_workers",
    "videoforge_shared",
    "videoforge_providers",
    "videoforge_prompts",
    "videoforge_timeline",
]

# The domain layer encodes workflow rules and must be unit-testable without a
# database, a broker, or an app context. If any of these appear there, the layer
# has stopped being pure and the DB-free domain tests promised in SADD §22 are
# no longer possible.
FRAMEWORKS_BANNED_FROM_DOMAIN = (
    "flask",
    "sqlalchemy",
    "celery",
    "redis",
    "boto3",
    "botocore",
    "minio",
    "requests",
    "httpx",
    "psycopg",
)


def _imported_modules(source: Path) -> set[str]:
    """Return top-level module names imported by a Python file."""
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".")[0])
        # node.level == 0 excludes relative imports, which name no top-level module.
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module.split(".")[0])
    return modules


def _python_files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.rglob("*.py") if "__pycache__" not in p.parts)


@pytest.mark.parametrize("package", WORKSPACE_PACKAGES)
def test_workspace_package_imports(package: str) -> None:
    """Each workspace member is installed and importable.

    This is what catches a broken hatchling `packages` path or a member missing
    from the uv workspace -- failures that otherwise surface much later as a
    confusing ImportError inside a container.
    """
    module = importlib.import_module(package)
    assert module.__doc__, f"{package} should carry a module docstring"


def test_domain_layer_imports_no_frameworks() -> None:
    """domain/ stays pure: no web, ORM, broker, or HTTP dependencies.

    **[Fixed M2-02]** This scanned ``apps/backend/src/videoforge/domain`` — the
    directory ADR-015 *emptied* in M1-02 when the workflow rules moved to
    ``packages/domain``. It therefore walked one empty ``__init__.py`` and
    passed unconditionally: the purity guarantee ADR-015's whole argument rests
    on has not actually been checked since the day it was made.

    Both paths are scanned now. The backend one stays because the placeholder
    still exists and something re-populating it should fail here.
    """
    roots = [
        REPO_ROOT / "packages" / "domain" / "src" / "videoforge_domain",
        REPO_ROOT / "apps" / "backend" / "src" / "videoforge" / "domain",
    ]
    for root in roots:
        assert root.is_dir(), f"{root.relative_to(REPO_ROOT)} is missing"

    violations: list[str] = []
    for root in roots:
        for path in _python_files(root):
            banned = _imported_modules(path) & set(FRAMEWORKS_BANNED_FROM_DOMAIN)
            violations.extend(
                f"{path.relative_to(REPO_ROOT)} imports {name}"
                for name in sorted(banned)
            )

    assert not violations, "domain layer must stay framework-free:\n" + "\n".join(
        violations
    )


def test_apps_do_not_import_each_other() -> None:
    """backend and workers share code through packages/ and the database only.

    A direct import between them is the beginning of the "worker imports the
    Flask app" tangle that the apps/packages split exists to prevent.
    """
    pairs = [
        ("apps/backend/src/videoforge", "videoforge_workers"),
        ("apps/workers/src/videoforge_workers", "videoforge"),
    ]

    violations: list[str] = []
    for relative_source, forbidden in pairs:
        source_root = REPO_ROOT / relative_source
        if not source_root.is_dir():
            continue
        for path in _python_files(source_root):
            if forbidden in _imported_modules(path):
                violations.append(f"{path.relative_to(REPO_ROOT)} imports {forbidden}")

    assert not violations, "apps must not import each other:\n" + "\n".join(violations)
