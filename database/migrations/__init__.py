# database/migrations/__init__.py

import importlib
import sqlite3
import time
import shutil
import sys
import re
from pathlib import Path
from typing import Protocol, List

from database.connection import DB_PATH, DatabaseManager, SQLITE_VEC_AVAILABLE
# We will dynamically access BASE_SCHEMA_VERSION from the reloaded module
# to avoid stale local values after auto-fold.
import database.schemas
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)
MIGRATIONS_DIR = Path(__file__).parent

class MigrationProtocol(Protocol):
    version: int
    description: str
    def up(self, conn: sqlite3.Connection, sqlite_vec_available: bool) -> None: ...


def perform_auto_fold() -> None:
    """Automatically fold the oldest migration into base schema if active migrations exceed limit.
    
    Critical safety notes:
    - Environment-agnostic source generation: Reconstructs VIRTUAL TABLE specs even on machines
      without sqlite-vec to prevent BLOB fallback DDL from being baked into source.
    - State accuracy: Does not merge with existing files to handle table deletions.
    - In-memory state refresh: Reloads schema modules after writing new files.
    - Non-recursive: Avoids recursion depth issues on failures.
    """
    log.dual_log(tag="DB:Migration:Autofold", message="Auto-fold: starting enforcement", level="WARNING")

    schemas_dir = MIGRATIONS_DIR.parent / "schemas"
    archive_dir = MIGRATIONS_DIR.parent / "migrations_archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    # Logical mapping of tables to schema module names
    module_map = {
        "jobs": ["jobs", "job_items", "job_logs", "broadcast_batches"],
        "finance": ["financial_metrics", "market_data_snapshots", "financial_formulas", "calculated_metrics", "raw_fundamentals", "stock_prices"],
        "vector": ["scraped_articles", "long_term_memories", "scraped_articles_vec", "long_term_memories_vec"],
        "pdf": ["pdf_parsed_pages", "pdf_parsed_pages_vec"],
        "token": ["token_usage"],
    }

    paths = sorted([p for p in MIGRATIONS_DIR.glob("v*.py") if not p.name.startswith("__")])
    if not paths:
        return

    oldest = paths[0]
    log.dual_log(tag="DB:Migration:Autofold", message=f"Auto-fold: folding oldest migration {oldest.name}", level="WARNING")

    mem_conn = sqlite3.connect(":memory:")
    try:
        # Load vec0 extension if available (best-effort)
        if SQLITE_VEC_AVAILABLE:
            try:
                import sqlite_vec  # type: ignore
                mem_conn.enable_load_extension(True)
                sqlite_vec.load(mem_conn)
            except Exception:
                pass

        # Initialize base schema in-memory
        try:
            from database.schemas import get_init_script
        except Exception as e:
            raise RuntimeError(f"Failed to import schema registry while auto-folding: {e}") from e

        base_script = get_init_script()
        mem_conn.executescript(base_script)

        # Import and apply the oldest migration module
        module_name = f"database.migrations.{oldest.stem}"
        if module_name in sys.modules:
            del sys.modules[module_name]
        mod = importlib.import_module(module_name)

        # Apply migration with current vec0 availability (SQLITE_VEC_AVAILABLE) to match runtime
        mod.up(mem_conn, SQLITE_VEC_AVAILABLE)

        # Collect DDL from the in-memory DB
        tables_by_module: dict[str, dict] = {k: {"tables": {}, "vec": {}} for k in module_map.keys()}

        for mod_name, table_list in module_map.items():
            for table in table_list:
                # Determine if this is logically a vector table regardless of current environment
                is_vector_table = table.endswith("_vec")

                row = mem_conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
                if row and row[0]:
                    create_sql = row[0].strip()
                    indices = ""

                    # Append any indexes associated with the table
                    idx_rows = mem_conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=?", (table,)).fetchall()
                    for idx in idx_rows:
                        if idx[0]:
                            indices += "\n" + idx[0].strip()

                    if is_vector_table:
                        # Reconstruct VIRTUAL TABLE spec
                        inner = "embedding float[1024]"
                        if "USING VEC0" in create_sql.upper():
                            match = re.search(r"USING VEC0\s*\((.+)\)", create_sql, re.DOTALL | re.IGNORECASE)
                            if match:
                                inner = match.group(1).strip()
                        
                        # Set create_sql to virtual definition and THEN append indices
                        create_sql = f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING vec0({inner});"
                        tables_by_module[mod_name]["vec"][table] = create_sql + indices
                    else:
                        tables_by_module[mod_name]["tables"][table] = create_sql + indices

        # Overwrite schema module files (do not merge, to handle deletions)
        for mod_name, contents in tables_by_module.items():
            # Render module source
            rendered = []
            rendered.append(f"# file: database/schemas/{mod_name}.py\n\n")
            rendered.append("TABLES = {\n")
            for tname, ddl in contents.get("tables", {}).items():
                rendered.append(f'    "{tname}": """{ddl}\n""",\n')
            rendered.append("}\n\n")
            rendered.append("VEC_TABLES = {\n")
            for tname, ddl in contents.get("vec", {}).items():
                rendered.append(f'    "{tname}": """{ddl}\n""",\n')
            rendered.append("}\n")

            target_path = schemas_dir / f"{mod_name}.py"
            target_path.write_text("".join(rendered), encoding="utf-8")
            log.dual_log(tag="DB:Migration:Autofold", message=f"Wrote updated schema module: {target_path}")

        # Move oldest migration to archive
        dest = archive_dir / oldest.name
        shutil.move(str(oldest), str(dest))
        log.dual_log(tag="DB:Migration:Autofold", message=f"Moved {oldest.name} -> {dest}")

        # Increment BASE_SCHEMA_VERSION
        init_path = schemas_dir / "__init__.py"
        txt = init_path.read_text(encoding='utf-8')
        m = re.search(r"BASE_SCHEMA_VERSION\s*=\s*(\d+)", txt)
        if m:
            old_base = int(m.group(1))
            new_base = old_base + 1
            txt = re.sub(r"BASE_SCHEMA_VERSION\s*=\s*\d+", f"BASE_SCHEMA_VERSION = {new_base}", txt)
            init_path.write_text(txt, encoding='utf-8')
            log.dual_log(tag="DB:Migration:Autofold", message=f"Bumped BASE_SCHEMA_VERSION {old_base} -> {new_base}")
        else:
            raise RuntimeError("Could not find BASE_SCHEMA_VERSION in schemas/__init__.py")

        # REFRESH RUNTIME STATE: Reload schema modules so current process sees changes
        try:
            import database.schemas
            importlib.reload(database.schemas)
            for name in module_map.keys():
                try:
                    submod = importlib.import_module(f"database.schemas.{name}")
                    importlib.reload(submod)
                except Exception:
                    pass
        except Exception as e:
            log.dual_log(tag="DB:Migration:Autofold", message=f"Module reload failed: {e}", level="WARNING")

        # Remove moved migration from sys.modules
        mod_name = f"database.migrations.{oldest.stem}"
        if mod_name in sys.modules:
            try:
                del sys.modules[mod_name]
            except Exception:
                pass

        importlib.invalidate_caches()

    except Exception as e:
        raise RuntimeError(f"Auto-fold failed: {e}") from e
    finally:
        try:
            mem_conn.close()
        except Exception:
            pass


