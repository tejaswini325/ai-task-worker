"""Independent pre-write reviewer. Before a WRITE is shown for approval, a separate LLM call (fresh context, no
tools) checks the payload against what the worker actually read. Catches 'plausible but wrong' values that a
persistence check cannot, e.g. an 'Issued' date typed into a 'Due date' field. Fails open if the model is unavailable."""
import json
import re

SYSTEM = "You are a strict QA reviewer for an automated worker. Reply with JSON only."

PROMPT = """TASK given to the worker:
{task}

The worker is about to submit this form to the destination system:
{payload}

Pages the worker has read so far (most recent last):
{sources}

Check every field value against those pages:
- Is it supported by the pages, and taken from the field with the SAME MEANING? (A 'Due date' must come from a due / \
payable-by field, not 'Issued' or 'Invoice date'; an amount must be that record's amount.)
- Is it the record the task actually asks for (e.g. the latest across ALL pages, an invoice rather than a credit note)?
- Reformatting is fine ('$14,980.00' -> '14980.00', '28 Oct 2026' -> '2026-10-28'); changing the underlying value is not.
Only report concrete problems you can point to in the pages. If a value never appears in the pages, say it is UNSUPPORTED.
Reply exactly as JSON: {{"ok": true|false, "concerns": ["short concrete problem", ...]}}"""


class Critic:
    def __init__(self, llm):
        self.llm = llm

    def review(self, task, payload, sources):
        src = "\n---\n".join(f"{u}\n{t}" for u, t in sources) or "(none)"
        prompt = PROMPT.format(task=task, payload=json.dumps(payload, indent=1), sources=src)
        try:
            text = self.llm.complete(SYSTEM, [{"role": "user", "content": prompt}], []).text
            m = re.search(r"\{.*\}", text, re.S)
            data = json.loads(m.group(0)) if m else {}
        except Exception:
            return []  # fail open: the human approval gate still applies
        if data.get("ok", True):
            return []
        return [str(c) for c in data.get("concerns", [])][:4] or ["reviewer flagged the submission (no detail)"]
