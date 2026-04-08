# utils/logger/core.py
import asyncio
import hashlib
import json
import logging
import threading
# timedelta is REQUIRED for the debounce comparison in dual_log().
# The original monolith imported it at the top level; omitting it here
# causes NameError at the first WARNING/ERROR/CRITICAL dispatch.
from datetime import datetime, timezone, timedelta
from typing import Any

from utils.id_generator import ULID
from utils.logger.formatters import _serialize_payload
from utils.logger.handlers import (
    _cache_lock,
    _get_master_handlers,
    _get_specialized_handler,
    _handler_cache,
    _normalize_exc_info,
)
from utils.logger.routing import LOG_MAP, _LOG_DIR
from utils.logger.state import (
    _debounce_dict,
    _debounce_lock,
    _debugger_log_buffer,
    _log_config,
    _tool_log_buffer,
    _current_job_id,  # imported for dual_log state sync
)

# Module-alias import for _debugger_main_loop specifically.
# This variable requires rebinding (not in-place mutation), so it must be
# accessed via the module object: `_state_mod._debugger_main_loop = loop`.
# A direct `from utils.logger.state import _debugger_main_loop` would give a
# local alias pointing at the original None; subsequent rebinding inside this
# module would be invisible to all other readers.
import utils.logger.state as _state_mod

_logger_cache: dict[str, "SumAnalLogger"] = {}


