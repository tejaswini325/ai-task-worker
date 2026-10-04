"""LLM adapter. The agent only depends on `complete(system, messages, tools) -> Reply`."""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass


@dataclass
class Reply:
    blocks: list  # Anthropic-style content blocks: {"type": "text"|"tool_use", ...}

    @property
    def tool_calls(self):
        return [b for b in self.blocks if b["type"] == "tool_use"]

    @property
    def text(self):
        return " ".join(b["text"] for b in self.blocks if b["type"] == "text")


class AnthropicLLM:
    def __init__(self, model=None, max_tokens=2000):
        import anthropic  # imported lazily so tests run without the SDK configured
        self.client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY; SDK retries 429/5xx itself
        self.model = model or os.getenv("WORKER_MODEL", "claude-sonnet-5-5")
        self.max_tokens = max_tokens

    def complete(self, system, messages, tools):
        kw = {"tools": tools, "tool_choice": {"type": "auto", "disable_parallel_tool_use": True}} if tools else {}
        r = self.client.messages.create(model=self.model, max_tokens=self.max_tokens, system=system,
                                        messages=messages, **kw)
        blocks = []
        for b in r.content:
            if b.type == "text" and b.text.strip():
                blocks.append({"type": "text", "text": b.text})
            elif b.type == "tool_use":
                blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
        return Reply(blocks)


def to_openai_messages(system, messages):
    """Internal history is Anthropic-style blocks; convert to OpenAI/Groq chat format."""
    out = [{"role": "system", "content": system}]
    for m in messages:
        c = m["content"]
        if isinstance(c, str):
            out.append({"role": m["role"], "content": c})
        elif m["role"] == "assistant":
            text = " ".join(b["text"] for b in c if b["type"] == "text")
            calls = [{"id": b["id"], "type": "function",
                      "function": {"name": b["name"], "arguments": json.dumps(b["input"])}}
                     for b in c if b["type"] == "tool_use"]
            msg = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
        else:
            for b in c:
                if b["type"] == "tool_result":
                    out.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": b["content"]})
                elif b["type"] == "text":
                    out.append({"role": "user", "content": b["text"]})
    return out


PREFERRED_GROQ_MODELS = ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.6-27b", "qwen/qwen3.8-27b",
                         "llama-3.3-70b-versatile", "llama-3.1-8b-instant"]  # tried in order; "groq/compound*" excluded on purpose
_THINK = re.compile(r"<think>.*?</think>", re.S)


class GroqLLM:
    """Groq (OpenAI-compatible) chat completions with tool calling. Free tier friendly.
    Model ids on Groq change often, so unless --model/WORKER_MODEL is given we pick the first preferred model
    that your key can actually see (client.models.list())."""

    def __init__(self, model=None, max_tokens=1500, client=None):
        if client is None:
            from groq import Groq
            client = Groq(max_retries=5)  # reads GROQ_API_KEY; SDK backs off on 429/5xx
        self.client = client
        self.model = model or os.getenv("WORKER_MODEL")
        self.max_tokens = max_tokens
        self.parallel_flag = True
        self.reasoning = True
        self.exhausted = set()  # models whose daily token quota is used up (each model has its own quota)

    def available_models(self):
        return sorted(m.id for m in self.client.models.list().data)

    def _pick_model(self):
        avail = self.available_models()
        for m in PREFERRED_GROQ_MODELS:
            if m in avail and m not in self.exhausted:
                print(f"[groq] using model: {m}", flush=True)
                return m
        raise RuntimeError(f"No usable model left. Preferred: {PREFERRED_GROQ_MODELS}; available: {avail}; "
                           f"daily quota exhausted on: {sorted(self.exhausted)}. Wait for the quota to reset, use another "
                           "key, or pass --model <id>.")

    def complete(self, system, messages, tools):
        oa_tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                       "parameters": t["input_schema"]}} for t in tools]
        if not self.model:
            self.model = self._pick_model()
        last = None
        for attempt in range(10):  # open models occasionally emit a malformed tool call -> just resample
            try:
                kw = {"parallel_tool_calls": False} if (self.parallel_flag and oa_tools) else {}
                if oa_tools:
                    kw["tools"], kw["tool_choice"] = oa_tools, "auto"
                if self.reasoning and "gpt-oss" in self.model:  # fewer reasoning tokens => faster, cheaper steps
                    kw["extra_body"] = {"reasoning_effort": "low"}
                r = self.client.chat.completions.create(
                    model=self.model, messages=to_openai_messages(system, messages),
                    max_tokens=self.max_tokens, temperature=0.2, **kw)
                break
            except Exception as e:
                last = e
                if getattr(e, "status_code", None) == 429:
                    msg_ = str(e)
                    if "per day" in msg_ or "TPD" in msg_ or "RPD" in msg_:  # daily quota gone: switch model
                        self.exhausted.add(self.model)
                        old, self.model = self.model, self._pick_model()
                        print(f"[groq] daily quota exhausted on {old}; switching to {self.model}", flush=True)
                        continue
                    m = re.search(r"try again in (?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?", msg_)  # per-minute limit: wait it out
                    wait = (int(m.group(1) or 0) * 60 + float(m.group(2) or 0)) if m else 20
                    print(f"[groq] rate limited; waiting {min(wait + 1, 65):.0f}s", flush=True)
                    time.sleep(min(wait + 1, 65))
                    continue
                if "reasoning_effort" in str(e):
                    self.reasoning = False
                    continue
                if "parallel_tool_calls" in str(e):  # some models reject the flag; drop it and retry
                    self.parallel_flag = False
                    continue
                if getattr(e, "status_code", None) in (400, 500, 503):
                    time.sleep(1 + attempt)
                    continue
                raise
        else:
            raise RuntimeError(f"Groq call failed after retries: {last}")
        msg = r.choices[0].message
        blocks = []
        text = _THINK.sub("", msg.content or "").strip()  # strip reasoning traces some models inline
        if text:
            blocks.append({"type": "text", "text": text})
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_malformed_arguments": tc.function.arguments}  # surfaces as a tool error the model can fix
            blocks.append({"type": "tool_use", "id": tc.id, "name": tc.function.name, "input": args})
        return Reply(blocks)


def make_llm(provider="groq", model=None):
    return GroqLLM(model) if provider == "groq" else AnthropicLLM(model)