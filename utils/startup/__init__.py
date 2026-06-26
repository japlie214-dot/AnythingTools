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

    Runs BEFORE _init_backup_step (which calls CloudEngine.startup() →
    reconcile_types() → rebuild_table(), the last of which REPOPULATES
    staging tables from SQLite). Wiping AFTER _init_backup_step would
    TRUNCATE the freshly-repopulated rows — destructive. The wipe MUST
    stay before _init_backup_step.

    The Snowflake wipe uses a discrete, locally-constructed CloudEngine
    (disposed after the wipe) because the global _global_sync_engine is
    not yet initialized at this point in the startup sequence. Constructing
    a fresh CloudEngine from BackupSettings() is the idiomatic way to
    obtain a short-lived Snowflake connection pool.
    Ref: https://docs.snowflake.com/en/developer-guide/python-connector/python-connector-connect

    Best-effort: failures are logged WARNING, never block startup.
    """
    from config import DATABASE_STAGING_ENABLED, DATABASE_STAGING_WIPE_ON_STARTUP
    from utils.logger import get_dual_logger
    log = get_dual_logger(__name__)

    if not DATABASE_STAGING_ENABLED or not DATABASE_STAGING_WIPE_ON_STARTUP:
        return

    from database.backup.staging import StagingWipeService

    # ─── SQLite wipe ───────────────────────────────────────────────────
    # SQLite wipe runs first. The connection is staging-aware (diverts to
    # data/staging/sumanal.db when DATABASE_STAGING_ENABLED=true) via
    # DatabaseManager.create_write_connection() → _resolve_db_path().
    sqlite_result = StagingWipeService.wipe_sqlite()
    log.dual_log(
        tag="Startup:StagingWipe:SQLite",
        message=f"SQLite staging wipe complete: {len(sqlite_result)} tables",
        level="INFO",
        payload=sqlite_result,
    )

    # ─── Snowflake wipe ────────────────────────────────────────────────
    # Construct a discrete CloudEngine locally. The global _global_sync_engine
    # is NOT yet initialized (this step runs before _init_backup_step).
    # The local engine is disposed after the wipe to release the Snowflake
    # connection pool. _init_backup_step will construct its own CloudEngine
    # later — acceptable startup cost.
    #
    # We construct via BackupSettings() (not CloudBackupSettings directly)
    # because BackupSettings reads env vars with the BACKUP_ prefix and
    # composes the cloud + sync settings the CloudEngine constructor expects.
    # Ref: database/backup/settings.py:40-56 (BackupSettings composes cloud + sync)
    # Ref: database/backup/engine/cloud_engine.py:31-33 (CloudEngine.__init__ takes cloud + cb_settings)
    try:
        from config import DATABASE_INTEGRATION_ENABLED
        # Simplified from: not getattr(DATABASE_INTEGRATION_ENABLED, "__bool__", lambda: True)() or not DATABASE_INTEGRATION_ENABLED
        # The original expression reduced to `not bool(x) or not x` == `not x` — inert complexity.
        # getattr(x, "__bool__") does NOT raise AttributeError (it returns a bound method-wrapper);
        # the refactor is justified purely on complexity removal.
        # Ref: https://docs.python.org/3/reference/datamodel.html#special-method-lookup
        if not DATABASE_INTEGRATION_ENABLED:
            # Cloud integration disabled — skip Snowflake wipe.
            log.dual_log(
                tag="Startup:StagingWipe:Snowflake:Skipped",
                message="DATABASE_INTEGRATION_ENABLED=false — skipping Snowflake staging wipe",
                level="INFO",
                payload={"action": "skip", "reason": "database_integration_disabled"},
            )
        else:
            from database.backup.settings import BackupSettings
            from database.backup.engine.cloud_engine import CloudEngine
            settings = BackupSettings()
            if not settings.cloud.enabled:
                log.dual_log(
                    tag="Startup:StagingWipe:Snowflake:Skipped",
                    message="BACKUP_CLOUD_ENABLED=false — skipping Snowflake staging wipe",
                    level="INFO",
                    payload={"action": "skip", "reason": "cloud_disabled"},
                )
            else:
                # Construct a short-lived CloudEngine. The constructor
                # initializes the SQLAlchemy engine pool, loads the private
                # key, and registers session-recovery listeners — same as
                # the global one constructed later in _init_backup_step.
                cloud_engine = CloudEngine(settings.cloud, settings.sync)
                try:
                    if cloud_engine.engine is not None:
                        sf_result = StagingWipeService.wipe_snowflake(cloud_engine)
                        log.dual_log(
                            tag="Startup:StagingWipe:Snowflake",
                            message=f"Snowflake staging wipe complete: {len(sf_result)} tables",
                            level="INFO",
                            payload=sf_result,
                        )
                finally:
                    # Release the Snowflake connection pool. _init_backup_step
                    # will construct its own CloudEngine; this one is not reused.
                    cloud_engine.shutdown()
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
            # FATAL FIX: dual_log requires a non-empty dict payload (utils/logger/core.py:54-55).
            # Without this, DATABASE_STAGING_ENABLED=true crashes startup with TypeError here
            # (this call is NOT inside a try/except — it runs before the try block at line 190).
            payload={"action": "skip", "reason": "staging_mode"},
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
