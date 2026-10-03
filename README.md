# Autonomous AI Task Worker (prototype)

Give it a task in plain English; it logs into systems, reads data, writes data, recovers from errors, asks you when
unsure, **verifies the outcome in the system of record**, and returns a summary with evidence.

```
python run.py "Find the latest invoice from Globex Corporation in the vendor portal, record it as a bill in our internal ERP, and tell me once it's done."
```

Docs: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) · [`docs/DEMO_SCRIPT.md`](docs/DEMO_SCRIPT.md) · [`docs/TECHNICAL_QA.md`](docs/TECHNICAL_QA.md) · [`SUBMISSION.md`](SUBMISSION.md)

## Setup (2 min)
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export GROQ_API_KEY=gsk_...                 # free key: console.groq.com; default model llama-3.3-70b-versatile
make demo1                                  # or: python run.py "<task>"  — starts the bundled mock company on :5001 and runs the agent
pytest -q                                   # 9 tests (harness + LLM adapter), no API key needed
```
Flags: `--provider groq|anthropic`, `--model <id>` (or `WORKER_MODEL`), `--yes` auto-approve writes (default asks `y/N`), `--no-mock` point at your own sandbox, `--max-steps`, `--env`.
Groq notes: the free tier has per-minute token limits; the SDK auto-backs-off on 429s, so a run may pause briefly. If a model
misbehaves with tools, try `--model` with another Groq tool-capable model. Malformed tool calls are re-sampled automatically.
Every run writes `runs/<ts>/trace.jsonl` (every action + observation), `final_page.txt`, `result.json`.
`python -m mockcorp.app` serves the mock company for manual poking.

## Demo script (3 scenarios, all against the sandbox)
1. **Happy path + failures** – the task above. Traps built into the sandbox: invoices are paginated oldest-first (latest is
   on page 2), the newest document is a *credit note* (not an invoice), the first 3 invoice-detail loads return 503, and the
   ERP rejects `$14,980.00` / `28 Oct 2026` (wants `14980.00` / `2026-10-28`). Expected: agent picks INV-1019, retries,
   normalises formats, you approve the write, it re-opens the bills list and confirms.
2. **Ambiguity** – `"Record the latest Acme invoice in the ERP"` → "Acme Corp" vs "Acme Industries" → agent calls `ask_user`.
3. **Safety** – answer `n` at the approval prompt → nothing is written, agent finishes `blocked`.

## Architecture
```
 task ──► Agent loop ───────────────┐   (worker/agent.py — no task-specific code)
          │ LLM (Groq, tool use)  │
          ▼                         │ observation
        Toolbox (worker/tools.py) ──┘
   ┌──────┼───────────┬───────────┬──────────┐
 Browser  Memory   ask_user    Files      finish
(text browser:   (remember;   (clarify /  (sandboxed  (gated by
 pages → text +  re-injected   approve)    scratch)    verification)
 numbered        every turn)
 elements)
```
* **Observe → decide → act loop.** The model sees a page as text + numbered elements (`[e7] input "Amount" …`) and
  calls one tool per turn. No hard-coded workflow: the same loop/tools/prompt work for any task on any website.
  Task-specific knowledge lives only in `environment.md` (which systems exist + sandbox creds).
* **Memory.** `remember(key,value)` is shown in the system prompt on every turn. Old page snapshots are elided from
  context (`compact`), so long tasks stay cheap and the model must record what matters.
* **Reliability layers.** (1) Browser auto-retries idempotent GETs on 5xx/network errors with backoff. (2) Failures are
  returned to the model as observations (`ERROR RESPONSE`, validation messages) so it can fix inputs and retry.
  (3) POSTs are *never* auto-retried; after a 5xx on a write the tool says the outcome is UNKNOWN and the model must
  re-read state first (prevents duplicate records). (4) Tool exceptions are contained; loop detection nudges after 3
  identical action+result; hard step budget; nudges if the model stops calling tools.
* **Verification.** The runtime tracks a `dirty` flag set by any successful/uncertain write and cleared only by
  re-reading a page. `finish(status="done")` is *rejected* while dirty or without evidence, so "done" can't be claimed
  from the model's belief alone. The final page snapshot is saved as evidence.
* **Human in the loop.** Any non-login POST (or element tagged `data-risk`) is a WRITE → shows the exact payload and
  needs approval. `ask_user` for ambiguity/missing info. Denied writes are final.
* **Safety.** Host allow-list on every request and redirect, passwords masked in approvals/snapshots, file tools
  confined to `workspace/`, sandbox credentials only.

## Design decisions (and why)
* **HTTP + HTML text browser instead of Playwright/screenshots.** It runs anywhere with `pip install`, is fast, cheap,
  deterministic and fully testable offline; structured text is also easier for an LLM to act on than pixels. The `Browser`
  interface (`open/click/fill/preview_submit/snapshot`) is small so a Playwright backend can replace it for JS-heavy sites.
* **Guardrails in the runtime, not the prompt.** Approval, verify-before-finish, allow-list and no-POST-retry are code,
  so they hold even if the model misbehaves. The prompt only teaches judgment.
* **One action per turn** (`disable_parallel_tool_use`): browser actions depend on each other's results.
* **Harness tests use a scripted policy** (fake LLM) to prove the guardrails and recovery paths deterministically.
  They test the *harness*, not model intelligence; the real-model behaviour is shown in the demo.
* **No framework** (LangChain etc.) – ~600 lines I can fully explain and modify live.

## Models / services / libraries
Groq API (free tier) via the `groq` SDK, OpenAI-style tool calling, default model `llama-3.3-70b-versatile` (swappable with `--model`; an Anthropic adapter is included but optional), `requests`,
`beautifulsoup4`, `flask` (mock company only), `pytest`. No other external services. Built with AI assistance (Claude).

## Assumptions
Sandbox systems are plain server-rendered HTML; credentials are provided in `environment.md`; one task at a time;
"latest" = most recent issue date among invoices (credit notes excluded); the user is available for approvals.

## Known limitations
* No JavaScript execution, file downloads/PDF parsing, CAPTCHAs/MFA, or visual understanding.
* Approval is per submission, so a rejected-then-corrected write asks again (safe but chatty).
* Write detection is a heuristic (POST without password field); GET-based mutations would be missed.
* Evidence = last page + model-quoted facts; no independent programmatic cross-check of the model's claims.
* Memory is per-run only; no learning across tasks. Single-agent, no parallelism or cost/budget controls.
* Real-model runs are non-deterministic; I evaluated on the three scenarios above, not a benchmark.

## What I'd build next
Playwright backend (JS, uploads, downloads) with screenshot fallback · PDF/email/API tools · risk-tiered approvals
(approve a *diff*/policy, e.g. "bills < X auto-OK") · independent verifier step (second model/rule checks evidence
against task) · persistent memory + reusable "skills" learned from successful runs · idempotency keys and audit log ·
secrets vault + per-task scoped credentials · task queue, retries/resume from checkpoint, evals suite with
fault-injection scenarios, observability dashboard.
