# utils/startup/server.py

import config
from pathlib import Path
from utils.logger.core import get_dual_logger

log = get_dual_logger(__name__)

def get_mount_artifacts_step(app_instance):
    async def step():
        artifacts_path = getattr(config, "ANYTHINGLLM_ARTIFACTS_DIR", "artifacts")
        if not artifacts_path:
            artifacts_path = "artifacts"
            
        artifacts_dir = Path(artifacts_path).resolve()
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        if app_instance:
            from fastapi.staticfiles import StaticFiles
            app_instance.mount("/api/artifacts", StaticFiles(directory=str(artifacts_dir)), name="artifacts")
            app_instance.mount("/artifacts", StaticFiles(directory=str(artifacts_dir)), name="artifacts_public")

        log.dual_log(tag="Startup:Artifacts", message=f"Artifacts directory mounted at {artifacts_dir}", level="INFO")
    return step
