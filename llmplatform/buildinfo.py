from __future__ import annotations

import hashlib
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
BUILD_FILES = (
    "llmplatform/app.py",
    "llmplatform/buildinfo.py",
    "llmplatform/diagnostics.py",
    "llmplatform/diagnostics_export.py",
    "llmplatform/harness.py",
    "llmplatform/inference.py",
    "llmplatform/llamacpp.py",
    "llmplatform/monitoring.py",
    "llmplatform/providers.py",
    "llmplatform/service.py",
    "llmplatform/storage.py",
    "llmplatform/tools.py",
    "llmplatform/workflow_engine.py",
)


def _compute_build_id() -> str:
    digest = hashlib.sha256()
    for relative in BUILD_FILES:
        path = ROOT / relative
        digest.update(relative.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


# Capture the source fingerprint when the server imports this module. Computing
# it for every health request would let a stale process report the new disk
# version after files changed, hiding that it needs a restart.
BUILD_ID = _compute_build_id()


def get_build_id() -> str:
    """Return the backend source fingerprint loaded by this process."""
    return BUILD_ID
