#!/usr/bin/env python3
"""CLI: python run.py "<natural language task>" [--yes] [--no-mock]"""
import argparse
import sys
import time
from pathlib import Path

from worker.agent import Agent
from worker.browser import Browser
from worker.io import ConsoleIO
from worker.critic import Critic
from worker.llm import make_llm
from worker.tools import Toolbox


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("task", nargs="?")
    ap.add_argument("--list-models", action="store_true", help="print Groq models your key can use, then exit")
    ap.add_argument("--env", default="environment.md", help="environment briefing (systems, URLs, credentials)")
    ap.add_argument("--allow-host", action="append", help="hosts the browser may reach (default: 127.0.0.1)")
    ap.add_argument("--yes", action="store_true", help="auto-approve WRITE actions (non-interactive)")
    ap.add_argument("--max-steps", type=int, default=60)
    ap.add_argument("--provider", choices=["groq", "anthropic"], default="groq")
    ap.add_argument("--model", help="Groq model id; default: auto-pick an available one (see --list-models)")
    ap.add_argument("--no-mock", action="store_true", help="do not start the bundled mock company")
    ap.add_argument("--no-critic", action="store_true", help="disable the independent pre-write reviewer")
    ap.add_argument("--port", type=int, default=5001)
    a = ap.parse_args()
    if a.list_models:
        print("\n".join(make_llm("groq").available_models()))
        return
    if not a.task:
        ap.error("task is required")

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
    try:
        llm = make_llm(a.provider, a.model)
        res = Agent(llm, tb, env, io, a.max_steps, critic=None if a.no_critic else Critic(llm)).run(a.task)
    except KeyboardInterrupt:
        print(f"\nInterrupted. Partial trace: {run_dir}/trace.jsonl")
        sys.exit(130)
    except Exception as e:  # e.g. quota exhausted on every model; show a clean message, keep the trace
        print(f"\nRun aborted: {type(e).__name__}: {str(e)[:400]}\nPartial trace: {run_dir}/trace.jsonl")
        if app is not None:
            print(f"Ground truth (mock ERP bills): {app.state['bills']}")
        sys.exit(2)

    print(f"\n{'=' * 60}\nSTATUS : {res.status.upper()}  ({res.steps} steps)\nSUMMARY: {res.summary}\nEVIDENCE:")
    for e in res.evidence:
        print(f"  - {e}")
    if app is not None:
        print(f"\nGround truth (mock ERP bills): {app.state['bills']}")
    print(f"Trace: {res.run_dir}/trace.jsonl")
    sys.exit(0 if res.status == "done" else 1)


if __name__ == "__main__":
    main()