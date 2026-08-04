"""Reading ``templates/pipeline.yaml`` — the I/O half of ADR-009.

Deliberately separate from ``videoforge_domain.pipeline``, which turns the
parsed mapping into a validated graph. Parsing a declaration into workflow
rules is a domain concern; opening a file is not, and ADR-015 is explicit that
a new dependency on ``packages/domain`` is a claim needing an argument. PyYAML
would be exactly that claim, for no gain: the graph is more testable built from
a dict literal anyway.

So the split is one function wide, and the seam is a plain mapping.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

__all__ = ["DEFAULT_PIPELINE_FILE", "PIPELINE_FILE_ENV", "load_pipeline_mapping"]

#: Relative to the repository root in development, and to ``/app`` in the
#: container images — the same shape as ``config/providers.yaml``.
DEFAULT_PIPELINE_FILE = "templates/pipeline.yaml"

#: Override, for tests that want a deliberately broken graph and for an
#: operator pinning an alternative pipeline without rebuilding an image.
PIPELINE_FILE_ENV = "VIDEOFORGE_PIPELINE_FILE"


def load_pipeline_mapping(
    path: str | os.PathLike[str] | None = None,
) -> Mapping[str, Any]:
    """Read and parse the pipeline declaration.

    Raises ``FileNotFoundError`` rather than falling back to a built-in
    default. A missing pipeline file means a container built wrong, and a
    silent default would let it start and then behave as though stages the
    operator removed still exist.
    """
    resolved = Path(path or os.environ.get(PIPELINE_FILE_ENV) or DEFAULT_PIPELINE_FILE)
    text = resolved.read_text(encoding="utf-8")

    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"{resolved} does not contain a YAML mapping")
    return data
