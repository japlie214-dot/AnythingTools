# utils/startup/__init__.py

from .core import StartupContext, StartupOrchestrator
from .cleanup import cleanup_zombie_chrome, cleanup_temp_files
from .server import get_mount_artifacts_step
from .database import init_database_layer, run_db_migrations, validate_vec0
from .registry import load_tool_registry
from .browser import warmup_browser
from .recovery import run_startup_recovery
# from .hydration import hydrate_from_backup

_global_dual_engine = None

async def _init_backup_step() -> None:
    from database.backup.settings import BackupSettings
    from database.backup.engine.dual_engine import DualEngine
    from utils.logger import get_dual_logger
    import asyncio
    
    log = get_dual_logger(__name__)
    try:
        settings = BackupSettings()
        engine = DualEngine(settings)
        result = await asyncio.to_thread(engine.startup)
        log.dual_log(tag="Startup:Backup:Init", message="DualEngine backup initialized", level="INFO", payload=result)
        
        global _global_dual_engine
        _global_dual_engine = engine
    except Exception as e:
        log.dual_log(tag="Startup:Backup:Error", message=f"Backup engine failed to initialize: {e}", level="WARNING", payload={"error": str(e)})

async def run_startup(app_instance=None) -> StartupContext:
    ctx = StartupContext()
    orchestrator = StartupOrchestrator()

    # Tier 1: Independent setup/cleanup (Concurrent)
    orchestrator.add_concurrent_tier([
        ("mount_artifacts", get_mount_artifacts_step(app_instance)),
        ("cleanup_zombie_chrome", cleanup_zombie_chrome),
        ("cleanup_temp_files", cleanup_temp_files),
        ("init_database_layer", init_database_layer),
    ])

    # Tier 2: Dependent Database logic (Sequential)
    orchestrator.add_sequential("run_db_migrations", run_db_migrations)
    # Note: hydrate_from_backup (legacy JSON hydration) removed in favor of DualEngine
    # orchestrator.add_sequential("hydrate_from_backup", hydrate_from_backup)
    orchestrator.add_sequential("validate_vec0", validate_vec0)
    orchestrator.add_sequential("init_backup", _init_backup_step)
    orchestrator.add_sequential("startup_recovery", run_startup_recovery)

    # Tier 3: Application logic (Concurrent)
    orchestrator.add_concurrent_tier([
        ("load_tool_registry", load_tool_registry),
        ("warmup_browser", warmup_browser),
    ])

    await orchestrator.run(ctx)
    return ctx
