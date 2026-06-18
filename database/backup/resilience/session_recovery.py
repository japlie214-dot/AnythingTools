# database/backup/resilience/session_recovery.py
"""Snowflake session-recovery primitives for the SQLAlchemy-based CloudEngine.

This module implements two complementary defenses against Snowflake's
390111 "Session no longer exists" error:

1. A SQLAlchemy ``DialectEvents.handle_error`` listener that recognises
   390111 (and the equivalent SQLSTATE 08003) as a disconnect condition,
   forcing SQLAlchemy to invalidate the connection pool. This means the
   next ``engine.begin()`` will draw a fresh connection instead of
   re-using the poisoned one.

2. A retry decorator that catches ``ProgrammingError`` raised by the
   wrapped callable, disposes the engine pool, and re-invokes the
   callable once. This handles the case where 390111 is raised
   mid-statement (which ``pool_pre_ping`` cannot prevent).

Official documentation grounding:
- Snowflake error 390111 and ProgrammingError attributes (errno, sqlstate, msg, sfqid):
  https://docs.snowflake.com/en/developer-guide/python-connector/python-connector-example
  (Section: "Handling errors" — example pattern shows e.errno, e.sqlstate, e.msg, e.sfqid)
- Snowflake session idle timeout (default 4 hours for programmatic sessions):
  https://docs.snowflake.com/en/user-guide/session-policies
- SQLAlchemy DialectEvents.handle_error event:
  https://docs.sqlalchemy.org/en/20/core/events.html#sqlalchemy.events.DialectEvents.handle_error
  Quote: "Use cases supported by this hook include: Establishing whether a DBAPI
  connection error message indicates that the database connection needs to be
  reconnected, including for the 'pre_ping' handler used by some dialects."
- SQLAlchemy Engine.dispose() (closes idle connections, preserves engine + listeners):
  https://docs.sqlalchemy.org/en/20/core/connections.html#sqlalchemy.engine.Engine.dispose
  Quote: "A new connection pool is created immediately after the old one has been
  disposed... The Engine is intended to normally be a permanent fixture."
- SQLAlchemy retry recipe (FAQ recommends decorator-based retry):
  https://docs.sqlalchemy.org/en/20/faq/connections.html#how-do-i-retry-a-statement-execution-automatically
  Quote: "The canonical approach to dealing with mid-operation disconnects is to
  retry the entire operation from the start of the transaction, often by using a
  custom Python decorator that will 'retry' a particular function several times."
- SQLAlchemy pool_pre_ping limitation (does NOT protect mid-transaction):
  https://docs.sqlalchemy.org/en/20/core/pooling.html#sqlalchemy.create_engine.params.pool_pre_ping
  Quote: "It is critical to note that the pre-ping approach does not accommodate
  for connections dropped in the middle of transactions or other SQL operations."
"""
from __future__ import annotations

import functools
import threading
from typing import Any, Callable, TypeVar

from sqlalchemy import event
from sqlalchemy.engine import Engine

T = TypeVar("T")

# Snowflake error codes that indicate the session is gone and a fresh
# connection is required.
#   390111 — canonical "Session no longer exists. New login required to
#            access this service." (per Snowflake Python Connector docs)
#   390114 — "Authentication token has expired" variant
#   390188 — token refresh failed (observed in some OAuth flows)
SESSION_EXPIRED_ERRNOS = {390111, 390114, 390188}

# ANSI SQLSTATE codes for connection-related failures.
#   08003 — "connection does not exist" (the SQLSTATE that accompanies 390111)
#   08006 — "connection failure"
# Reference: https://en.wikipedia.org/wiki/SQLSTATE
SESSION_EXPIRED_SQLSTATES = {"08003", "08006"}

# Thread-local guard to prevent nested retries (if the retry itself
# raises 390111 during reconnect, we must not recurse infinitely).
_retry_guard = threading.local()


def _is_session_expired(exc: BaseException) -> bool:
    """Inspect an exception and decide whether it represents a Snowflake
    session-gone condition that warrants a reconnect.

    Per the Snowflake Python Connector API documentation:
    https://docs.snowflake.com/en/developer-guide/python-connector/python-connector-api
    "All exception classes defined by the Python database API standard.
    The Snowflake Connector for Python provides the attributes msg, errno,
    sqlstate, sfqid and raw_msg."

    We check three signals (errno, sqlstate, message substring) for defense
    in depth against Snowflake version drift.
    """
    # Check errno first (most reliable).
    errno = getattr(exc, "errno", None)
    if errno in SESSION_EXPIRED_ERRNOS:
        return True
    # Check sqlstate (ANSI standard).
    sqlstate = getattr(exc, "sqlstate", None)
    if sqlstate in SESSION_EXPIRED_SQLSTATES:
        return True
    # Defensive fallback: scan the message string. The runtime message
    # format is "390111 (08003): Session no longer exists. New login
    # required to access this service." so either substring is a strong
    # signal.
    msg = str(exc)
    if "390111" in msg or "Session no longer exists" in msg:
        return True
    return False