def _discover_migrations() -> List[MigrationProtocol]:
    migrations = []
    for path in sorted(MIGRATIONS_DIR.glob("v*.py")):
        if path.name.startswith("__"):
            continue
        module_name = f"database.migrations.{path.stem}"
        try:
            # Ensure fresh import for discovery accuracy
            if module_name in sys.modules:
                del sys.modules[module_name]
            mod = importlib.import_module(module_name)
        except Exception as e:
            log.dual_log(tag="DB:Migration", message=f"Failed to import {path.stem}: {e}", level="ERROR", exc_info=e)
            raise RuntimeError(f"Migration import failed: {path.stem}") from e

        if not hasattr(mod, 'version') or not hasattr(mod, 'description') or not hasattr(mod, 'up'):
            raise RuntimeError(f"Migration {path.stem} violates MigrationProtocol")
        
        migrations.append(mod)

    migrations.sort(key=lambda m: m.version)

    # Validate sequential versioning
    # NOTE: We get BASE_SCHEMA_VERSION from the module to avoid stale local reference
    current_base = database.schemas.BASE_SCHEMA_VERSION
    for i, m in enumerate(migrations):
        expected = current_base + i + 1
        if m.version != expected:
            raise RuntimeError(f"Migration version gap: expected v{expected}, found v{m.version}")

    # Enforce strict 3-file limit — attempt automatic folding if exceeded
    if len(migrations) > database.schemas.MAX_MIGRATION_SCRIPTS:
        log.dual_log(tag="DB:Migration", message=f"Active migrations ({len(migrations)}) exceed max ({database.schemas.MAX_MIGRATION_SCRIPTS}). Attempting automatic fold.", level="WARNING")
        perform_auto_fold()
        # Invalidate again to ensure the deleted migration is not cached
        importlib.invalidate_caches()

        # Linear re-scan without recursion to prevent stack overflow and ensure fresh state
        # Re-import database.schemas to get the fresh BASE_SCHEMA_VERSION
        importlib.reload(database.schemas)
        migrations = []
        for path in sorted(MIGRATIONS_DIR.glob("v*.py")):
            if path.name.startswith("__"):
                continue
            module_name = f"database.migrations.{path.stem}"
            if module_name in sys.modules:
                del sys.modules[module_name]
            try:
                mod = importlib.import_module(module_name)
            except Exception as e:
                log.dual_log(tag="DB:Migration", message=f"Failed to import {path.stem} during re-scan: {e}", level="ERROR", exc_info=e)
                raise RuntimeError(f"Migration import failed after auto-fold: {path.stem}") from e
            migrations.append(mod)

        migrations.sort(key=lambda m: m.version)
        # Post-fold validation
        if len(migrations) > database.schemas.MAX_MIGRATION_SCRIPTS:
            raise RuntimeError("Auto-fold failed to reduce migration count below limit.")
        
        # Re-validate sequential versioning for safety using the fresh reloaded version
        fresh_base = database.schemas.BASE_SCHEMA_VERSION
        for i, m in enumerate(migrations):
            expected = fresh_base + i + 1
            if m.version != expected:
                raise RuntimeError(f"Migration version gap after auto-fold: expected v{expected}, found v{m.version}")

    return migrations


