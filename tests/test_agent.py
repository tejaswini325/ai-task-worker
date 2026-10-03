import json
import re

import pytest
import requests

from mockcorp.app import serve
from tests.helpers import ScriptedLLM, FakeIO, eid
from worker.agent import Agent, compact
from worker.browser import Browser, BrowserError
from worker.tools import Toolbox


@pytest.fixture
def world(tmp_path):
    srv, app = serve(0)
    base = f"http://127.0.0.1:{srv.server_port}"
    yield base, app
    srv.shutdown()


def run(policy, base, tmp_path, io, steps=60):
    tb = Toolbox(Browser(["127.0.0.1"], backoff=0.01), io, tmp_path / "ws", tmp_path)
    return Agent(ScriptedLLM(policy), tb, "env", io, steps).run("task"), tb


def login(base, area, user):
    o = yield ("open_url", {"url": f"{base}/{area}/login"})
    o = yield ("fill", {"element_id": eid(o, "name=username"), "value": user})
    o = yield ("fill", {"element_id": eid(o, "name=password"), "value": "demo-pass"})
    return (yield ("click", {"element_id": eid(o, '"Sign in"')}))


def read_latest_globex(base):
    yield from login(base, "vendor", "vendor")
    o = yield ("open_url", {"url": f"{base}/vendor/invoices?company=Globex&page=2"})
    o = yield ("click", {"element_id": eid(o, '"INV-1019"')})
    while "STATUS: 503" in o:  # agent-level recovery after the runtime's own GET retries are exhausted
        o = yield ("open_url", {"url": re.search(r"URL: (\S+)", o).group(1)})
    assert "$14,980.00" in o and "28 Oct 2026" in o
    yield ("remember", {"key": "invoice", "value": "INV-1019 14980.00 2026-10-28"})


def enter_bill(base, amount, due, first_try=None):
    yield from login(base, "erp", "admin")
    o = yield ("open_url", {"url": f"{base}/erp/bills/new"})
    attempts = [first_try, (amount, due)] if first_try else [(amount, due)]
    for amt, d in attempts:
        o = yield ("fill", {"element_id": eid(o, "name=vendor"), "value": "Globex Corporation"})
        o = yield ("fill", {"element_id": eid(o, "name=invoice_number"), "value": "INV-1019"})
        o = yield ("fill", {"element_id": eid(o, "name=amount"), "value": amt})
        o = yield ("fill", {"element_id": eid(o, "name=due_date"), "value": d})
        o = yield ("click", {"element_id": eid(o, '"Save bill"')})
    return o


def test_happy_path_with_recovery(world, tmp_path):
    base, app = world

    def policy():
        yield from read_latest_globex(base)
        o = yield from enter_bill(base, "14980.00", "2026-10-28", first_try=("$14,980.00", "28 Oct 2026"))
        assert "Bill saved: INV-1019" in o
        o = yield ("open_url", {"url": f"{base}/erp/bills"})
        yield ("finish", {"status": "done", "summary": "recorded", "evidence": ["INV-1019 | 14980.00 | 2026-10-28 listed"]})

    io = FakeIO()
    res, tb = run(policy(), base, tmp_path, io)
    assert res.status == "done"
    assert app.state["bills"] == [{"vendor": "Globex Corporation", "invoice_number": "INV-1019",
                                   "amount": 14980.0, "due_date": "2026-10-28"}]
    assert len(io.approvals) == 2  # failed validation attempt + the successful one, each gated
    assert tb.memory["invoice"].startswith("INV-1019")
    assert (tmp_path / "final_page.txt").exists() and (tmp_path / "trace.jsonl").exists()


def test_denied_write_never_happens(world, tmp_path):
    base, app = world

    def policy():
        yield from enter_bill(base, "14980.00", "2026-10-28")
        yield ("finish", {"status": "blocked", "summary": "user denied", "evidence": []})

    res, _ = run(policy(), base, tmp_path, FakeIO(approve=False))
    assert res.status == "blocked" and app.state["bills"] == []


def test_finish_rejected_until_verified(world, tmp_path):
    base, app = world
    seen = {}

    def policy():
        yield from enter_bill(base, "14980.00", "2026-10-28")
        seen["early"] = yield ("finish", {"status": "done", "summary": "x", "evidence": ["trust me"]})
        yield ("open_url", {"url": f"{base}/erp/bills"})
        yield ("finish", {"status": "done", "summary": "ok", "evidence": ["listed"]})

    res, _ = run(policy(), base, tmp_path, FakeIO())
    assert "REJECTED" in seen["early"] and res.status == "done"


def test_host_allowlist_blocks(world, tmp_path):
    base, _ = world
    seen = {}

    def policy():
        seen["o"] = yield ("open_url", {"url": "http://evil.example.com/"})
        yield ("finish", {"status": "failed", "summary": "blocked", "evidence": []})

    run(policy(), base, tmp_path, FakeIO())
    assert "Blocked" in seen["o"]


def test_get_retry_hides_transient_503(world):
    base, app = world
    b = Browser(["127.0.0.1"], backoff=0.01, get_retries=3)
    b.open(f"{base}/vendor/login")
    s = requests.Session()
    app.state["flake"] = 2  # two failures, third attempt succeeds -> invisible to the agent
    b.http = s
    b.http.post(f"{base}/vendor/login", data={"username": "vendor", "password": "demo-pass"})
    assert "STATUS: 200" in b.open(f"{base}/vendor/invoice/4")


def test_compaction_elides_old_observations():
    big = "x" * 1000
    msgs = [{"role": "user", "content": "TASK"}]
    for i in range(6):
        msgs += [{"role": "assistant", "content": [{"type": "tool_use", "id": str(i), "name": "t", "input": {}}]},
                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": str(i), "content": big}]}]
    c = compact(msgs, keep=3)
    lens = [len(m["content"][0]["content"]) for m in c if m["role"] == "user" and isinstance(m["content"], list)]
    assert lens[:3] < [400] * 3 and lens[-3:] == [1000] * 3
