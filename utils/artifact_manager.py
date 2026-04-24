# utils/artifact_manager.py

import os
import re
from pathlib import Path
from typing import Any, Union
import config


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
    safe_job_id = re.sub(r"[^A-Za-z0-9_-]", "", job_id)
    
    job_dir = target_dir / safe_tool / safe_job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    
    filename = f"{safe_type}.{safe_ext}"
    filepath = job_dir / filename
    
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
    """Construct a public URL for an artifact given a FastAPI `request` and
    the artifact relative path (posix-style) under the artifacts root.
    """
    base = str(request.base_url).rstrip("/")
    rel = rel_path.lstrip("/")
    return f"{base}/artifacts/{rel}"
