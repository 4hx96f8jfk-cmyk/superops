# HongYun · SuperOps

**Give your AI agent a ledger that answers one question: did it actually execute?**

SuperOps is an operations layer for agent side-effect reliability and model-access governance. Your agent knows what it _intended_ to do. SuperOps tells you what actually _happened_ — which command ran, which email was sent, whether that retry double-charged.

> Official name: **HongYun Super Ops Agent** (technical short name: **SuperOps**). An independent HongYun product line.

## The problem

Checkpoints let an agent _remember_. They cannot prove that a command ran, that an email went out, or that a retry didn't bill twice.

- Industry consensus in 2026: checkpoint state ≠ executed effects. Memory is not proof.
- Practitioner reports: on a busy agent, roughly **1 in 4 tool calls may be duplicated**. Duplicate orders, duplicate sends, duplicate charges are the accident zone of 2026.
- Existing tools leave a gap. Temporal, Restate, and DBOS are infrastructure-layer durable-execution engines — you have to move your workload into their runtime. LangGraph checkpoints store state, not effect proof. **Semantic-layer effect reconciliation that doesn't require switching frameworks is an open gap.** That's what SuperOps fills.

## Quick start

> Placeholder — packages are being extracted from production code and will be published on PyPI.

```bash
# coming soon
pip install superops-cli
superops init        # guided setup, SQLite by default, zero external deps
```

Expected first flow: wrap one tool (e.g. a "send email" function), register its side effect, crash the process mid-call, restart, and watch the ledger reconcile the `UNCERTAIN` effect against external truth instead of blindly re-running it.

## Three components

| Component         | Package         | What it does                                                                                                                                                                                       |
| ----------------- | --------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Effect Ledger** | `effect-ledger` | Side-effect registration, idempotency keys, lease fencing, `UNCERTAIN` → reconcile-against-truth → `CONFIRMED` / escalate-to-human, audit events                                                   |
| **Model Hub**     | `modelhub`      | Local + cloud LLM provider registry, health probes via _real inference_ (self-reported green ≠ green), local-first routing with fallback chains, secrets stored as references only, usage stats    |
| **Ops Console**   | `ops-console`   | Ledger visualization, effect DAG, authentication. Dual mode: `full` (all write operations, fully audited) or `read-only` (hardened guard rail). Hard rule: it will never become a second scheduler |

## Why it's different

1. **Semantic-layer reconciliation.** SuperOps doesn't take over orchestration. It governs side effects: probe what's uncertain, reconcile against external truth, confirm or escalate.
2. **Non-invasive.** Poller/adapter shape. `pip install` and go — keep whatever agent framework you already use.
3. **Forged in production.** Dogfooded on an internal agent fleet: 747 tests, counter-example fixtures, end-to-end crash-recovery scenarios, and shadow-enforcement with a measured false-block rate of 0.00%.
4. Rooted in HongYun's bio-evolutionary design lineage (Eastern-philosophy-inspired self-evolution mechanisms) — details in the brand story, not in your critical path.

## Open source

Core is fully open under **Apache-2.0**: `effect-ledger` and `modelhub`. A commercial tier (multi-tenant console, compliance reporting) is planned but **not yet available** — no waitlist fiction here.

## Contact

**hongyun@hongyun.dev**
