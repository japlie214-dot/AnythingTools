# utils/startup/__init__.py

from .core import StartupContext, StartupOrchestrator
from .cleanup import cleanup_zombie_chrome, cleanup_temp_files
from .server import get_mount_artifacts_step
from .database import init_database_layer, run_db_migrations, validate_vec0
from .registry import load_tool_registry
from .browser import warmup_browser
from .telegram import start_telegram_handshake

async def run_startup(app_instance=None) -> StartupContext:
    ctx = StartupContext()
    orchestrator = StartupOrchestrator()

    # Tier 1: Independent setup/cleanup (Concurrent)
    orchestrator.add_concurrent_tier([
        ("mount_artifacts", get_mount_artifacts_step(app_instance)),
        ("cleanup_zombie_chrome", cleanup_zombie_chrome),
        ("cleanup_temp_files", cleanup_temp_files),
        ("init_database_layer", init_database_layer),
        ("start_telegram_handshake", start_telegram_handshake),
    ])

    # Tier 2: Dependent Database logic (Sequential)
    orchestrator.add_sequential("run_db_migrations", run_db_migrations)
    orchestrator.add_sequential("validate_vec0", validate_vec0)

    # Tier 3: Application logic (Concurrent)
    orchestrator.add_concurrent_tier([
        ("load_tool_registry", load_tool_registry),
        ("warmup_browser", warmup_browser),
    ])

    await orchestrator.run(ctx)
    return ctx