class SumAnalLogger:
    """Dual-stream logger: console + master file, optional specialized routing."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._logger = logging.getLogger(f"sumanal.{name}")
        self._logger.setLevel(logging.DEBUG)
        self._logger.propagate = False
        console_h, file_h = _get_master_handlers()
        if console_h not in self._logger.handlers:
            self._logger.addHandler(console_h)
        if file_h not in self._logger.handlers:
            self._logger.addHandler(file_h)

    def dual_log(
        self,
        tag: str,
        message: str,
        level: str = "INFO",
        payload: Any = None,
        destination: str | None = None,
        exc_info: Exception | bool | tuple | None = None,
        status_state: str | None = None,
        notify_user: bool = False,
    ) -> None:
        level_int = getattr(logging, level.upper(), logging.INFO)
        event_id = ULID.generate()
        extra = {"tag": tag, "payload": payload, "event_id": event_id}

        # 1. Console + master file via composed logger.
        self._logger.log(level_int, message, extra=extra, exc_info=exc_info)

        # ── Logger Agent buffer capture ──────────────────────────────────────
        _buf = _tool_log_buffer.get()
        if _buf is not None:
            _buf_payload = _serialize_payload(payload)
            if _buf_payload is not None:
                _buf_payload_str = json.dumps(_buf_payload, ensure_ascii=False, default=str)
                _max_ctx = (
                    getattr(_log_config, 'LOGGER_AGENT_MAX_CONTEXT', 100_000)
                    if _log_config else 100_000
                )
                if len(_buf_payload_str) > _max_ctx:
                    _marker = f"...[ENTRY TRUNCATED: original {len(_buf_payload_str)} chars]"
                    _avail = _max_ctx - len(_marker)
                    _buf_payload = (_buf_payload_str[:_avail] + _marker) if _avail > 0 else _marker
            _buf.append({
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "level":     level.upper(),
                "tag":       tag,
                "message":   message,
                "payload":   _buf_payload,
                "status_state": status_state,  # carried for DB flush later
            })

        # 2. Specialized file via direct emit (no handler-list mutation).
        if destination:
            sp_handler = _get_specialized_handler(destination)
            if sp_handler:
                _exc = _normalize_exc_info(exc_info)
                rec = logging.LogRecord(
                    name=self._logger.name,
                    level=level_int,
                    pathname="",
                    lineno=0,
                    msg=message,
                    args=(),
                    exc_info=_exc,
                )
                rec.tag = tag
                rec.payload = payload
                rec.event_id = event_id
                try:
                    sp_handler.emit(rec)
                except Exception:
                    pass

        # ── AnythingTools state sync & notifications ────────────────────────
        # Immediate DB flush and job status + optional user notification.
        job_id = _current_job_id.get()
        if job_id:
            payload_str = None
            if payload is not None:
                try:
                    payload_str = json.dumps(_serialize_payload(payload), ensure_ascii=False, default=str)
                except Exception:
                    payload_str = None

            log_id = ULID.generate()
            ts = datetime.now(timezone.utc).isoformat()

            from database.writer import enqueue_write

            enqueue_write(
                "INSERT INTO job_logs (id, job_id, tag, level, status_state, message, payload_json, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (log_id, job_id, tag, level.upper(), status_state, message, payload_str, ts),
            )

            if status_state:
                enqueue_write(
                    "UPDATE jobs SET status = ?, updated_at = ? WHERE job_id = ?",
                    (status_state, ts, job_id),
                )

            if notify_user:
                try:
                    from api.telegram_notifier import send_notification
                    formatted = f"[{status_state or level.upper()}] {tag} — {message}"
                    try:
                        _loop = asyncio.get_running_loop()
                        _loop.create_task(send_notification(formatted))
                    except RuntimeError:
                        # Fallback spawn in a small daemon thread
                        threading.Thread(
                            target=lambda: asyncio.run(send_notification(formatted)),
                            name=f"Notifier-{job_id}",
                            daemon=True,
                        ).start()
                except Exception:
                    # Do not break logging if notifier fails
                    pass

        # ── Debugger Agent: Step 1 — Unconditional buffer append ─────────────
        _debugger_log_buffer.append({
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level":     level.upper(),
            "tag":       tag,
            "message":   message,
            "payload":   _serialize_payload(payload),
        })

        # ── Step 2 — Level gate ───────────────────────────────────────────────
        if level_int < logging.WARNING:
            return

        # ── Step 3 — Infinite Loop Guard ──────────────────────────────────────
        if tag.startswith("Debugger:"):
            return

        # ── Step 4 — Warning toggle ───────────────────────────────────────────
        _trigger_on_warn = (
            getattr(_log_config, "DEBUGGER_AGENT_TRIGGER_ON_WARNING", True)
            if _log_config else True
        )
        if level_int == logging.WARNING and not _trigger_on_warn:
            return

        # ── Step 5 — Atomic debounce ──────────────────────────────────────────
        _debounce_key = hashlib.sha256(f"{tag}::{message}".encode("utf-8")).hexdigest()
        _now = datetime.now(timezone.utc)
        with _debounce_lock:
            _last = _debounce_dict.get(_debounce_key)
            if _last and (_now - _last) < timedelta(minutes=3):
                return
            _debounce_dict[_debounce_key] = _now

        # ── Step 6 — Thread-safe snapshot ────────────────────────────────────
        while True:
            try:
                _snapshot = list(_debugger_log_buffer)
                break
            except RuntimeError:
                pass

        # ── Step 7 — Three-tier dispatch ──────────────────────────────────────
        # Deferred import breaks the core ↔ debugger_agent circular dependency
        # at module load time. sys.modules caches it after the first call.
        from utils.debugger_agent import run_debugger_agent  # noqa: PLC0415

        # Tier 1 — caller is on the event-loop thread.
        try:
            _loop = asyncio.get_running_loop()
            if _state_mod._debugger_main_loop is None:
                _state_mod._debugger_main_loop = _loop  # lazily capture for Tier 2
            _loop.create_task(run_debugger_agent(tag, _snapshot))
            return
        except RuntimeError:
            pass  # not in a running loop on this thread

        # Tier 2 — caller is a background thread; main loop is alive elsewhere.
        if (
            _state_mod._debugger_main_loop is not None
            and _state_mod._debugger_main_loop.is_running()
        ):
            try:
                asyncio.run_coroutine_threadsafe(
                    run_debugger_agent(tag, _snapshot),
                    _state_mod._debugger_main_loop,
                )
                return
            except Exception:
                pass  # loop stopped between check and call

        # Tier 3 — no usable loop reference; spin up an isolated daemon thread.
        def _fallback_runner() -> None:
            asyncio.run(run_debugger_agent(tag, _snapshot))

        threading.Thread(target=_fallback_runner, daemon=True).start()


# ── Public API ───────────────────────────────────────────────────────────────


def flush_tool_buffer_to_job_logs(job_id: str, buf: list[dict] | None) -> None:
    """Flush the in-memory tool log buffer into the persistent job_logs table.

    This function enqueues one INSERT per buffered entry using the single-writer
    queue (enqueue_write) so all DB writes remain serialized and WAL-safe.

    Entries that already had an immediate DB insert (marked by _persisted=True)
    are skipped to avoid duplicates.
    """
    if not job_id or not buf:
        return
    try:
        # local import to avoid circular import at module import time
        from database.writer import enqueue_write
    except Exception as e:
        try:
            get_dual_logger(__name__).dual_log(
                tag="Logger:Flush", message=f"enqueue_write import failed: {e}", level="ERROR", exc_info=e
            )
        except Exception:
            pass
        return

    for entry in buf:
        # Skip entries that were already persisted immediately to job_logs.
        if entry.get("_persisted") is True:
            continue
        try:
            row_id = ULID.generate()
            timestamp = entry.get("timestamp")
            tag = entry.get("tag")
            level = entry.get("level")
            status_state = entry.get("status_state") or entry.get("status") if entry.get("status") else None
            message = entry.get("message")
            payload_obj = entry.get("payload")
            payload_json = None
            if payload_obj is not None:
                try:
                    payload_json = json.dumps(payload_obj, ensure_ascii=False, default=str)
                except Exception:
                    try:
                        payload_json = json.dumps(str(payload_obj), ensure_ascii=False)
                    except Exception:
                        payload_json = None

            enqueue_write(
                "INSERT INTO job_logs (id, job_id, tag, level, status_state, message, payload_json, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (row_id, job_id, tag, level, status_state, message, payload_json, timestamp),
            )
        except Exception as e:
            try:
                get_dual_logger(__name__).dual_log(tag="Logger:Flush", message=f"Failed to enqueue job_log: {e}", level="ERROR", exc_info=e)
            except Exception:
                pass


def get_dual_logger(name: str) -> SumAnalLogger:
    """Cached factory. Usage: log = get_dual_logger(__name__)"""
    with _cache_lock:
        if name not in _logger_cache:
            _logger_cache[name] = SumAnalLogger(name)
        return _logger_cache[name]


def clear_sql_log(statement_type: str) -> None:
    """Close cached handler, remove from cache, truncate the specialized file."""
    if statement_type not in LOG_MAP:
        return
    filename = LOG_MAP[statement_type]
    with _cache_lock:
        if filename in _handler_cache:
            handler = _handler_cache[filename]
            handler.acquire()
            try:
                handler.close()
            finally:
                handler.release()
            del _handler_cache[filename]
        try:
            open(_LOG_DIR / filename, "w").close()
        except OSError:
            pass


def get_sql_logger(statement_type: str) -> SumAnalLogger:
    """Convenience wrapper returning a SumAnalLogger named for *statement_type*."""
    return get_dual_logger(f"sql.{statement_type.replace(' ', '_')}")


def global_log_purge() -> None:
    """Archive existing logs, close all handlers, and prepare a fresh logging state."""
    import shutil
    import sys

    with _cache_lock:
        for handler in _handler_cache.values():
            try:
                handler.close()
            except Exception:
                pass
        _handler_cache.clear()
        _logger_cache.clear()

        for name in list(logging.root.manager.loggerDict):
            if name.startswith("sumanal."):
                lg = logging.root.manager.loggerDict[name]
                if isinstance(lg, logging.Logger):
                    for h in lg.handlers[:]:
                        lg.removeHandler(h)

        if _LOG_DIR.exists():
            log_files = [f for f in _LOG_DIR.iterdir() if f.is_file()]
            if log_files:
                ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
                archive_dir = _LOG_DIR / "archive" / ts
                try:
                    archive_dir.mkdir(parents=True, exist_ok=True)
                    for f in log_files:
                        try:
                            shutil.move(str(f), str(archive_dir / f.name))
                        except Exception as e:
                            sys.stderr.write(f"Warning: could not archive {f.name}: {e}\n")
                except Exception as e:
                    sys.stderr.write(f"Warning: archive directory creation failed: {e}\n")

        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        console_h, file_h = _get_master_handlers()
        for name in list(logging.root.manager.loggerDict):
            if name.startswith("sumanal."):
                lg = logging.root.manager.loggerDict[name]
                if isinstance(lg, logging.Logger):
                    lg.addHandler(console_h)
                    lg.addHandler(file_h)


def flush_all_log_handlers() -> None:
    """Force-flush every cached file handler to disk."""
    with _cache_lock:
        for handler in _handler_cache.values():
            try:
                handler.flush()
            except Exception:
                pass
