# utils/artifact_manager.py

import os
import re
from pathlib import Path
from typing import Any, Union
import config

VALID_ARTIFACT_NAME = re.compile(r"^[a-z_]+_[A-Z0-9]{26}_[a-z0-9_]+\.[a-z]+$")

def get_artifacts_root() -> Path:
    if not config.ANYTHINGLLM_ARTIFACTS_DIR:
        raise RuntimeError("ANYTHINGLLM_ARTIFACTS_DIR is empty.")
    return Path(config.ANYTHINGLLM_ARTIFACTS_DIR).resolve()

def write_artifact(tool_name: str, job_id: str, artifact_type: str, ext: str, content: str | bytes) -> Path:
    target_dir = get_artifacts_root()
    target_dir.mkdir(parents=True, exist_ok=True)
    
    safe_tool = re.sub(r"[^a-z_]", "", tool_name.lower())
    safe_type = re.sub(r"[^a-z0-9_]", "", artifact_type.lower())
    safe_ext = re.sub(r"[^a-z]", "", ext.lower())
    filename = f"{safe_tool}_{job_id}_{safe_type}.{safe_ext}"
    
    if not VALID_ARTIFACT_NAME.match(filename):
        raise ValueError(f"Invalid artifact filename: {filename}")
        
    filepath = target_dir / filename
    
    mode = "w" if isinstance(content, str) else "wb"
    encoding = "utf-8" if isinstance(content, str) else None
    with open(filepath, mode, encoding=encoding) as fh:
        fh.write(content)
        
    return filepath


def artifact_url_from_request(request, rel_path: str) -> str:
    """Construct a public URL for an artifact given a FastAPI `request` and
    the artifact relative path (posix-style) under the artifacts root.
    """
    base = str(request.base_url).rstrip("/")
    rel = rel_path.lstrip("/")
    return f"{base}/artifacts/{rel}"
