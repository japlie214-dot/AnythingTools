# api/sse/envelope.py
"""SSE wire-format envelope per WHATWG §9.2.5.

Ref: https://html.spec.whatwg.org/multipage/server-sent-events.html#parsing-an-event-stream
"""
import json
from typing import Any


def format_sse_event(
    *,
    event: str,
    id: str | None = None,
    data: Any = None,
    retry: int | None = None,
    comment: str | None = None,
) -> str:
    """Serialize one SSE event to a string terminated by \\n\\n.

    Multi-line `data` strings are split across multiple `data:` lines so the
    browser reassembles them with \\n. This is required by the spec: "If the
    field name is data, append the field value to the data buffer, then append
    a U+000A LINE FEED character."
    """
    lines: list[str] = []
    if comment is not None:
        # Comment lines start with `:` and are ignored by the client but
        # keep the connection alive through intermediary proxies.
        lines.append(f": {comment}")
    if event:
        lines.append(f"event: {event}")
    if id is not None:
        lines.append(f"id: {id}")
    if retry is not None:
        lines.append(f"retry: {retry}")
    if data is not None:
        if isinstance(data, (dict, list)):
            # Pydantic v2 would also work, but json.dumps is sufficient here
            # and avoids the model_dump_json() requirement for arbitrary dicts.
            # Ref: https://docs.pydantic.dev/latest/concepts/serialization/
            payload_str = json.dumps(data, ensure_ascii=False, default=str)
        else:
            payload_str = str(data)
        # Split on \n so each line gets its own `data:` prefix.
        for line in payload_str.split("\n"):
            lines.append(f"data: {line}")
    # The empty line terminates the event block.
    return "\n".join(lines) + "\n\n"