def register_session_recovery(engine: Engine, log) -> None:
    """Attach a ``handle_error`` listener that marks Snowflake 390111
    as a disconnect condition, so SQLAlchemy invalidates the pool.

    Per SQLAlchemy docs:
    https://docs.sqlalchemy.org/en/20/core/events.html#sqlalchemy.events.DialectEvents.handle_error

    Setting ``context.is_disconnect = True`` causes SQLAlchemy to
    invalidate the connection and recreate the pool on the next
    checkout. The current operation is still lost — the retry decorator
    ``with_session_recovery`` handles re-running it.

    This listener also runs during pre-ping operations (per the same
    docs, "Changed in version 2.0: the DialectEvents.handle_error() event
    is moved to the DialectEvents class... so that it may also participate
    in the 'pre ping' operation").
    """
    @event.listens_for(engine, "handle_error")
    def _on_handle_error(context) -> None:
        # context.original_exception is the DBAPI-level exception raised
        # by the snowflake-connector-python layer.
        exc = context.original_exception
        if _is_session_expired(exc):
            # Mark as disconnect so SQLAlchemy invalidates the pool.
            # The current operation is still lost — the retry decorator
            # outside the engine layer handles re-running it.
            context.is_disconnect = True
            try:
                log.dual_log(
                    tag="Backup:Cloud:SessionInvalidated",
                    message="Snowflake session-gone error detected; pool will be invalidated.",
                    level="WARNING",
                    payload={
                        "errno": getattr(exc, "errno", None),
                        "sqlstate": getattr(exc, "sqlstate", None),
                        "sfqid": getattr(exc, "sfqid", None),
                        "is_pre_ping": getattr(context, "is_pre_ping", False),
                    },
                )
            except Exception:
                # Logging must never crash the error-handling path.
                pass


def with_session_recovery(
    fn: Callable[..., T],
    *,
    engine: Engine,
    log,
    tag: str,
    max_retries: int = 1,
) -> Callable[..., T]:
    """Decorator that retries ``fn`` on Snowflake 390111.

    On a session-expired error:
    1. Log the recovery attempt with structured fields.
    2. Call ``engine.dispose()`` to drop the poisoned pool. Per the
       SQLAlchemy docs:
       https://docs.sqlalchemy.org/en/20/core/connections.html#sqlalchemy.engine.Engine.dispose
       "A new connection pool is created immediately after the old one
       has been disposed. The previous connection pool is disposed either
       actively, by closing out all currently checked-in connections in
       that pool, or passively... The Engine and event listeners are
       preserved."
    3. Re-invoke ``fn``. The next ``engine.begin()`` will draw from the
       fresh (empty) pool, triggering a new Snowflake login.

    If the retry also fails, the original exception is re-raised so
    the existing circuit breaker (in CloudEngine.circuit_breaker_push)
    can record the failure and eventually OPEN to prevent cascade.

    The SQLAlchemy FAQ explicitly recommends this decorator-based retry
    pattern for mid-operation disconnects:
    https://docs.sqlalchemy.org/en/20/faq/connections.html#how-do-i-retry-a-statement-execution-automatically
    """
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> T:
        # Guard against nested retries: if we're already inside a retry
        # attempt and 390111 fires again, don't recurse — let the
        # exception propagate to the circuit breaker.
        if getattr(_retry_guard, "in_retry", False):
            return fn(*args, **kwargs)

        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if not _is_session_expired(exc):
                # Not a session-gone error — propagate unchanged so the
                # circuit breaker sees the real failure (e.g. 100090
                # duplicate-row MERGE error must NOT be retried).
                raise
            if max_retries < 1:
                raise

            try:
                log.dual_log(
                    tag="Backup:Cloud:SessionRecovering",
                    message=(
                        f"Snowflake session expired during '{tag}'. "
                        f"Disposing engine pool and retrying (max_retries={max_retries})."
                    ),
                    level="WARNING",
                    payload={
                        "tag": tag,
                        "errno": getattr(exc, "errno", None),
                        "sqlstate": getattr(exc, "sqlstate", None),
                        "max_retries": max_retries,
                    },
                )
            except Exception:
                pass

            # Drop the poisoned pool. Per SQLAlchemy docs, this is safe
            # to call from any thread; checked-out connections are
            # orphaned and will be GC'd. Event listeners are preserved
            # on the new pool.
            try:
                engine.dispose()
            except Exception as dispose_exc:
                try:
                    log.dual_log(
                        tag="Backup:Cloud:DisposeFailed",
                        message=f"engine.dispose() failed during recovery: {dispose_exc}",
                        level="ERROR",
                        payload={"tag": tag, "error": str(dispose_exc)},
                    )
                except Exception:
                    pass

            _retry_guard.in_retry = True
            try:
                result = fn(*args, **kwargs)
            except Exception as retry_exc:
                if _is_session_expired(retry_exc):
                    try:
                        log.dual_log(
                            tag="Backup:Cloud:SessionRecoveryFailed",
                            message=(
                                f"Snowflake session still expired after retry "
                                f"during '{tag}'. Surfacing error to circuit breaker."
                            ),
                            level="ERROR",
                            payload={
                                "tag": tag,
                                "errno": getattr(retry_exc, "errno", None),
                                "sqlstate": getattr(retry_exc, "sqlstate", None),
                            },
                        )
                    except Exception:
                        pass
                raise
            else:
                try:
                    log.dual_log(
                        tag="Backup:Cloud:SessionRecovered",
                        message=(
                            f"Snowflake session recovered after retry during '{tag}'."
                        ),
                        level="INFO",
                        payload={"tag": tag, "recovered": True},
                    )
                except Exception:
                    pass
                return result
            finally:
                _retry_guard.in_retry = False

    return wrapper
