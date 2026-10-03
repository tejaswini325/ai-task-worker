# Demo video script (target 3–4 min, screen + voice)

Setup before recording: fresh terminal, large font, `export ANTHROPIC_API_KEY=...`, `rm -rf runs`, repo open in editor.
Tip: record at 1080p (OBS / Loom / QuickTime), upload unlisted to YouTube or Loom, paste the link in the form.

**0:00 – Intro (20s)** — show README top.
> "This is an autonomous task worker. I give it a goal in plain English; it operates a browser against a sandbox
> company — a vendor invoice portal and an internal ERP — and it has to finish the job *and prove it*."

**0:20 – Architecture (30s)** — show the diagram in `docs/ARCHITECTURE.md` (or README).
> "One generic loop: the model sees pages as text with numbered elements and calls one tool per turn. The important
> guardrails are in code, not the prompt: write approval, no auto-retry of writes, and `finish` is rejected unless the
> system of record was re-read after the last write."

**0:50 – Scenario 1: happy path (≈90s)** — `make demo1`. Narrate as it runs, point out:
1. logs into the portal itself (no steps given); searches company; **page 1 of 2 → goes to page 2** to find the latest.
2. skips **CN-1020 (credit note)**, selects **INV-1019**; calls `remember`.
3. detail page returns **503** → retry → success.
4. logs into ERP, fills form; **approval prompt** shows exact payload → press `y`.
5. ERP **rejects the format** (`$14,980.00`, `28 Oct 2026`) → agent reads error, fixes to `14980.00` / `2026-10-28`, resubmits (approve again).
6. re-opens `/erp/bills`, sees the row, calls `finish(done)` with evidence.
Then show the printed **ground truth** line and `runs/<ts>/trace.jsonl`.

**2:20 – Scenario 2: ambiguity (30s)** — `make demo2`. Agent searches "Acme", sees two vendors, **asks you** which. Answer "Acme Corp".

**2:50 – Scenario 3: denial (20s)** — `make demo3`, answer `n`. Show ground truth `[]` and status BLOCKED.

**3:10 – Guardrail proof (25s)** — `make test` → 6 passed; mention the tests use a scripted model to prove recovery,
gating and verify-before-finish deterministically.

**3:35 – Close (20s)**
> "Limitations: no JavaScript or PDFs yet, approval is per submission. Next: Playwright backend, risk-tiered approvals,
> an independent verifier, persistent memory."

If a live run behaves differently than expected, keep it in — then show the trace and explain what the agent decided.
That is exactly what the interview asks for.