def get_latest_version() -> int:
    migrations = _discover_migrations()
    if not migrations:
        return database.schemas.BASE_SCHEMA_VERSION
    return migrations[-1].version


def _perform_destructive_reset(conn: sqlite3.Connection):
    log.dual_log(tag="DB:Migration", message="Performing destructive reset of all tables", level="WARNING")
    # Import locally to ensure fresh state
    from database.schemas import ALL_TABLES, ALL_VEC_TABLES
    for table in list(ALL_TABLES.keys()) + list(ALL_VEC_TABLES.keys()):
        try:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        except sqlite3.Error as e:
            log.dual_log(tag="DB:Migration", message=f"Error dropping table {table}: {e}", level="WARNING")
    conn.execute("PRAGMA user_version = 0")
    log.dual_log(tag="DB:Migration", message="Destructive reset completed", level="INFO")


def _verify_fk_violations(conn: sqlite3.Connection) -> List:
    return conn.execute("PRAGMA foreign_key_check").fetchall()


def run_migrations(conn: sqlite3.Connection) -> None:
    migrations = _discover_migrations()
    
    try:
        current_v = conn.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.DatabaseError:
        current_v = 0

    BASE_SCHEMA_VERSION = database.schemas.BASE_SCHEMA_VERSION
    if 0 < current_v < BASE_SCHEMA_VERSION:
        log.dual_log(tag="DB:Migration", message=f"Stranded database detected (v{current_v} < base v{BASE_SCHEMA_VERSION}). Performing destructive reset.", level="CRITICAL")
        _perform_destructive_reset(conn)
        current_v = 0

    if not migrations:
        if current_v < BASE_SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version = {BASE_SCHEMA_VERSION}")
            conn.commit()
        return

    target_v = migrations[-1].version
    pending = [m for m in migrations if m.version > current_v]
    if not pending:
        if current_v < BASE_SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version = {BASE_SCHEMA_VERSION}")
            conn.commit()
        return

    backup_path = DB_PATH.with_suffix(".db.bak")
    try:
        log.dual_log(tag="DB:Migration", message="Checkpointing WAL before backup")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        log.dual_log(tag="DB:Migration", message=f"Creating pre-migration backup at {backup_path}")
        shutil.copy2(DB_PATH, backup_path)
        if not backup_path.exists():
            raise RuntimeError("Backup file was not created")
        log.dual_log(tag="DB:Migration", message="Pre-migration backup created successfully")
    except Exception as e:
        log.dual_log(tag="DB:Migration", message=f"Failed to create pre-migration backup: {e}", level="CRITICAL")
        sys.exit(1)

    conn.isolation_level = None
    migration_success = False
    try:
        log.dual_log(tag="DB:Migration", message="Disabling foreign key constraints (pre-transaction)")
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
        except Exception:
            pass

        log.dual_log(tag="DB:Migration", message="Acquiring EXCLUSIVE lock")
        conn.execute("BEGIN EXCLUSIVE")

        for migration in pending:
            start = time.monotonic()
            log.dual_log(tag="DB:Migration", message=f"Applying migration v{migration.version}: {migration.description}")
            try:
                migration.up(conn, SQLITE_VEC_AVAILABLE)
            except Exception as e:
                raise RuntimeError(f"Migration script failed: {e}")

            log.dual_log(tag="DB:Migration", message="Validating foreign key constraints")
            fk_violations = _verify_fk_violations(conn)
            if fk_violations:
                raise RuntimeError(f"Foreign key constraint violation after v{migration.version}: {fk_violations}")

            conn.execute(f"PRAGMA user_version = {migration.version}")
            elapsed = time.monotonic() - start
            log.dual_log(tag="DB:Migration", message=f"Migration v{migration.version} applied successfully in {elapsed:.3f}s")

        conn.execute("COMMIT")
        migration_success = True
        log.dual_log(tag="DB:Migration", message=f"All migrations applied. Schema version: {target_v}")

    except Exception as e:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        log.dual_log(tag="DB:Migration", message=f"Migration failed: {e}. Restoring backup and terminating.", level="CRITICAL", exc_info=e)
        try:
            conn.close()
        except Exception:
            pass
        try:
            log.dual_log(tag="DB:Migration", message=f"Restoring database from {backup_path}")
            shutil.copy2(backup_path, DB_PATH)
            for suffix in ["-wal", "-shm"]:
                try:
                    sidecar = DB_PATH.with_name(DB_PATH.name + suffix)
                    if sidecar.exists():
                        sidecar.unlink()
                except Exception:
                    pass
            log.dual_log(tag="DB:Migration", message="Database restored successfully", level="WARNING")
        except Exception as restore_err:
            log.dual_log(tag="DB:Migration", message=f"FATAL: Failed to restore backup: {restore_err}", level="CRITICAL")
        sys.exit(1)
    finally:
        try:
            conn.execute("PRAGMA foreign_keys = ON")
        except Exception:
            pass
        conn.isolation_level = ""
        if migration_success and backup_path.exists():
            try:
                backup_path.unlink()
                log.dual_log(tag="DB:Migration", message="Temporary backup cleaned up")
            except OSError:
                log.dual_log(tag="DB:Migration", message="Could not remove temporary backup", level="WARNING")
