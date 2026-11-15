#!/usr/bin/env python3
"""Minimal integration example: make an agent's "send email" tool safe to retry.

Run:  python examples/email_tool.py
Watch: the second identical call is blocked with ALREADY_CONFIRMED —
the email goes out exactly once even though the agent retried.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from effect_ledger import EffectLedger

DB = Path(tempfile.gettempdir()) / "email_demo.db"


def send_email_api(to: str, subject: str) -> dict:
    """Imagine this calls a real SMTP API. Non-idempotent by nature."""
    print(f"    [external] email actually sent -> {to}: {subject}")
    return {"message_id": "smtp-123"}


def probe_email(task_id: str, step_id: str) -> bool | None:
    """Ground-truth probe: ask the mail provider whether it already sent this.

    Real implementations: query SMTP send logs / provider API / IMAP Sent folder.
    Return None when the provider is unreachable — the ledger then keeps the
    intent UNCERTAIN (low risk) or PAUSED (high risk) instead of guessing.
    """
    conn = sqlite3.connect(DB)
    row = conn.execute(
        "SELECT state FROM effects WHERE task_id=? AND step_id=?", (task_id, step_id)
    ).fetchone()
    conn.close()
    # Demo stand-in: if our own ledger says it was confirmed, the provider has it.
    return row is not None and row[0] == "CONFIRMED"


def agent_tool_send_email(ledger: EffectLedger, to: str, subject: str) -> dict:
    """Wrap the tool with the ledger — one call site, retry-safe forever."""
    return ledger.guard(
        task_id=f"newsletter-{to}",
        step_id="send_email",
        params={"to": to, "subject": subject},
        execute=lambda: send_email_api(to, subject),
        probe=probe_email,
        risk="low",
    )


if __name__ == "__main__":
    DB.unlink(missing_ok=True)
    ledger = EffectLedger(str(DB))

    print("attempt 1 (fresh):")
    r1 = agent_tool_send_email(ledger, "customer@example.com", "Your order shipped")
    print(f"    -> {r1['action']}\n")

    print("attempt 2 (agent crashed and retried the identical call):")
    r2 = agent_tool_send_email(ledger, "customer@example.com", "Your order shipped")
    print(f"    -> {r2['action']}  (email NOT sent again)\n")

    print("attempt 3 (different params = different intent):")
    r3 = agent_tool_send_email(ledger, "customer@example.com", "Order update #2")
    print(f"    -> {r3['action']}\n")

    ledger.close()
    DB.unlink(missing_ok=True)
    print("Done: the world received exactly 2 emails for 3 agent calls.")
