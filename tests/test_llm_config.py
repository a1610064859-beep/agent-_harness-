"""模型配置：默认千问 Qwen3.8-Max（初赛官方跑测模型），只配 DeepSeek 密钥的旧环境仍走 DeepSeek。不调用模型。"""
import json

from fiscal_harness import config, llm

_KEYS = ("LLM_API_KEY", "DASHSCOPE_API_KEY", "DEEPSEEK_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "LLM_THINKING")
FAKE = "sk-" + "test" * 4


def _env(monkeypatch, **kv):
    for k in _KEYS:
        monkeypatch.delenv(k, raising=False)
    for k, v in kv.items():
        monkeypatch.setenv(k, v)
    return config.load_settings()


def test_default_is_qwen(monkeypatch):
    s = _env(monkeypatch, LLM_API_KEY=FAKE)
    assert (s.llm_base_url, s.llm_model) == (config.QWEN_BASE_URL, "qwen3.8-max")
    assert s.llm_thinking is None


def test_dashscope_key_name(monkeypatch):
    s = _env(monkeypatch, DASHSCOPE_API_KEY=FAKE)
    assert s.api_key == FAKE and s.llm_model == "qwen3.8-max"


def test_legacy_deepseek_only(monkeypatch):
    s = _env(monkeypatch, DEEPSEEK_API_KEY=FAKE)
    assert (s.llm_base_url, s.llm_model) == (config.DEEPSEEK_BASE_URL, config.DEEPSEEK_MODEL)


def test_explicit_endpoint_wins(monkeypatch):
    s = _env(monkeypatch, DEEPSEEK_API_KEY=FAKE, LLM_BASE_URL="https://example.com/v1/", LLM_MODEL="m")
    assert (s.llm_base_url, s.llm_model) == ("https://example.com/v1", "m")


def test_thinking_flag_sent(monkeypatch):
    s = _env(monkeypatch, LLM_API_KEY=FAKE, LLM_THINKING="0")
    client = llm.from_settings(s)
    sent = {}

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout):
        sent.update(json.loads(req.data))
        r = Resp()
        r.read = lambda *a: json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
        return r

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(llm.json, "load", lambda f: json.loads(f.read()))
    assert client.chat([{"role": "user", "content": "hi"}], []).message["content"] == "ok"
    assert sent["enable_thinking"] is False and sent["model"] == "qwen3.8-max"
    assert FAKE not in repr(client)
