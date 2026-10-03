import json
from types import SimpleNamespace as NS

from worker.llm import GroqLLM, to_openai_messages
from worker.tools import TOOL_SCHEMAS


def test_message_conversion_roundtrip():
    msgs = [{"role": "user", "content": "TASK: x"},
            {"role": "assistant", "content": [{"type": "text", "text": "plan"},
                                               {"type": "tool_use", "id": "c1", "name": "open_url", "input": {"url": "u"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "PAGE", "is_error": False}]}]
    out = to_openai_messages("SYS", msgs)
    assert [m["role"] for m in out] == ["system", "user", "assistant", "tool"]
    assert out[2]["tool_calls"][0]["function"]["arguments"] == json.dumps({"url": "u"})
    assert out[3] == {"role": "tool", "tool_call_id": "c1", "content": "PAGE"}


def fake_client(responses):
    calls = []

    def create(**kw):
        calls.append(kw)
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    return NS(chat=NS(completions=NS(create=create))), calls


def resp(content=None, tool=None):
    tcs = [NS(id="c9", function=NS(name=tool[0], arguments=tool[1]))] if tool else None
    return NS(choices=[NS(message=NS(content=content, tool_calls=tcs))])


def test_groq_parses_tool_call_and_flags_malformed(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    client, calls = fake_client([resp("thinking", ("click", '{"element_id": "e3"}'))])
    r = GroqLLM(client=client).complete("S", [{"role": "user", "content": "t"}], TOOL_SCHEMAS)
    assert r.tool_calls[0]["input"] == {"element_id": "e3"} and r.text == "thinking"
    assert calls[0]["parallel_tool_calls"] is False and calls[0]["tools"][0]["type"] == "function"
    client, _ = fake_client([resp(None, ("click", "{bad json"))])
    r = GroqLLM(client=client).complete("S", [{"role": "user", "content": "t"}], TOOL_SCHEMAS)
    assert "_malformed_arguments" in r.tool_calls[0]["input"]


def test_groq_resamples_on_tool_use_failed(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    err = Exception("tool_use_failed"); err.status_code = 400
    client, calls = fake_client([err, resp(None, ("finish", "{}"))])
    r = GroqLLM(client=client).complete("S", [{"role": "user", "content": "t"}], TOOL_SCHEMAS)
    assert len(calls) == 2 and r.tool_calls[0]["name"] == "finish"
