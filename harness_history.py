"""Session history: persist, compact, and reload across harness restarts."""

from __future__ import annotations

import json
import os
from typing import Any, Callable

SESSION_VERSION = 1
DEFAULT_STATE_DIR = ".harness_state"
DEFAULT_SESSION_FILE = "session.json"
NUDGE_PREFIXES = ("[harness]", "[Conversation memory")


def _project_root() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def session_path() -> str:
    return _state_path()


def _state_path() -> str:
    root = _project_root()
    rel = os.getenv("HARNESS_HISTORY_FILE", f"{DEFAULT_STATE_DIR}/{DEFAULT_SESSION_FILE}")
    if os.path.isabs(rel):
        return rel
    return os.path.join(root, rel)


def _compact_every() -> int:
    return max(2, int(os.getenv("HARNESS_HISTORY_COMPACT_EVERY", "6")))


def _max_chars() -> int:
    return max(8000, int(os.getenv("HARNESS_HISTORY_MAX_CHARS", "48000")))


def _keep_user_turns() -> int:
    return max(1, int(os.getenv("HARNESS_HISTORY_KEEP_USER_TURNS", "4")))


def enabled() -> bool:
    return os.getenv("HARNESS_HISTORY_ENABLED", "1").lower() not in ("0", "false", "no")


def load_summarizer_instructions() -> str:
    path = os.path.join(_project_root(), "harness_history.txt")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    return "Summarize the conversation for a CLI agent. Keep facts, answers, and open questions."


def empty_state() -> dict[str, Any]:
    return {
        "version": SESSION_VERSION,
        "summary": "",
        "history": [],
        "user_turns_since_compact": 0,
        "compactions": 0,
    }


def load_state() -> dict[str, Any]:
    path = _state_path()
    if not os.path.isfile(path):
        return empty_state()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return empty_state()
    if not isinstance(data, dict):
        return empty_state()
    data.setdefault("version", SESSION_VERSION)
    data.setdefault("summary", "")
    data.setdefault("history", [])
    data.setdefault("user_turns_since_compact", 0)
    data.setdefault("compactions", 0)
    if not isinstance(data["history"], list):
        data["history"] = []
    return data


def save_state(state: dict[str, Any]) -> None:
    path = _state_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def history_json_size(history: list) -> int:
    return len(json.dumps(history, ensure_ascii=False))


def is_real_user_message(item: dict) -> bool:
    if item.get("role") != "user":
        return False
    content = item.get("content") or ""
    if not isinstance(content, str):
        return False
    return not content.startswith(NUDGE_PREFIXES)


def user_message_indices(history: list) -> list[int]:
    return [i for i, item in enumerate(history) if is_real_user_message(item)]


def split_for_compaction(history: list, keep_user_turns: int) -> tuple[list, list]:
    indices = user_message_indices(history)
    if len(indices) <= keep_user_turns:
        return [], list(history)
    cut = indices[-keep_user_turns]
    return list(history[:cut]), list(history[cut:])


def item_to_text(item: dict) -> str:
    role = item.get("role")
    if role == "user":
        return f"USER: {item.get('content', '')}"
    if role == "assistant":
        return f"ASSISTANT: {item.get('content', '')}"
    typ = item.get("type")
    if typ == "function_call":
        return f"TOOL CALL {item.get('name')}: {item.get('arguments', '')}"
    if typ == "function_call_output":
        out = item.get("output", "")
        if len(out) > 2500:
            out = out[:2500] + "\n…[truncated]"
        return f"TOOL RESULT ({item.get('call_id', '')}): {out}"
    if typ == "message":
        parts = []
        for block in item.get("content") or []:
            if block.get("type") == "output_text":
                parts.append(block.get("text", ""))
        return "ASSISTANT: " + "".join(parts)
    return json.dumps(item, ensure_ascii=False)[:2000]


def serialize_slice(history_slice: list) -> str:
    return "\n\n".join(item_to_text(item) for item in history_slice)


def memory_message(summary: str) -> dict:
    return {
        "role": "user",
        "content": (
            "[Conversation memory — compiled from earlier turns; treat as facts]\n"
            f"{summary.strip()}"
        ),
    }


def rebuild_history(summary: str, tail: list) -> list:
    if summary.strip():
        return [memory_message(summary), *tail]
    return list(tail)


def compaction_reasons(history: list, state: dict) -> list[str]:
    reasons = []
    if not enabled():
        return reasons
    if state.get("user_turns_since_compact", 0) >= _compact_every():
        reasons.append(
            f"turnos {state.get('user_turns_since_compact', 0)}/{_compact_every()}"
        )
    size = history_json_size(history)
    if size >= _max_chars():
        reasons.append(f"tamanho {fmt_size(size)}/{fmt_size(_max_chars())}")
    return reasons


