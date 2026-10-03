#!/usr/bin/env python3
"""CLI: python run.py "<natural language task>" [--yes] [--no-mock]"""
import argparse
import sys
import time
from pathlib import Path

from worker.agent import Agent
from worker.browser import Browser
from worker.io import ConsoleIO
from worker.llm import make_llm
from worker.tools import Toolbox


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("task")
    ap.add_argument("--env", default="environment.md", help="environment briefing (systems, URLs, credentials)")
    ap.add_argument("--allow-host", action="append", help="hosts the browser may reach (default: 127.0.0.1)")
    ap.add_argument("--yes", action="store_true", help="auto-approve WRITE actions (non-interactive)")
    ap.add_argument("--max-steps", type=int, default=40)
    ap.add_argument("--provider", choices=["groq", "anthropic"], default="groq")
    ap.add_argument("--model", help="default: llama-3.3-70b-versatile (groq)")
    ap.add_argument("--no-mock", action="store_true", help="do not start the bundled mock company")
    ap.add_argument("--port", type=int, default=5001)
    a = ap.parse_args()

    base = f"http://127.0.0.1:{a.port}"
    app = None
    if not a.no_mock:
        from mockcorp.app import serve
        _, app = serve(a.port)
    env = Path(a.env).read_text().replace("{BASE}", base)
    run_dir = Path("runs") / time.strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True)

    io = ConsoleIO(auto_approve=a.yes)
    tb = Toolbox(Browser(a.allow_host or ["127.0.0.1"]), io, "workspace", run_dir)
    res = Agent(make_llm(a.provider, a.model), tb, env, io, a.max_steps).run(a.task)

    print(f"\n{'=' * 60}\nSTATUS : {res.status.upper()}  ({res.steps} steps)\nSUMMARY: {res.summary}\nEVIDENCE:")
    for e in res.evidence:
        print(f"  - {e}")
    if app is not None:
        print(f"\nGround truth (mock ERP bills): {app.state['bills']}")
    print(f"Trace: {res.run_dir}/trace.jsonl")
    sys.exit(0 if res.status == "done" else 1)


if __name__ == "__main__":
    main()
