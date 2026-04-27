# utils/startup/registry.py

from tools.registry import REGISTRY
from utils.logger.core import get_dual_logger

log = get_dual_logger(__name__)

async def load_tool_registry() -> None:
    REGISTRY.load_all()
    loaded = len(REGISTRY._tools)
    if loaded == 0:
        raise RuntimeError("Registry loaded 0 tools.")
    log.dual_log(tag="Startup:Registry", message=f"Tool registry loaded: {loaded} tools", level="INFO")
