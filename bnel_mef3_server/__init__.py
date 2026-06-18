"""
BNEL MEF3 Server Python package.

This package provides gRPC-based access to MEF3 files, including server, client, and protobuf definitions.
It is designed for efficient, concurrent, and robust access to large-scale electrophysiology data.
"""

# Public package exports.
#
# Importing from this module provides the stable public surface intended for
# downstream use.

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import re

def _detect_version() -> str:
    """Return the installed package version or fall back to the nearest ``pyproject.toml``."""

    for package_name in ("alera-parser", "alera-dashboard-engine"):
        try:
            return version(package_name)
        except PackageNotFoundError:
            pass

    try:
        pyproject_path = next(
            (parent / "pyproject.toml" for parent in Path(__file__).resolve().parents if (parent / "pyproject.toml").exists()),
            None,
        )
        if pyproject_path is None:
            raise RuntimeError("Unable to locate pyproject.toml for version detection")
        match = re.search(
            r'^version\s*=\s*"([^"]+)"\s*$',
            pyproject_path.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        if match is None:
            raise RuntimeError(
                "Unable to determine package version from pyproject.toml"
            )
        return match.group(1)
    except RuntimeError:
        raise


__version__ = _detect_version()

__all__ = [
    "__version__",
]
