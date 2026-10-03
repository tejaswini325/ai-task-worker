"""Human-in-the-loop channel: logging, clarification questions, write approvals."""
import sys


class ConsoleIO:
    def __init__(self, auto_approve=False, quiet=False):
        self.auto_approve, self.quiet = auto_approve, quiet

    def log(self, msg):
        if not self.quiet:
            print(msg, flush=True)

    def ask(self, question):
        print(f"\n\033[33m? Agent asks:\033[0m {question}")
        return input("  your answer> ").strip()

    def approve(self, description):
        print(f"\n\033[33m! Approval needed\033[0m\n  {description}")
        if self.auto_approve:
            print("  (auto-approved via --yes)")
            return True
        return input("  approve? [y/N]> ").strip().lower() in ("y", "yes")
