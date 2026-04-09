# bot/orchestrator/eviction.py
"""Context budget enforcement.

Implements FIFO eviction of older messages until the total cost falls within the
configured token budget.
"""


def enforce_budget(messages: list[dict], max_budget: int) -> list[dict]:
    """Trim older messages until total cost <= max_budget.

    Cost is defined as sum of `char_count` + `attachment_char_count`.
    The system prompt (role == "system") at index 0 is always preserved if present.
    """
    total_cost = sum(m.get("char_count", 0) + m.get("attachment_char_count", 0) for m in messages)
    if total_cost <= max_budget:
        return messages

    # FIFO eviction: keep system prompt if present
    trimmed = []
    sys_msg = None
    if messages and messages[0].get("role") == "system":
        sys_msg = messages[0]
        total_cost -= (sys_msg.get("char_count", 0) + sys_msg.get("attachment_char_count", 0))
        messages = messages[1:]

    for msg in reversed(messages):
        cost = msg.get("char_count", 0) + msg.get("attachment_char_count", 0)
        if total_cost > max_budget:
            total_cost -= cost
            continue
        trimmed.insert(0, msg)

    if sys_msg:
        trimmed.insert(0, sys_msg)
    return trimmed
