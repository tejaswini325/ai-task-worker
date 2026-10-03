# Submission form — copy/paste answers

**Repository:** <PASTE GITHUB URL>
**Demo video:** <PASTE LOOM/YOUTUBE (unlisted) URL>

**One-paragraph summary**
An autonomous task worker: given a natural-language goal, a Claude-driven loop operates a text browser against a sandbox
company (vendor invoice portal + internal ERP), remembers what it discovers, recovers from errors (pagination, 503s,
validation failures), asks for clarification or approval when it can't safely proceed, and only reports "done" after
re-reading the system of record. Safety and verification are enforced in code, not just prompts.

**Architecture (short)**
Generic observe→decide→act loop (`worker/agent.py`) → tool layer with guardrails (`worker/tools.py`) → text browser
(`worker/browser.py`) + memory + ask_user + sandboxed files. Only `environment.md` is task/environment-specific.
Details and diagram: `docs/ARCHITECTURE.md`.

**Key design decisions**
1. Guardrails in the runtime: write-approval gate, POSTs never auto-retried, `finish(done)` rejected until state is re-read.
2. HTTP+HTML text browser behind a tiny interface (Playwright-swappable) for speed, determinism and testability.
3. Working memory re-injected each turn + old observations elided, so long tasks stay bounded.
4. No agent framework — small, fully explainable code. Harness tested with a scripted model (6 tests, CI included).

**Models / APIs / frameworks / services**
Anthropic Claude (`claude-sonnet-5-5`, configurable) via the `anthropic` Python SDK with tool use; `requests`,
`beautifulsoup4`, `flask` (mock company), `pytest`, GitHub Actions. No other external services. AI coding assistance (Claude) used.

**Assumptions** — sandbox only, server-rendered HTML, creds supplied in `environment.md`, user available for approvals,
"latest invoice" = latest issue date excluding credit notes.

**Known limitations** — no JavaScript/PDF/downloads/CAPTCHA/MFA; approval per submission (chatty); write detection is a
heuristic; verification proves a re-read, not a correct comparison; per-run memory only; prompt-injection only partly mitigated.

**What I'd build next** — Playwright backend, PDF/email/API tools, risk-tiered approvals, independent verifier, persistent
memory/skills, idempotency + audit log, secrets vault, task queue with resume, fault-injection eval suite.
