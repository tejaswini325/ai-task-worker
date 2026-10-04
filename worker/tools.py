"""Tool layer: everything the model can do, plus the runtime guardrails around those actions
(approval gate for writes, verify-before-finish, sandboxed files, error containment)."""
from __future__ import annotations

import re
from collections import OrderedDict
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
    _s("open_url", "Open a URL; returns page text + numbered elements.", {"url": STR}, ["url"]),
    _s("click", "Click a link/button by element id. Buttons submit their form. WRITE buttons need user approval.",
       {"element_id": STR}, ["element_id"]),
    _s("fill", "Set one input/select (select: option text).", {"element_id": STR, "value": STR}, ["element_id", "value"]),
    _s("fill_many", "Set several inputs/selects on the current page in one call.",
       {"fields": {"type": "array", "items": {"type": "object", "properties": {"element_id": STR, "value": STR},
                                              "required": ["element_id", "value"]}}}, ["fields"]),
    _s("remember", "Store a fact (exact value) in working memory. Old snapshots get dropped, memory does not.",
       {"key": STR, "value": STR}, ["key", "value"]),
    _s("ask_user", "Ask the user when ambiguous, missing info that can't be looked up, or unsafe to proceed.",
       {"question": STR}, ["question"]),
    _s("list_files", "List scratch workspace files.", {}, []),
    _s("read_file", "Read a workspace file.", {"path": STR}, ["path"]),
    _s("write_file", "Write a workspace file.", {"path": STR, "content": STR}, ["path", "content"]),
    _s("finish", "End the task: done | blocked | failed. 'done' needs evidence observed AFTER your last write.",
       {"status": {"type": "string", "enum": ["done", "blocked", "failed"]}, "summary": STR,
        "evidence": {"type": "array", "items": STR}}, ["status", "summary", "evidence"]),
]


class Toolbox:
    def __init__(self, browser: Browser, io, workdir, run_dir):
        self.browser, self.io = browser, io
        self.workdir = Path(workdir).resolve()
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.run_dir = Path(run_dir)
        self.memory: dict[str, str] = {}
        self.task, self.critic, self.flagged = "", None, {}
        self.seen = OrderedDict()  # every page read this run (for the reviewer), bounded
        self.page_log = OrderedDict()  # url -> text of recently read pages (survives context compaction)
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
        b = self.browser
        snap = b.snapshot()
        if b.status < 400 and "login" not in b.url:
            txt = b._page_text()
            self.seen[b.url] = f"{b.title}\n{txt[:700]}"
            self.seen.move_to_end(b.url)
            while len(self.seen) > 10:
                self.seen.popitem(last=False)
            self.page_log[b.url] = f"{b.title}\n{txt[:500]}"
            self.page_log.move_to_end(b.url)
            while len(self.page_log) > 3:
                self.page_log.popitem(last=False)
        m = re.search(r"[Pp]age (\d+) of (\d+)", b._page_text())
        has_next = any(e.kind == "link" and re.search(r"\bnext\b", e.label, re.I) for e in b.elements.values())
        if (m and int(m.group(1)) < int(m.group(2))) or has_next:
            where = f"page {m.group(1)} of {m.group(2)}" if m else "a multi-page list"
            snap += (f"\nNOTE: pagination: this is {where}; more results exist on other pages. If your choice depends on "
                     "ALL results (latest, oldest, largest, first...), view every page before deciding.")
        if b.status >= 500:
            snap += f"\nNOTE: transient server error. Retry with open_url on this exact URL: {b.url}"
        return ToolOutput(snap, is_error=b.status >= 400)

    def _discard_note(self, had_input):
        return ("\nNOTE: you navigated away, so values you typed into the previous form were DISCARDED. Gather the data "
                "you need first, then fill and submit a form in one go." if had_input else "")

    def t_open_url(self, url):
        had = bool(self.browser.fills)
        self.browser.open(url)
        self.dirty = False  # a fresh read of a page counts as re-checking state
        out = self._page()
        out.text += self._discard_note(had)
        return out

    def t_click(self, element_id):
        el = self.browser.element(element_id)
        if el.kind == "link":
            had = bool(self.browser.fills)
            self.browser.click(el)
            self.dirty = False
            out = self._page()
            out.text += self._discard_note(had)
            return out
        if el.kind != "button":
            raise BrowserError(f"{element_id} is a {el.kind}; use fill")
        method, url, data = self.browser.preview_submit(el)
        warn = ""
        if el.risk == "write" and self.critic:
            key = tuple(sorted(data.items()))
            if key in self.flagged:  # already flagged once and resubmitted unchanged: let the human decide
                warn = "\n  !! automated reviewer concerns (unresolved): " + "; ".join(self.flagged[key])
            else:
                sources = [(u, t) for u, t in self.seen.items() if u != self.browser.url]
                concerns = self.critic.review(self.task, self.browser.labeled_payload(el, data), sources)
                if concerns:
                    self.flagged[key] = concerns
                    self.io.log("      ! pre-write review flagged: " + "; ".join(concerns))
                    return ToolOutput("BLOCKED by automated pre-write review; nothing was submitted. Concerns:\n- "
                                      + "\n- ".join(concerns) + "\nResolve them by checking the source (e.g. open the "
                                      "record's detail page), then resubmit the corrected values. If you are certain they "
                                      "are right, resubmit unchanged and the user will decide.", True)
        if el.risk == "write":
            shown = {k: ("*****" if "pass" in k.lower() else v) for k, v in data.items()}
            lines = "\n    ".join(f"{k} = {v!r}" for k, v in shown.items())
            if not self.io.approve(f"{method} {url}\n    {lines}{warn}"):
                return ToolOutput("DENIED by user: this write was NOT performed. Do not retry on your own; if the user "
                                  "later explicitly tells you to proceed you may resubmit (they will be asked again), "
                                  "otherwise finish with status=blocked.", True)
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
                out.text += "\nNOTE: write was rejected (nothing saved). Read the message. The form has been RESET: refill EVERY field (fill_many), fixing what the message says, then resubmit."
        return out

    def t_fill_many(self, fields):
        if isinstance(fields, dict):  # tolerate {"e5": "x"} from sloppy tool calls
            fields = [{"element_id": k, "value": v} for k, v in fields.items()]
        for f in fields:
            self.browser.fill(f["element_id"], str(f["value"]))
        return ToolOutput("OK. " + "\n".join(self.browser._fmt(self.browser.element(f["element_id"])) for f in fields))

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