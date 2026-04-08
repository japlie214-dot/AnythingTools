# utils/artifacts.py
"""
Utilities for validating and normalizing artifact file paths under the
AnythingTools `artifacts/` root. All functions ensure paths are resolved and
constrained to the configured ARTIFACTS_ROOT.
"""
from pathlib import Path
from typing import Union
import config as cfg

ARTIFACTS_ROOT: Path = Path(cfg.ARTIFACTS_ROOT).resolve()


def normalize_artifact_path(path_str: str) -> Path:
    """Return a resolved Path that is guaranteed to be inside ARTIFACTS_ROOT.

    Accepts either:
      - A path already relative to the project like "artifacts/scrapes/x.json"
      - An absolute path under the artifacts root
      - A basename or relative path (treated as relative to ARTIFACTS_ROOT)

    Raises ValueError if the resulting path is outside ARTIFACTS_ROOT.
    """
    p = Path(path_str)

    # If the value begins with the literal folder name 'artifacts', treat it as
    # project-relative and interpret the remainder under ARTIFACTS_ROOT.
    sp = str(p)
    if sp.startswith("artifacts/") or sp.startswith("artifacts\\") or sp == "artifacts":
        # strip leading 'artifacts' segment and join to ARTIFACTS_ROOT
        remainder = sp.split("/", 1)[1] if "/" in sp else ""
        remainder = remainder.split("\\", 1)[1] if "\\" in sp and not remainder else remainder
        target = ARTIFACTS_ROOT / remainder
    elif not p.is_absolute():
        # Treat other relative paths as relative to ARTIFACTS_ROOT
        target = ARTIFACTS_ROOT / p
    else:
        # Absolute path — accept only if it is inside ARTIFACTS_ROOT
        target = p

    target = target.resolve()

    try:
        target.relative_to(ARTIFACTS_ROOT)
    except Exception:
        raise ValueError(f"Artifact path outside artifacts root: {path_str}")

    return target


def artifact_relpath_for_http(path: Union[str, Path]) -> str:
    """Return a normalized relative path (posix-style) to append to /artifacts/ URL.

    Example: Path('C:/.../project/artifacts/scrapes/top.json') -> 'scrapes/top.json'
    """
    p = Path(path)
    if not p.is_absolute():
        p = normalize_artifact_path(str(p))

    rel = p.relative_to(ARTIFACTS_ROOT)
    # Use forward slashes for URLs
    return str(rel.as_posix())


def artifact_url_from_request(request, rel_path: str) -> str:
    """Construct a public URL for an artifact given a FastAPI `request` and
    the artifact relative path (posix-style) under the artifacts root.
    """
    base = str(request.base_url).rstrip("/")
    rel = rel_path.lstrip("/")
    return f"{base}/artifacts/{rel}"
