# Technical discussion prep

**Why a text browser, not Playwright/computer-use?** Fast, cheap, deterministic, testable offline; text is easier for an
LLM than pixels. Cost: no JS/visuals. `Browser` is a 5-method interface so Playwright drops in.

**How does it decide the next step?** The LLM sees task + environment briefing + working memory + last 3 observations in full
and picks one tool. Prompt teaches policy (retry/verify/ask); code enforces safety. Trace in `runs/<ts>/trace.jsonl`.

**How does it handle a failing action?** Layered: GET retries in `browser._request`; errors returned as observations;
`tools.t_click` appends NOTE guidance after write failures; `agent.run` loop detection after 3 identical results.

**Why never auto-retry POSTs?** A 5xx may have committed the write; retrying could duplicate. Tool says "outcome UNKNOWN —
re-read first" (`tools.t_click`).

**How is "done" verified?** `Toolbox.dirty` set after a write (`t_click`), cleared by any page navigation
(`t_open_url`/link click); `t_finish` rejects `done` while dirty or with empty evidence. Honest limitation: it proves a
re-read happened, not that the model compared values correctly — next step is an independent verifier.

**What's remembered?** `Toolbox.memory` rendered into the system prompt each turn (`Agent.system`). `compact()` elides
tool results older than 3 turns, so memory is how facts survive.

**When does it ask the user?** Model-driven `ask_user` (ambiguity/missing info) + code-driven approval for WRITE buttons
(`browser._index`: non-login POST ⇒ `risk="write"`, or `data-risk` attribute).

**How general is it?** Loop, tools, prompt unchanged across tasks; only `environment.md` differs. Limits: server-rendered HTML.

**Security?** Host allow-list (also checked after redirects), password masking, workspace-confined files, sandbox creds,
no secrets in prompts beyond the briefing. Prompt injection from web pages is NOT fully solved: page text is untrusted;
mitigations today are approval on writes + allow-list. Next: treat page text as data with a separate verifier, scoped creds.

**How would you scale it to production?** Playwright pool + queue workers, checkpoint/resume, idempotency keys, audit log,
secrets vault, tiered approvals, eval suite with fault injection, cost/latency budgets, observability.

## Likely live modifications — where to touch
* *Auto-approve small amounts:* in `tools.t_click`, before `io.approve`, parse `data` and skip if amount < threshold.
* *Add a tool (e.g. `http_get_json`):* add schema to `TOOL_SCHEMAS`, add `t_http_get_json` method; nothing else.
* *New environment:* edit `environment.md`, run with `--no-mock --allow-host <host>`.
* *Change step budget / context window:* `--max-steps`; `compact(keep=3, cap=240)`.
* *Add a failure mode to the sandbox:* `mockcorp/app.py` (e.g. flake counter `st["flake"]`), add a test in `tests/test_agent.py`.
* *Debug "why did it do X":* open `trace.jsonl`; each line = step, tool, input, output.
