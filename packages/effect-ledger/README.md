# effect-ledger

**A durable effect ledger for AI agents — prove what actually executed, block blind replays.**

Your agent crashed mid-retry. Did the email go out? Did the order get placed?
Chat history proves what the agent _remembered_. It cannot prove what the world _received_.
`effect-ledger` is that proof: a local, zero-dependency ledger of external side effects
with idempotency keys, fencing tokens, and probe-based reconciliation.

## 60-second start

```python
from effect_ledger import EffectLedger

ledger = EffectLedger("agent.db")

result = ledger.guard(
    task_id="order-42",
    step_id="charge_card",
    params={"amount": 99, "currency": "CNY"},
    execute=lambda: charge_card(99),          # your real side effect
    probe=lambda t, s: card_api.has_charge("order-42"),  # external ground truth
    risk="high",                              # high risk -> PAUSED for human if unverifiable
)
# result["action"] in: CONFIRMED / ALREADY_CONFIRMED / PAUSED / RETRY_ALLOWED / ...
```

`guard()` runs `execute()` **exactly once in effect**, even across crashes:

| What happened                    | Ledger verdict                                                 |
| -------------------------------- | -------------------------------------------------------------- |
| Fresh intent                     | executes, records receipt → `CONFIRMED`                        |
| Identical retry after success    | blocked at DB layer → `ALREADY_CONFIRMED` (zero replay)        |
| Crash after the op, receipt lost | `UNCERTAIN` → probe reconciles → `CONFIRMED` or safe retry     |
| Two executors racing             | valid lease wins; stale executor's receipts rejected (fencing) |
| High risk + probe can't confirm  | `PAUSED` — a human decides, never a guess                      |

## Why not Temporal / LangGraph checkpoints?

They answer "where was my program?" — this answers **"what did the world actually receive?"**
No runtime swap, no framework lock-in: one SQLite file, stdlib only, `pip install` and wrap
your tool call.

- Docs: `docs/` in the monorepo · Apache-2.0 · hongyun@hongyun.dev
