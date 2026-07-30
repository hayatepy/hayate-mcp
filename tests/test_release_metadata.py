from __future__ import annotations

import tomllib
from pathlib import Path

from hayate_mcp import __version__

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_HOME = "https://hayatepy.dev/"
PUBLIC_PACKAGE_HOME = "https://hayatepy.dev/ecosystem/#hayate-mcp"
PUBLIC_COMPATIBILITY = "https://hayatepy.dev/evidence/compatibility/"
SUPERSEDED_DOCS_PREFIX = "https://github.com/hayatepy/.github/blob/main/docs/"


def test_distribution_version_and_homepage_are_consistent() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert project["version"] == __version__
    assert project["urls"]["Homepage"] == PUBLIC_PACKAGE_HOME


def test_readme_uses_canonical_public_discovery() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert PUBLIC_HOME in readme
    assert PUBLIC_COMPATIBILITY in readme
    assert SUPERSEDED_DOCS_PREFIX not in readme
