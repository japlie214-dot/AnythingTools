# utils/startup/cleanup.py

import os
from utils.logger.core import get_dual_logger
from database.backup.config import BackupConfig

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
                if p.status() == "zombie" or "--headless" in cmdline:
                    zombie_count += 1
                    p.kill()
                elif os.name != "nt" and "DISPLAY" not in os.environ and "--headless" not in cmdline:
                    zombie_count += 1
                    p.kill()
        if zombie_count > 0:
            log.dual_log(tag="Startup:ChromeCleanup", message=f"Cleaned up {zombie_count} zombie chrome processes", level="INFO", payload={"zombie_count": zombie_count})
    except Exception as e:
        log.dual_log(tag="Startup:ChromeCleanup", message=f"Chrome cleanup warning: {e}", level="WARNING", payload={"error": str(e)})

    try:
        os.makedirs("chrome_download", exist_ok=True)
    except Exception:
        pass

async def cleanup_temp_files() -> None:
    try:
        bak_cfg = BackupConfig.from_global_config()
        if bak_cfg.enabled and bak_cfg.backup_dir.exists():
            removed_count = 0
            for p in bak_cfg.backup_dir.rglob("*.tmp.parquet"):
                try:
                    p.unlink(missing_ok=True)
                    removed_count += 1
                except Exception:
                    pass
            log.dual_log(tag="Startup:Cleanup", message="Cleaned up temp Parquet files", level="INFO", payload={"files_removed": removed_count})
    except Exception as e:
        log.dual_log(tag="Startup:Cleanup", message=f"Temp file cleanup warning: {e}", level="WARNING", payload={"error": str(e)})
