"""The agent loop: observe -> decide (LLM) -> act (tool) -> observe, with context compaction,
loop detection and a hard step budget. Contains nothing task-specific."""
from __future__ import annotations

import copy
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path

from .tools import TOOL_SCHEMAS, Toolbox

SYSTEM = """You are an autonomous AI worker operating a computer on behalf of a user. You act through tools: a text \
browser, a scratch workspace, a working memory, and the user themselves. Achieve the user's END GOAL; they will not \
give you steps.

How to work
- Plan briefly, then act ONE tool call at a time and read each observation before deciding the next move.
- Page snapshots are dropped from your context after a few steps. Call `remember` for every fact you will need \
later (amounts, dates, ids, which record you selected and why).
- Copy values exactly as the source shows them; never invent or guess. Convert them to whatever format the \
destination demands. Read selection criteria carefully (e.g. "latest invoice" excludes credit notes; check ALL pages \
before concluding what is latest/largest/etc.).

Failures
- Transient errors (5xx, timeouts): retry. Validation errors: read the message, fix the input, resubmit.
- After a server error on a WRITE the outcome is unknown: re-read the system of record before retrying so you do not \
create duplicates.
- Never repeat an identical failing action more than twice; change approach, or ask the user, or finish blocked.

Safety
- Use only the systems and credentials listed in the environment briefing.
- Use `ask_user` when the request is ambiguous (several plausible matches), when required information is missing and \
cannot be found, or when something looks wrong. Do not ask for things you can look up. If the user denies a write, \
do not retry it.

Finishing
- After EVERY write, re-open the system of record and confirm the stored values match your intent. Only then call \
`finish`. status=done needs evidence quoting what you observed; use blocked/failed honestly when you could not \
complete the task. Keep the summary concise.

ENVIRONMENT BRIEFING
{env}
"""


@dataclass
class Result:
    status: str
    summary: str
    evidence: list
    steps: int
    run_dir: str


def compact(messages, keep=3, cap=240):
    """Elide old tool results to bound context; facts live in working memory instead."""
    out = copy.deepcopy(messages)
    idx = [i for i, m in enumerate(out) if m["role"] == "user" and isinstance(m["content"], list)]
    for i in idx[:-keep]:
        for b in out[i]["content"]:
            if b.get("type") == "tool_result" and len(b["content"]) > cap:
                b["content"] = b["content"][:cap] + " ...[older observation elided]"
    return out


class Agent:
    def __init__(self, llm, toolbox: Toolbox, env_text: str, io, max_steps=40):
        self.llm, self.tb, self.io, self.max_steps = llm, toolbox, io, max_steps
        self.env_text = env_text
        self.trace = open(Path(toolbox.run_dir) / "trace.jsonl", "a")

    def system(self):
        mem = json.dumps(self.tb.memory, indent=1) if self.tb.memory else "(empty)"
        return SYSTEM.format(env=self.env_text) + f"\nWORKING MEMORY\n{mem}\n"

    def _log(self, **kw):
        self.trace.write(json.dumps({"t": round(time.time(), 2), **kw}, default=str) + "\n")
        self.trace.flush()

    def run(self, task: str) -> Result:
        messages = [{"role": "user", "content": f"TASK: {task}"}]
        recent, nudges, step = [], 0, 0
        for step in range(1, self.max_steps + 1):
            reply = self.llm.complete(self.system(), compact(messages), TOOL_SCHEMAS)
            messages.append({"role": "assistant", "content": reply.blocks})
            if reply.text:
                self.io.log(f"\n\033[2m[{step}] {reply.text.strip()[:300]}\033[0m")
            calls = reply.tool_calls
            if not calls:
                nudges += 1
                if nudges > 3:
                    break
                messages.append({"role": "user", "content": "Continue by calling a tool. Call `finish` when done."})
                continue
            results = []
            for c in calls[:1]:  # one action at a time
                self.io.log(f"[{step}] > {c['name']} {json.dumps(c['input'])[:200]}")
                out = self.tb.execute(c["name"], c["input"])
                sig = (c["name"], json.dumps(c["input"], sort_keys=True), hashlib.md5(out.text.encode()).hexdigest())
                recent = (recent + [sig])[-3:]
                text = out.text
                if len(recent) == 3 and len(set(recent)) == 1:
                    text += "\nNOTE: identical action and result 3 times in a row. Change approach, ask the user, or finish."
                first = out.text.strip().splitlines()[0:2]
                self.io.log(f"      < {'ERR ' if out.is_error else ''}{' | '.join(first)[:160]}")
                self._log(step=step, tool=c["name"], input=c["input"], is_error=out.is_error, output=out.text[:1500])
                results.append({"type": "tool_result", "tool_use_id": c["id"], "content": text, "is_error": out.is_error})
            for c in calls[1:]:  # keep the API contract: every tool_use needs a result
                results.append({"type": "tool_result", "tool_use_id": c["id"], "is_error": True,
                                "content": "Skipped: one action per turn. Re-issue it if still needed."})
            messages.append({"role": "user", "content": results})
            if self.tb.final:
                break
        f = self.tb.final or {"status": "failed", "evidence": [],
                              "summary": f"Stopped without finishing after {step} steps (budget or no tool use)."}
        res = Result(f["status"], f["summary"], f["evidence"], step, str(self.tb.run_dir))
        (Path(self.tb.run_dir) / "result.json").write_text(json.dumps({**f, "steps": step}, indent=2))
        return res
