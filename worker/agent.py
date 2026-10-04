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

SYSTEM = """You are an autonomous AI worker operating a computer for a user via tools (text browser, memory, scratch \
files, the user). Achieve the user's end goal; they give no steps.

Work
- One tool call at a time; read each observation before the next. Plan briefly.
- Element ids (e1, e2, ...) are valid only for the page currently shown. Use fill_many for several fields at once.
- Gather first, then enter: collect every value you need from the source (open a detail page if a field such as a due \
date is not on the list page) and `remember` the exact values the moment you see them (never guesses) BEFORE opening \
the destination form. Leaving a half-filled form discards it: open the form, fill_many, submit.
- Do not ask the user for facts you can look up. Selection rules matter ("latest invoice" excludes credit notes; check \
all pages). On lists, read the pagination and sort order in the page text; for latest/oldest/largest/etc. examine \
EVERY page before choosing. Copy values exactly, then convert to the format the destination requires.

Failures
- 5xx/timeouts: retry. Validation error: read the message, fix the input, resubmit. After a 5xx on a WRITE, re-read the \
system of record before retrying (avoid duplicates). Never repeat the same failing action more than twice.

Safety
- Use only the systems/credentials in the briefing. ask_user when the request is ambiguous (several matches), info is \
missing and cannot be looked up, or something looks wrong. If the user denies a write, do not retry unless they \
explicitly tell you to.

Finish
- After every write, re-open the system of record and confirm the stored values. Before finishing, check each \
requirement of the task, including that the record you acted on really satisfies the selection rule. Then call finish: done with evidence \
quoting what you saw, or blocked/failed honestly. Keep the summary short.

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


def compact(messages, keep=3, cap=200):
    """Elide old tool results to bound context; facts live in working memory instead."""
    out = copy.deepcopy(messages)
    idx = [i for i, m in enumerate(out) if m["role"] == "user" and isinstance(m["content"], list)]
    for i in idx[:-keep]:
        for b in out[i]["content"]:
            if b.get("type") == "tool_result" and len(b["content"]) > cap:
                b["content"] = b["content"][:cap] + " ...[older observation elided]"
    return out


class Agent:
    def __init__(self, llm, toolbox: Toolbox, env_text: str, io, max_steps=40, critic=None):
        self.llm, self.tb, self.io, self.max_steps = llm, toolbox, io, max_steps
        self.env_text = env_text
        if critic:
            toolbox.critic = critic
        self.trace = open(Path(toolbox.run_dir) / "trace.jsonl", "a")

    def system(self):
        mem = json.dumps(self.tb.memory, indent=1) if self.tb.memory else "(empty)"
        recent = "\n---\n".join(f"{u}\n{t}" for u, t in self.tb.page_log.items() if u != self.tb.browser.url)
        return (SYSTEM.format(env=self.env_text) + f"\nWORKING MEMORY\n{mem}\n"
                + (f"\nRECENTLY READ PAGES (text only, for reference)\n{recent}\n" if recent else ""))

    def _log(self, **kw):
        self.trace.write(json.dumps({"t": round(time.time(), 2), **kw}, default=str) + "\n")
        self.trace.flush()

    def run(self, task: str) -> Result:
        self.tb.task = task
        messages = [{"role": "user", "content": f"TASK: {task}"}]
        recent, opens, nudges, step = [], [], 0, 0
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
                if c["name"] == "open_url":
                    opens = (opens + [c["input"].get("url")])[-12:]
                    if opens.count(c["input"].get("url")) >= 3:
                        text += ("\nNOTE: you have opened this URL 3 times recently. The facts you need should already be "
                                 "in WORKING MEMORY / RECENTLY READ PAGES. Use them instead of re-reading.")
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