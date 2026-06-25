# utils/startup/__init__.py

from .core import StartupContext, StartupOrchestrator
from .cleanup import cleanup_zombie_chrome, cleanup_temp_files
from .server import get_mount_artifacts_step
from .database import init_database_layer, run_db_migrations, validate_vec0
from .registry import load_tool_registry
from .browser import warmup_browser
from .recovery import run_startup_recovery

_global_sync_engine = None

async def _init_backup_step() -> None:
    # Honor the master DB integration toggle. When disabled, skip backup
    # engine initialization and cloud-writer startup entirely.
    import config
    if not getattr(config, "DATABASE_INTEGRATION_ENABLED", True):
        log = get_dual_logger(__name__)
        log.dual_log(
            tag="Database:Integration:Disabled",
            level="INFO",
            message="Database integration disabled; skipping init_backup",
            payload={"action": "skip", "reason": "toggle_disabled"},
        )
        return

    from database.backup.settings import BackupSettings
    from database.backup.engine.sync_engine import SyncEngine
    from database.backup.writer.cloud_writer import start_cloud_writer
    from utils.logger import get_dual_logger
    import asyncio
    
    log = get_dual_logger(__name__)
    try:
        settings = BackupSettings()
        engine = SyncEngine(settings)
        result = await asyncio.to_thread(engine.startup)
        log.dual_log(tag="Startup:Backup:Init", message="SyncEngine initialized", level="INFO", payload=result)
        
        # Start the cloud writer thread reusing the SyncEngine's CloudEngine
        start_cloud_writer(cloud_engine=engine.cloud)
        
        global _global_sync_engine
        _global_sync_engine = engine
    except Exception as e:
        log.dual_log(tag="Startup:Backup:Error", message=f"Backup engine failed: {e}", level="WARNING", payload={"error": str(e)})

# Startup vs Shutdown sync semantics
# ----------------------------------
# Developer note:
# - The startup sync (_sync_from_backup_step) runs during application
#   initialization and must perform a *smart* evaluation (via SyncEngine.sync_startup)
#   that compares local and cloud proofs and may decide to:
#     * skip (no-op)
#     * push local -> cloud
#     * pull cloud -> local (restore)
#     * run a bidirectional sync with HITL conflict resolution
#   Because actions like "pull" or "restore" can be destructive to local state
#   they are intentionally run only during startup when the operator expects
#   lifecycle-affecting behaviors and when the SyncEngine owns the CloudEngine
#   pool.
#
# - The shutdown sync (the cloud writer thread final flush) is strictly
#   best-effort and MUST NOT attempt long-running, blocking, or state-altering
#   cloud pulls or restores. On shutdown we only flush queued upserts/deletes
#   to cloud and release resources if the writer actually owns them. This
#   avoids long blocking during process termination and prevents races where
#   a shutdown-initiated restore could overwrite an operator's recent changes.
#
# Keep these semantics in mind when modifying startup/shutdown orchestration.

async def _staging_wipe_step() -> None:
    """Wipe staging tables on startup when staging mode is on.

    Runs before any migrations or sync operations so that staging
    starts clean. Best-effort: failures are logged WARNING, never block startup.
    """
    from config import DATABASE_STAGING_ENABLED, DATABASE_STAGING_WIPE_ON_STARTUP
    from utils.logger import get_dual_logger
    log = get_dual_logger(__name__)

    if not DATABASE_STAGING_ENABLED or not DATABASE_STAGING_WIPE_ON_STARTUP:
        return

    from database.backup.staging import StagingWipeService

    # Wipe SQLite staging tables
    sqlite_result = StagingWipeService.wipe_sqlite()
    log.dual_log(
        tag="Startup:StagingWipe:SQLite",
        message=f"SQLite staging wipe complete: {len(sqlite_result)} tables",
        level="INFO",
        payload=sqlite_result,
    )

    # Wipe Snowflake staging tables (if CloudEngine is initialized)
    try:
        from database.backup.engine.cloud_engine import _global_cloud_engine
        if _global_cloud_engine and _global_cloud_engine.engine:
            sf_result = StagingWipeService.wipe_snowflake(_global_cloud_engine)
            log.dual_log(
                tag="Startup:StagingWipe:Snowflake",
                message=f"Snowflake staging wipe complete: {len(sf_result)} tables",
                level="INFO",
                payload=sf_result,
            )
    except Exception as e:
        log.dual_log(
            tag="Startup:StagingWipe:Snowflake:Failed",
            message=f"Snowflake staging wipe failed (non-blocking): {e}",
            level="WARNING",
            payload={"error": str(e)},
        )

async def _sync_from_backup_step() -> None:
    from config import DATABASE_STAGING_ENABLED
    if DATABASE_STAGING_ENABLED:
        from utils.logger import get_dual_logger
        log = get_dual_logger(__name__)
        log.dual_log(
            tag="Startup:SyncFromBackup:StagingSkip",
            message="Staging mode — skipping sync_from_backup",
            level="INFO",
        )
        return

    from utils.logger import get_dual_logger
    import asyncio
    log = get_dual_logger(__name__)
    
    global _global_sync_engine
    if _global_sync_engine is None:
        log.dual_log(tag="Startup:Sync:Skip", message="SyncEngine offline", level="WARNING", payload={"action": "skip_sync"})
        return
        
    try:
        log.dual_log(tag="Startup:Sync:Start", message="Starting smart startup sync", level="INFO", payload={"action": "start_sync"})
        decision = await asyncio.to_thread(_global_sync_engine.sync_startup)
        log.dual_log(
            tag="Startup:Sync:Complete",
            message=f"Startup sync completed: {decision.action}",
            level="INFO" if not decision.divergence_detected else "WARNING",
            payload={
                "action": decision.action,
                "reason": decision.reason,
                "divergence_detected": decision.divergence_detected,
                "hitl_required": decision.hitl_required,
                "hitl_outcome": decision.hitl_outcome,
                "duration_seconds": decision.duration_seconds,
                "local_proofs": decision.local_proofs,
                "cloud_proofs": decision.cloud_proofs,
            }
        )
    except Exception as e:
        log.dual_log(tag="Startup:Sync:Error", message=f"Startup sync failed: {e}", level="CRITICAL", payload={"error": str(e)})

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
    orchestrator.add_sequential("staging_wipe", _staging_wipe_step)
    orchestrator.add_sequential("run_db_migrations", run_db_migrations)
    orchestrator.add_sequential("validate_vec0", validate_vec0)
    orchestrator.add_sequential("init_backup", _init_backup_step)
    orchestrator.add_sequential("sync_from_backup", _sync_from_backup_step)
    orchestrator.add_sequential("startup_recovery", run_startup_recovery)

    # Tier 3: Application logic (Concurrent)
    orchestrator.add_concurrent_tier([
        ("load_tool_registry", load_tool_registry),
        ("warmup_browser", warmup_browser),
    ])

    await orchestrator.run(ctx)
    return ctx
