# utils/startup/registry.py

from tools.registry import REGISTRY
from utils.logger.core import get_dual_logger

log = get_dual_logger(__name__)

async def load_tool_registry() -> None:
    REGISTRY.load_all()
    loaded_tools = list(REGISTRY._tools.keys())
    diagnostics = REGISTRY.diagnostic_list()
    
    failed_tools = {k: v for k, v in diagnostics.items() if v.get("status") in ("FAILED", "REJECTED", "MISSING")}
    
    payload = {
        "total_discovered": len(diagnostics),
        "loaded_count": len(loaded_tools),
        "loaded_tools": loaded_tools,
        "failed_count": len(failed_tools),
        "failed_tools": failed_tools,
    }
    
    level = "WARNING" if failed_tools else "INFO"
    log.dual_log(
        tag="Startup:Registry",
        message=f"Tool registry loaded: {len(loaded_tools)} successful, {len(failed_tools)} failed/missing",
        level=level,
        payload=payload
    )
    
    if len(loaded_tools) == 0:
        raise RuntimeError(f"Registry loaded 0 tools. Diagnostics: {failed_tools}")