def fmt_size(n: int) -> str:
    if n >= 10_000:
        return f"{n / 1000:.1f}k"
    return str(n)


def usage_metrics(history: list, state: dict) -> dict[str, Any]:
    max_c = _max_chars()
    chars = history_json_size(history)
    turns_since = int(state.get("user_turns_since_compact", 0))
    every = _compact_every()
    summary = state.get("summary") or ""
    can_compact = bool(split_for_compaction(history, _keep_user_turns())[0]) and len(
        user_message_indices(history)
    ) > _keep_user_turns() + 1
    return {
        "history_enabled": True,
        "items": len(history),
        "chars": chars,
        "max_chars": max_c,
        "chars_remaining": max(0, max_c - chars),
        "chars_pct": round((chars / max_c) * 100, 1) if max_c else 0.0,
        "turns_since_compact": turns_since,
        "compact_every": every,
        "turns_until_compact": max(0, every - turns_since),
        "compactions": int(state.get("compactions", 0)),
        "summary_chars": len(summary),
        "can_compact": can_compact,
        "would_compact_now": bool(compaction_reasons(history, state)) and can_compact,
    }


def should_compact(history: list, state: dict) -> bool:
    if not enabled():
        return False
    indices = user_message_indices(history)
    if len(indices) <= _keep_user_turns() + 1:
        return False
    to_archive, _ = split_for_compaction(history, _keep_user_turns())
    if not to_archive:
        return False
    if state.get("user_turns_since_compact", 0) >= _compact_every():
        return True
    if history_json_size(history) >= _max_chars():
        return True
    return False


def summarize(
    archive_slice: list,
    prior_summary: str,
    *,
    api_fn: Callable[[dict], dict],
    text_from_response_fn: Callable[[dict], str],
    model: str,
) -> str:
    blob = serialize_slice(archive_slice)
    if prior_summary.strip():
        user_content = (
            f"Prior compiled memory:\n{prior_summary}\n\n"
            f"New transcript slice to merge:\n{blob}"
        )
    else:
        user_content = f"Transcript to compact:\n{blob}"
    payload = {
        "model": model,
        "instructions": load_summarizer_instructions(),
        "input": [{"role": "user", "content": user_content}],
    }
    response = api_fn(payload, stream=False, status_reason="historico")
    summary = text_from_response_fn(response).strip()
    if not summary:
        summary = prior_summary.strip() or "Earlier turns were truncated; no summary text returned."
    return summary


def compact(
    history: list,
    state: dict,
    *,
    api_fn: Callable[..., dict],
    text_from_response_fn: Callable[[dict], str],
    model: str,
) -> tuple[list, dict]:
    to_archive, tail = split_for_compaction(history, _keep_user_turns())
    if not to_archive:
        return history, state
    prior = state.get("summary") or ""
    import harness_status

    harness_status.compaction_start(len(to_archive), history_json_size(to_archive))
    new_summary = summarize(
        to_archive,
        prior,
        api_fn=api_fn,
        text_from_response_fn=text_from_response_fn,
        model=model,
    )
    new_history = rebuild_history(new_summary, tail)
    state = {
        **state,
        "summary": new_summary,
        "history": new_history,
        "user_turns_since_compact": 0,
        "compactions": int(state.get("compactions", 0)) + 1,
    }
    save_state(state)
    harness_status.compaction_done(len(new_summary), _state_path())
    harness_status.print_usage(usage_metrics(new_history, state))
    return new_history, state


def after_user_turn(history: list, state: dict) -> tuple[list, dict]:
    state = {**state, "history": history}
    state["user_turns_since_compact"] = int(state.get("user_turns_since_compact", 0)) + 1
    save_state(state)
    return history, state


def maybe_compact(
    history: list,
    state: dict,
    *,
    api_fn: Callable[..., dict],
    text_from_response_fn: Callable[[dict], str],
    model: str,
) -> tuple[list, dict]:
    if not should_compact(history, state):
        state = {**state, "history": history}
        save_state(state)
        return history, state
    import harness_status

    harness_status.compaction_trigger(compaction_reasons(history, state))
    return compact(
        history,
        state,
        api_fn=api_fn,
        text_from_response_fn=text_from_response_fn,
        model=model,
    )


def bootstrap_history() -> tuple[list, dict]:
    state = load_state()
    history = list(state.get("history") or [])
    summary = state.get("summary") or ""
    if history:
        return history, state
    if summary.strip():
        return [memory_message(summary)], state
    return [], state