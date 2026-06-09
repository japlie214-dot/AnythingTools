# utils/startup/cleanup.py

import os
from utils.logger.core import get_dual_logger

log = get_dual_logger(__name__)

async def cleanup_zombie_chrome() -> None:
    try:
        import psutil
    except ImportError:
        return

    zombie_count = 0
    try:
        for p in psutil.process_iter(["pid", "name", "cmdline"]):
            name = p.info["name"] or ""
            cmdline = " ".join(p.info["cmdline"] or [])
            if "chrome" in name.lower() or "chromium" in name.lower():
                if p.status() == "zombie":
                    zombie_count += 1
                    p.kill()
        if zombie_count > 0:
            log.dual_log(tag="Startup:Cleanup:ChromeZombies", message=f"Cleaned up {zombie_count} zombie chrome processes", level="INFO", payload={"zombie_count": zombie_count})
    except Exception as e:
        log.dual_log(tag="Startup:Cleanup:ChromeError", message=f"Chrome cleanup warning: {e}", level="WARNING", payload={"error": str(e)})

    try:
        os.makedirs("chrome_download", exist_ok=True)
    except Exception:
        pass

async def cleanup_temp_files() -> None:
    """Clean up temporary files from the data directory."""
    # Legacy .tmp.parquet cleanup removed as we no longer use local parquet staging
    pass
