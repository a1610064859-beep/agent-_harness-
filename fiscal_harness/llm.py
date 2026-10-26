"""模型接入。默认 OpenAI 兼容的 chat/completions + tools 协议（DeepSeek、通义、主办方统一 Key 等），
换模型只改环境变量 LLM_BASE_URL / LLM_MODEL / LLM_API_KEY。"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Protocol


@dataclass
class LLMReply:
    message: dict          # assistant 消息（含 tool_calls / reasoning_content），原样放回下一步输入
    usage: dict
    model: str


class LLM(Protocol):
    model: str

    def chat(self, messages: list[dict], tools: list[dict]) -> LLMReply: ...


class LLMError(RuntimeError):
    pass


class OpenAICompat:
    def __init__(self, base_url: str, model: str, api_key: str, effort: str | None = None, timeout: int = 120):
        if not api_key:
            raise LLMError("没有配置模型密钥：请在 .env 中设置 LLM_API_KEY 或 DEEPSEEK_API_KEY")
        self.base_url, self.model, self._key, self.effort, self.timeout = base_url, model, api_key, effort, timeout

    def __repr__(self) -> str:  # 不暴露密钥
        return f"OpenAICompat({self.base_url!r}, {self.model!r})"

    def chat(self, messages: list[dict], tools: list[dict]) -> LLMReply:
        body = {"model": self.model, "messages": messages, "tools": tools, "temperature": 0.2}
        if self.effort:
            body["reasoning_effort"] = self.effort
        data = json.dumps(body, ensure_ascii=False).encode()
        for attempt in range(3):
            req = urllib.request.Request(f"{self.base_url}/chat/completions", data=data, headers={
                "Authorization": f"Bearer {self._key}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = json.load(resp)
                break
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:500]
                if e.code in (429, 500, 502, 503) and attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise LLMError(f"模型接口返回 {e.code}：{detail}") from None
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise LLMError(f"无法连接模型接口：{e}") from None
        msg = payload["choices"][0]["message"]
        out = {"role": "assistant", "content": msg.get("content") or ""}
        if msg.get("tool_calls"):
            out["tool_calls"] = [{"id": c["id"], "type": "function",
                                  "function": {"name": c["function"]["name"], "arguments": c["function"]["arguments"]}}
                                 for c in msg["tool_calls"]]
        if msg.get("reasoning_content"):
            out["reasoning_content"] = msg["reasoning_content"]
        return LLMReply(out, payload.get("usage") or {}, payload.get("model") or self.model)


class ScriptedLLM:
    """测试用：按顺序返回预设的回复，或由回调根据输入生成回复。不参与任何真实评测统计。"""

    model = "scripted"

    def __init__(self, steps: list[dict | Callable[[list[dict]], dict]]):
        self.steps = list(steps)
        self.calls: list[list[dict]] = []

    def chat(self, messages: list[dict], tools: list[dict]) -> LLMReply:
        self.calls.append(json.loads(json.dumps(messages)))
        step = self.steps.pop(0)
        msg = step(messages) if callable(step) else step
        return LLMReply({"role": "assistant", "content": "", **msg}, {}, self.model)


def tool_call(name: str, args: dict, call_id: str | None = None) -> dict:
    return {"id": call_id or f"call_{name}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


def from_settings(settings) -> LLM:
    if settings.llm_provider != "openai_compat":
        raise LLMError(f"暂不支持的 LLM_PROVIDER：{settings.llm_provider}")
    return OpenAICompat(settings.llm_base_url, settings.llm_model, settings.api_key, settings.llm_effort)
