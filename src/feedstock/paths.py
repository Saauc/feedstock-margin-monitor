"""
paths.py — resolve project-root paths for runtime data and config.

Code lives in src/feedstock/ but the config and database live at the project
root (they're runtime artifacts, not packaged code). We locate the root by
walking up from this file until we find pyproject.toml, so it works whether the
package is run in-place (PYTHONPATH=src) or installed editable (pip install -e).
"""

from __future__ import annotations

from pathlib import Path


def find_project_root(start: Path | None = None) -> Path:
    start = (start or Path(__file__)).resolve()
    for parent in [start, *start.parents]:
        if (parent / "pyproject.toml").exists():
            return parent
    # Fallback: src/feedstock/paths.py -> repo root is two levels up.
    return Path(__file__).resolve().parents[2]


PROJECT_ROOT = find_project_root()
CONFIG_PATH = PROJECT_ROOT / "config.json"
DB_PATH = PROJECT_ROOT / "feedstock.db"
ENV_PATH = PROJECT_ROOT / ".env"
