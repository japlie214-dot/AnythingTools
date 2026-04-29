# utils/logger/core.py
import json
import logging
from typing import Any
from datetime import datetime, timezone

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
    _log_config,
    _tool_log_buffer,
    _current_job_id,  # imported for dual_log state sync
)

_logger_cache: dict[str, "SumAnalLogger"] = {}


class SumAnalLogger:
    """Dual-stream logger: console + master file, optional specialized routing."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._logger = logging.getLogger(f"sumanal.{name}")
        self._logger.setLevel(logging.DEBUG)
        self._logger.propagate = False
        console_h = _get_master_handlers()[0]
        if console_h not in self._logger.handlers:
            self._logger.addHandler(console_h)

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
        
        if level_int >= logging.ERROR:
            try:
                from utils.error_export import export_error_context
                export_error_context(tag, message, _current_job_id.get())
            except Exception:
                pass

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

            # Route logs to logs.db instead of main database
            from database.logs_writer import logs_enqueue_write

            logs_enqueue_write(
                "INSERT INTO logs (id, job_id, tag, level, status_state, message, payload_json, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (log_id, job_id, tag, level.upper(), status_state, message, payload_str, ts),
            )

            if status_state:
                from database.writer import enqueue_write
                enqueue_write(
                    "UPDATE jobs SET status = ?, updated_at = ? WHERE job_id = ?",
                    (status_state, ts, job_id),
                )

            if notify_user:
                pass # Telegram notifier removed for pure tool-hosting environment.


# ── Public API ───────────────────────────────────────────────────────────────


def flush_tool_buffer_to_job_logs(job_id: str, buf: list[dict] | None) -> None:
    """Flush the in-memory tool log buffer into the persistent logs.db.

    This function enqueues one INSERT per buffered entry using the single-writer
    queue (logs_enqueue_write) so all DB writes remain serialized and WAL-safe.

    Entries that already had an immediate DB insert (marked by _persisted=True)
    are skipped to avoid duplicates.
    """
    if not job_id or not buf:
        return

    # Import here to avoid circular import at module level
    from database.logs_writer import logs_enqueue_write

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

            logs_enqueue_write(
                "INSERT INTO logs (id, job_id, tag, level, status_state, message, payload_json, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
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
    """Archiving removed. Only ensure log directory exists and console handler is configured."""
    with _cache_lock:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        console_h = _get_master_handlers()[0]
        for name in list(logging.root.manager.loggerDict):
            if name.startswith("sumanal."):
                lg = logging.root.manager.loggerDict[name]
                if isinstance(lg, logging.Logger):
                    lg.addHandler(console_h)


def flush_all_log_handlers() -> None:
    """Force-flush every cached file handler to disk."""
    with _cache_lock:
        for handler in _handler_cache.values():
            try:
                handler.flush()
            except Exception:
                pass
