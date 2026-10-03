"""Minimal text browser: HTTP + HTML parsing, exposing each page as text plus numbered, actionable elements.
Swappable for a Playwright-backed implementation exposing the same methods."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

MAX_TEXT = 3500
MAX_ELEMENTS = 70


class BrowserError(Exception):
    pass


@dataclass
class Element:
    id: str
    kind: str  # link | input | select | button
    label: str
    name: str = ""
    href: str = ""
    input_type: str = ""
    options: list = field(default_factory=list)  # [(value, text)]
    risk: str = ""  # "write" => state-changing submit (needs approval)
    tag: object = None


class Browser:
    def __init__(self, allowed_hosts, timeout=10, get_retries=2, backoff=0.3):
        self.allowed = set(allowed_hosts)
        self.timeout, self.get_retries, self.backoff = timeout, get_retries, backoff
        self.http = requests.Session()
        self.http.headers["User-Agent"] = "ai-task-worker/0.1"
        self.url, self.status, self.title, self.soup = "", 0, "", None
        self.elements: dict[str, Element] = {}
        self.fills: dict[str, str] = {}
        self._tag2id: dict[int, str] = {}

    # ------------------------------------------------------------------ network
    def _check(self, url):
        host = urlparse(url).hostname
        if host not in self.allowed:
            raise BrowserError(f"Blocked: host {host!r} is not in the allow-list {sorted(self.allowed)}")

    def _request(self, method, url, data=None):
        """Idempotent GETs are retried with backoff on 5xx/network errors. POSTs are never auto-retried
        (a retried write can duplicate a record) -- the agent decides after verifying state."""
        self._check(url)
        attempts = self.get_retries + 1 if method == "GET" else 1
        err = None
        for i in range(attempts):
            try:
                kw = {"params": data} if method == "GET" else {"data": data}
                r = self.http.request(method, url, timeout=self.timeout, **kw)
                if r.status_code >= 500 and i < attempts - 1:
                    time.sleep(self.backoff * 2 ** i)
                    continue
                self._check(r.url)
                return r
            except requests.RequestException as e:
                err = e
                if i < attempts - 1:
                    time.sleep(self.backoff * 2 ** i)
        raise BrowserError(f"Network error on {method} {url}: {err}")

    def _load(self, r):
        self.url, self.status = r.url, r.status_code
        self.soup = BeautifulSoup(r.text, "html.parser")
        self.title = self.soup.title.get_text(strip=True) if self.soup.title else ""
        self.fills = {}
        self._index()

    # ------------------------------------------------------------------ actions
    def open(self, url):
        url = urljoin(self.url, url) if self.url else url
        self._load(self._request("GET", url))
        return self.snapshot()

    def element(self, eid):
        if eid not in self.elements:
            raise BrowserError(f"No element {eid!r} on the current page. Ids are renumbered on every page load; "
                               "use ids from the most recent page snapshot.")
        return self.elements[eid]

    def fill(self, eid, value):
        el = self.element(eid)
        if el.kind == "select":
            for v, t in el.options:
                if value.strip().lower() in (v.lower(), t.lower()):
                    self.fills[eid] = v
                    return
            raise BrowserError(f"{eid} has no option {value!r}. Options: {[t for _, t in el.options]}")
        if el.kind != "input":
            raise BrowserError(f"{eid} is a {el.kind} and cannot be filled")
        self.fills[eid] = value

    def preview_submit(self, el):
        """(method, url, data) that clicking this button would send."""
        form = el.tag.find_parent("form")
        method = (form.get("method") or "get").upper()
        action = urljoin(self.url, form.get("action") or self.url)
        data = {}
        for c in form.find_all(["input", "select", "textarea"]):
            name = c.get("name")
            if not name:
                continue
            t = (c.get("type") or "text").lower()
            if c.name == "input" and t in ("submit", "button", "image", "checkbox", "radio", "file"):
                continue
            eid = self._tag2id.get(id(c))
            if eid in self.fills:
                data[name] = self.fills[eid]
            elif c.name == "select":
                opt = c.find("option", selected=True) or c.find("option")
                data[name] = (opt.get("value", opt.get_text(strip=True)) if opt else "")
            elif c.name == "textarea":
                data[name] = c.get_text()
            else:
                data[name] = c.get("value", "")
        if el.tag.get("name"):
            data[el.tag["name"]] = el.tag.get("value", "")
        return method, action, data

    def click(self, el):
        if el.kind == "link":
            self._load(self._request("GET", el.href))
        elif el.kind == "button":
            m, u, d = self.preview_submit(el)
            self._load(self._request(m, u, d))
        else:
            raise BrowserError(f"{el.id} is a {el.kind}; use fill() for form fields")
        return self.snapshot()

    # ----------------------------------------------------------------- indexing
    def _label(self, tag):
        if tag.get("id"):
            lab = self.soup.find("label", attrs={"for": tag["id"]})
            if lab:
                return lab.get_text(" ", strip=True)
        p = tag.find_parent("label")
        if p:
            return p.get_text(" ", strip=True)
        return tag.get("placeholder") or tag.get("aria-label") or tag.get("name") or ""

    def _index(self):
        self.elements, self._tag2id, n = {}, {}, 0
        for tag in self.soup.find_all(["a", "input", "select", "textarea", "button"]):
            form = tag.find_parent("form")
            el = None
            t = (tag.get("type") or "").lower()
            if tag.name == "a":
                href = tag.get("href", "")
                if not href or href.startswith(("#", "javascript:", "mailto:")):
                    continue
                el = Element("", "link", tag.get_text(" ", strip=True) or href, href=urljoin(self.url, href))
            elif tag.name == "input" and t in ("hidden", "checkbox", "radio", "file"):
                continue
            elif (tag.name == "input" and t in ("submit", "image")) or (tag.name == "button" and t != "button"):
                if not form:
                    continue
                label = tag.get("value") if tag.name == "input" else tag.get_text(" ", strip=True)
                risk = tag.get("data-risk", "")
                if not risk and (form.get("method") or "get").lower() == "post" and not form.find("input", {"type": "password"}):
                    risk = "write"  # generic policy: non-login POST == state change
                el = Element("", "button", label or "submit", risk=risk)
            elif tag.name in ("input", "textarea"):
                el = Element("", "input", self._label(tag), name=tag.get("name", ""), input_type=t or "text")
            elif tag.name == "select":
                opts = [(o.get("value", o.get_text(strip=True)), o.get_text(strip=True)) for o in tag.find_all("option")]
                el = Element("", "select", self._label(tag), name=tag.get("name", ""), options=opts)
            if el is None:
                continue
            n += 1
            el.id, el.tag = f"e{n}", tag
            self.elements[el.id] = el
            self._tag2id[id(tag)] = el.id

    # ----------------------------------------------------------------- snapshot
    def _page_text(self):
        soup = BeautifulSoup(str(self.soup), "html.parser")
        for t in soup(["script", "style", "head", "select", "textarea", "button"]):
            t.decompose()
        for tr in soup.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            tr.replace_with(soup.new_string("\n" + " | ".join(cells) + "\n"))
        lines = (re.sub(r"\s+", " ", ln).strip() for ln in soup.get_text("\n").splitlines())
        text = "\n".join(ln for ln in lines if ln)
        return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + "\n...[page text truncated]"

    def _fmt(self, el):
        if el.kind == "link":
            u = urlparse(el.href)
            return f'[{el.id}] link "{el.label}" -> {u.path}{"?" + u.query if u.query else ""}'
        if el.kind == "button":
            return f'[{el.id}] button "{el.label}"' + (" [WRITE: changes data, needs approval]" if el.risk == "write" else "")
        if el.kind == "select":
            cur = self.fills.get(el.id, el.options[0][0] if el.options else "")
            return f'[{el.id}] select "{el.label}" name={el.name} options={[t for _, t in el.options]} value="{cur}"'
        cur = self.fills.get(el.id, el.tag.get("value", ""))
        cur = "*****" if el.input_type == "password" and cur else cur
        return f'[{el.id}] input "{el.label}" name={el.name} type={el.input_type} value="{cur}"'

    def snapshot(self):
        if self.soup is None:
            return "(no page open)"
        flag = "" if self.status < 400 else "  <-- ERROR RESPONSE"
        out = [f"URL: {self.url}", f"STATUS: {self.status}{flag}", f"TITLE: {self.title}",
               "--- PAGE TEXT ---", self._page_text(), "--- ELEMENTS ---"]
        els = list(self.elements.values())
        out += [self._fmt(e) for e in els[:MAX_ELEMENTS]]
        if len(els) > MAX_ELEMENTS:
            out.append(f"...[{len(els) - MAX_ELEMENTS} more elements not shown]")
        return "\n".join(out)
