"""LLM adapter. The agent only depends on `complete(system, messages, tools) -> Reply`."""
from __future__ import annotations

import json
import os
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
        r = self.client.messages.create(
            model=self.model, max_tokens=self.max_tokens, system=system, messages=messages, tools=tools,
            tool_choice={"type": "auto", "disable_parallel_tool_use": True},  # actions are order-dependent
        )
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


class GroqLLM:
    """Groq (OpenAI-compatible) chat completions with tool calling. Free tier friendly."""

    def __init__(self, model=None, max_tokens=1500, client=None):
        if client is None:
            from groq import Groq
            client = Groq(max_retries=5)  # reads GROQ_API_KEY; SDK backs off on 429/5xx
        self.client = client
        self.model = model or os.getenv("WORKER_MODEL", "llama-3.3-70b-versatile")
        self.max_tokens = max_tokens

    def complete(self, system, messages, tools):
        oa_tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                       "parameters": t["input_schema"]}} for t in tools]
        last = None
        for attempt in range(4):  # open models occasionally emit a malformed tool call -> just resample
            try:
                r = self.client.chat.completions.create(
                    model=self.model, messages=to_openai_messages(system, messages), tools=oa_tools,
                    tool_choice="auto", parallel_tool_calls=False, max_tokens=self.max_tokens, temperature=0.2)
                break
            except Exception as e:
                last = e
                if getattr(e, "status_code", None) in (400, 500, 503):
                    time.sleep(1 + attempt)
                    continue
                raise
        else:
            raise RuntimeError(f"Groq call failed after retries: {last}")
        msg = r.choices[0].message
        blocks = []
        if msg.content and msg.content.strip():
            blocks.append({"type": "text", "text": msg.content})
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_malformed_arguments": tc.function.arguments}  # surfaces as a tool error the model can fix
            blocks.append({"type": "tool_use", "id": tc.id, "name": tc.function.name, "input": args})
        return Reply(blocks)


def make_llm(provider="groq", model=None):
    return GroqLLM(model) if provider == "groq" else AnthropicLLM(model)
