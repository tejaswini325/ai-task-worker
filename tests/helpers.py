import re
from worker.llm import Reply


def eid(obs, pattern):
    m = re.search(r"\[(e\d+)\][^\n]*" + pattern, obs)
    assert m, f"no element matching {pattern!r} in:\n{obs}"
    return m.group(1)


class ScriptedLLM:
    """Test double: replays a generator policy that sees each observation and yields (tool, args).
    Exercises the harness (retries, gates, verification) without needing an API key."""
    def __init__(self, gen):
        self.gen, self.n, self.page = gen, 0, ''

    def complete(self, system, messages, tools):
        obs = None
        last = messages[-1]["content"]
        if isinstance(last, list):
            obs = last[0]["content"]
            if obs.startswith("OK."):  # fill() echoes one element; the page (and its ids) is unchanged
                obs = self.page
            elif "--- ELEMENTS ---" in obs:
                self.page = obs
        name, args = self.gen.send(obs) if self.n else next(self.gen)
        self.n += 1
        return Reply([{"type": "tool_use", "id": f"t{self.n}", "name": name, "input": args}])


class FakeIO:
    def __init__(self, approve=True, answers=()):
        self.approvals, self.approve_value, self.answers, self.asked = [], approve, list(answers), []

    def log(self, m): pass
    def ask(self, q): self.asked.append(q); return self.answers.pop(0)
    def approve(self, d): self.approvals.append(d); return self.approve_value
