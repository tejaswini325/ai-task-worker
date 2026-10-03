"""Tool layer: everything the model can do, plus the runtime guardrails around those actions
(approval gate for writes, verify-before-finish, sandboxed files, error containment)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .browser import Browser, BrowserError

MAX_OUT = 6000


@dataclass
class ToolOutput:
    text: str
    is_error: bool = False


def _s(name, desc, props, req):
    return {"name": name, "description": desc,
            "input_schema": {"type": "object", "properties": props, "required": req}}


STR = {"type": "string"}
TOOL_SCHEMAS = [
    _s("open_url", "Open a URL in the browser and return the page snapshot (text + numbered elements).",
       {"url": STR}, ["url"]),
    _s("click", "Click a link or button by element id from the latest snapshot. Buttons submit their form "
       "with the values set via fill. Buttons marked WRITE require user approval.", {"element_id": STR}, ["element_id"]),
    _s("fill", "Set the value of an input/select by element id (for selects give the option text).",
       {"element_id": STR, "value": STR}, ["element_id", "value"]),
    _s("remember", "Store a fact in working memory (amounts, dates, ids, decisions). Old page snapshots are "
       "dropped from context, so anything needed later MUST be remembered.", {"key": STR, "value": STR}, ["key", "value"]),
    _s("ask_user", "Ask the user a question when the task is ambiguous, info is missing, or you cannot proceed safely.",
       {"question": STR}, ["question"]),
    _s("list_files", "List files in the scratch workspace.", {}, []),
    _s("read_file", "Read a text file from the scratch workspace.", {"path": STR}, ["path"]),
    _s("write_file", "Write a text file in the scratch workspace.", {"path": STR, "content": STR}, ["path", "content"]),
    _s("finish", "End the task. status: done | blocked | failed. 'done' requires evidence gathered by re-reading the "
       "system of record AFTER your last write.",
       {"status": {"type": "string", "enum": ["done", "blocked", "failed"]}, "summary": STR,
        "evidence": {"type": "array", "items": STR, "description": "Exact facts you observed that prove the outcome"}},
       ["status", "summary", "evidence"]),
]


class Toolbox:
    def __init__(self, browser: Browser, io, workdir, run_dir):
        self.browser, self.io = browser, io
        self.workdir = Path(workdir).resolve()
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.run_dir = Path(run_dir)
        self.memory: dict[str, str] = {}
        self.dirty = False  # a write happened and hasn't been re-read since
        self.final = None

    def execute(self, name, args) -> ToolOutput:
        fn = getattr(self, f"t_{name}", None)
        if fn is None:
            return ToolOutput(f"Unknown tool {name!r}", True)
        try:
            out = fn(**args)
            out = out if isinstance(out, ToolOutput) else ToolOutput(str(out))
        except BrowserError as e:
            out = ToolOutput(f"ERROR: {e}", True)
        except TypeError as e:
            out = ToolOutput(f"ERROR: bad arguments for {name}: {e}", True)
        except Exception as e:  # never let a tool crash the loop
            out = ToolOutput(f"ERROR: {type(e).__name__}: {e}", True)
        if len(out.text) > MAX_OUT:
            out.text = out.text[:MAX_OUT] + "\n...[truncated]"
        return out

    # ---------------------------------------------------------------- browser
    def _page(self):
        snap = self.browser.snapshot()
        return ToolOutput(snap, is_error=self.browser.status >= 400)

    def t_open_url(self, url):
        self.browser.open(url)
        self.dirty = False  # a fresh read of a page counts as re-checking state
        return self._page()

    def t_click(self, element_id):
        el = self.browser.element(element_id)
        if el.kind == "link":
            self.browser.click(el)
            self.dirty = False
            return self._page()
        if el.kind != "button":
            raise BrowserError(f"{element_id} is a {el.kind}; use fill")
        method, url, data = self.browser.preview_submit(el)
        if el.risk == "write":
            shown = {k: ("*****" if "pass" in k.lower() else v) for k, v in data.items()}
            lines = "\n    ".join(f"{k} = {v!r}" for k, v in shown.items())
            if not self.io.approve(f"{method} {url}\n    {lines}"):
                return ToolOutput("DENIED by user: this write was NOT performed. Do not retry it. Ask the user what "
                                  "they want instead, or finish with status=blocked.", True)
        self.browser.click(el)
        out = self._page()
        if el.risk == "write":
            s = self.browser.status
            if s >= 500:
                self.dirty = True
                out.text += ("\nNOTE: server error on a WRITE. Outcome is UNKNOWN (it may have been saved). Re-read "
                             "the list/record page to check BEFORE submitting again, to avoid duplicates.")
            elif s < 400:
                self.dirty = True
                out.text += "\nNOTE: write submitted. Verify it by re-opening the system of record before finishing."
            else:
                out.text += "\nNOTE: write was rejected (nothing saved). Read the message, fix the inputs, resubmit."
        return out

    def t_fill(self, element_id, value):
        self.browser.fill(element_id, value)
        return ToolOutput(f"OK. {self.browser._fmt(self.browser.element(element_id))}")

    # ------------------------------------------------------- memory / human
    def t_remember(self, key, value):
        self.memory[key] = value
        return f"Stored. Memory now has {len(self.memory)} item(s)."

    def t_ask_user(self, question):
        return f"User answered: {self.io.ask(question)}"

    # ------------------------------------------------------------------ files
    def _safe(self, path):
        p = (self.workdir / path).resolve()
        if self.workdir not in p.parents and p != self.workdir:
            raise BrowserError("Path escapes the workspace")
        return p

    def t_list_files(self):
        return "\n".join(sorted(str(p.relative_to(self.workdir)) for p in self.workdir.rglob("*") if p.is_file())) or "(empty)"

    def t_read_file(self, path):
        return self._safe(path).read_text()

    def t_write_file(self, path, content):
        p = self._safe(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"Wrote {len(content)} chars to {path}"

    # ----------------------------------------------------------------- finish
    def t_finish(self, status, summary, evidence):
        evidence = [evidence] if isinstance(evidence, str) else list(evidence or [])
        if status == "done":
            if self.dirty:
                return ToolOutput("REJECTED: you wrote data but have not re-read the system of record since. Open the "
                                  "relevant page, confirm the saved values match your intent, then call finish again.", True)
            if not evidence:
                return ToolOutput("REJECTED: status=done requires concrete evidence you observed.", True)
        self.final = {"status": status, "summary": summary, "evidence": evidence, "memory": dict(self.memory)}
        (self.run_dir / "final_page.txt").write_text(self.browser.snapshot())
        return "Recorded."
