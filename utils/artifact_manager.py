# utils/artifact_manager.py

import re
from pathlib import Path
from typing import Any, Union
import config


def get_artifacts_root() -> Path:
    """Resolve the artifacts root directory from config.ARTIFACTS_DIR.

    This was previously config.ANYTHINGLLM_ARTIFACTS_DIR (AnythingLLM's
    custom-documents folder). It is now a generic local directory,
    defaulting to data/artifacts/ (or data/staging/artifacts/ when
    DATABASE_STAGING_ENABLED=true).
    """
    artifacts_dir = getattr(config, "ARTIFACTS_DIR", None)
    if not artifacts_dir:
        # Fall back to a sensible default rather than raising.
        # This prevents crashes if config is not yet loaded.
        artifacts_dir = "data/artifacts"
    return Path(artifacts_dir).resolve()

def write_artifact(tool_name: str, job_id: str, artifact_type: str, ext: str, content: str | bytes) -> Path:
    """Write an artifact file atomically under <root>/<tool>/<job_id>/<type>.<ext>.

    Uses a temp file + atomic rename to prevent partial writes from
    being read by concurrent consumers. This pattern is recommended by
    the atomic write recipe at:
    https://docs.python.org/3/library/pathlib.html#pathlib.Path.replace
    """
    target_dir = get_artifacts_root()
    target_dir.mkdir(parents=True, exist_ok=True)

    # Sanitize path components to prevent directory traversal.
    # Only [a-z_] for tool/type, [a-z0-9_] for ext, [A-Za-z0-9_-] for job_id.
    safe_tool = re.sub(r"[^a-z_]", "", tool_name.lower())
    safe_type = re.sub(r"[^a-z0-9_]", "", artifact_type.lower())
    safe_ext = re.sub(r"[^a-z]", "", ext.lower())
    safe_job_id = re.sub(r"[^A-Za-z0-9_-]", "", job_id)

    job_dir = target_dir / safe_tool / safe_job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{safe_type}.{safe_ext}"
    filepath = job_dir / filename

    # Atomic write: write to .tmp, then rename. On POSIX, rename is atomic.
    # Ref: https://docs.python.org/3/library/os.html#os.replace
    temp_path = filepath.with_suffix(f".tmp{filepath.suffix}")
    mode = "w" if isinstance(content, str) else "wb"
    encoding = "utf-8" if isinstance(content, str) else None

    try:
        with open(temp_path, mode, encoding=encoding) as fh:
            fh.write(content)
        temp_path.replace(filepath)
    except Exception:
        if temp_path.exists():
            temp_path.unlink()
        raise

    return filepath


def artifact_url_from_request(request, rel_path: str) -> str:
    """Construct a public URL for an artifact given a FastAPI request and
    the artifact relative path (posix-style) under the artifacts root.
    """
    base = str(request.base_url).rstrip("/")
    rel = rel_path.lstrip("/")
    return f"{base}/artifacts/{rel}"
