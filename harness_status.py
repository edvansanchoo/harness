"""User-facing status lines on stderr (progress, LLM wait, history budget)."""

from __future__ import annotations

import os
import sys
from typing import Any


def enabled() -> bool:
    return os.getenv("HARNESS_STATUS", "1").lower() not in ("0", "false", "no")


def log(message: str) -> None:
    if not enabled():
        return
    sys.stderr.write(f"[status] {message}\n")
    sys.stderr.flush()


def fmt_chars(n: int) -> str:
    if n >= 10_000:
        return f"{n / 1000:.1f}k"
    return str(n)


def llm_wait(*, stream: bool, reason: str = "chat") -> None:
    mode = "stream" if stream else "resposta completa"
    log(f"Aguardando LLM ({reason}, {mode})…")


def llm_event(event: str) -> None:
    if event == "created":
        log("LLM: processando…")
    elif event == "first_token":
        log("LLM: gerando resposta…")
    elif event == "done":
        log("LLM: rodada concluída.")


def tool_running(command: str) -> None:
    preview = command.replace("\n", " ")
    if len(preview) > 72:
        preview = preview[:69] + "…"
    log(f"Executando: {preview}")


def compaction_trigger(reasons: list[str]) -> None:
    log(f"Compactação do histórico acionada ({'; '.join(reasons)})")


def compaction_start(archived_items: int, archived_chars: int) -> None:
    log(
        f"Compactando histórico: {archived_items} itens, "
        f"{fmt_chars(archived_chars)} chars → resumo (LLM sem stream)…"
    )


def compaction_done(summary_chars: int, path: str) -> None:
    log(f"Compactação salva: resumo {fmt_chars(summary_chars)} chars em {path}")


def usage_line(metrics: dict[str, Any]) -> str:
    if not metrics.get("history_enabled"):
        return "[status] Histórico persistente desativado."

    parts = [
        f"Histórico: {fmt_chars(metrics['chars'])} / {fmt_chars(metrics['max_chars'])} chars",
        f"({metrics['chars_pct']}%)",
        f"· restante: {fmt_chars(metrics['chars_remaining'])} chars",
        f"· turnos até compilar: {metrics['turns_until_compact']}",
        f"(contador {metrics['turns_since_compact']}/{metrics['compact_every']})",
        f"· {metrics['items']} itens",
        f"· memória compilada: {fmt_chars(metrics['summary_chars'])} chars",
        f"· compilações: {metrics['compactions']}",
    ]
    return "[status] " + " ".join(parts)


def print_usage(metrics: dict[str, Any]) -> None:
    if not enabled():
        return
    sys.stderr.write(usage_line(metrics) + "\n")
    sys.stderr.flush()


def print_usage_after_turn(history, state, *, history_module) -> None:
    if not history_module.enabled():
        print_usage({"history_enabled": False})
        return
    print_usage(history_module.usage_metrics(history, state))