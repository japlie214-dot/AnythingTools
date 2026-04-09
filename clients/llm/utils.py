# clients/llm/utils.py
"""Retry logic, backoff constants, and timeout factory."""

import asyncio
import httpx
from openai import RateLimitError, APITimeoutError, APIConnectionError

# Default timeout configuration (seconds)
DEFAULT_CONNECT_TIMEOUT_S = 10.0
DEFAULT_READ_TIMEOUT_S = 240.0
DEFAULT_WRITE_TIMEOUT_S = 30.0
DEFAULT_POOL_TIMEOUT_S = 30.0
MAX_API_RETRIES = 5
BASE_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 16.0


def _build_timeout() -> httpx.Timeout:
    """Build httpx timeout configuration."""
    return httpx.Timeout(
        connect=DEFAULT_CONNECT_TIMEOUT_S,
        read=DEFAULT_READ_TIMEOUT_S,
        write=DEFAULT_WRITE_TIMEOUT_S,
        pool=DEFAULT_POOL_TIMEOUT_S,
    )


def is_context_length_error(exc: Exception) -> bool:
    """
    Heuristic detector for provider context-length / prompt-too-large errors.

    Detection logic (in order):
      1. Try provider SDK typed exceptions via local import of openai.
      2. Check HTTP status attributes (status_code / http_status) == 400 then inspect message.
      3. Fallback: check substrings in the exception string for context/token phrases.

    Returns True only when it is reasonably likely the error is a context limit error.
    """
    try:
        import openai  # local import avoids strong top-level dependency
        # Some SDKs export typed BadRequest/InvalidRequest classes
        if isinstance(exc, getattr(openai, "BadRequestError", ())) or isinstance(
            exc, getattr(openai, "APIStatusError", ())
        ):
            msg = str(exc).lower()
            if "context_length_exceeded" in msg or "context window" in msg or "prompt too large" in msg or "maximum context" in msg:
                return True
    except Exception:
        # openai not installed or attributes missing — continue to heuristic checks
        pass

    status = getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
    if status == 400:
        msg = str(exc).lower()
        if "context" in msg and ("length" in msg or "too large" in msg or "max" in msg):
            return True

    # Generic text heuristic
    text = str(exc).lower()
    for marker in ("context_length_exceeded", "context length", "prompt too large", "maximum context", "request payload too large"):
        if marker in text:
            return True

    return False

async def _with_retry(request_factory):
    """Execute request factory with exponential backoff retry logic."""
    for attempt in range(1, MAX_API_RETRIES + 1):
        try:
            return await request_factory()
        except (RateLimitError, APITimeoutError, APIConnectionError):
            if attempt == MAX_API_RETRIES:
                raise
            await asyncio.sleep(
                min(MAX_BACKOFF_SECONDS, BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)))
            )
