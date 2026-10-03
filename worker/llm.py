"""LLM adapter. The agent only depends on `complete(system, messages, tools) -> Reply`."""
from __future__ import annotations

import os
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
