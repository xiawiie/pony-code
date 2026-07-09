"""Ollama provider client."""

import json
import urllib.error
import urllib.request

from .message_utils import strip_pico_meta
from .openai_chat import _content_to_text
from .response import Response, StopReason


def _flatten_request(system, messages):
    parts = []
    system_text = _content_to_text(system)
    if system_text:
        parts.append(system_text)
    for message in strip_pico_meta(messages):
        text = _content_to_text(message.get("content", ""))
        if text:
            parts.append(f"[{message.get('role', 'user')}] {text}")
    return "\n\n".join(parts)


class OllamaGenerateAdapter:
    supports_native_tools = False
    supports_prompt_cache = False

    def __init__(self, model, base_url=None, api_key="", temperature=0.2, top_p=0.9, timeout=300, host=None):
        self.model = model
        if base_url is None:
            base_url = host
        if base_url is None:
            raise TypeError("OllamaGenerateAdapter requires base_url")
        self.base_url = str(base_url).rstrip("/")
        self.host = self.base_url
        self.temperature = temperature
        self.top_p = top_p
        self.timeout = timeout
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens, **kwargs):
        # Ollama 当前不支持我们这里接入的 prompt cache 语义，
        # 所以 runtime 传下来的缓存参数会被忽略。
        self.last_completion_metadata = {}
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "raw": False,
            "think": False,
            "options": {
                "num_predict": max_new_tokens,
                "temperature": self.temperature,
                "top_p": self.top_p,
            },
        }
        request = urllib.request.Request(
            self.host + "/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Ollama request failed with HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                "Could not reach Ollama.\n"
                "Make sure `ollama serve` is running and the model is available.\n"
                f"Host: {self.host}\n"
                f"Model: {self.model}"
            ) from exc

        if data.get("error"):
            raise RuntimeError(f"Ollama error: {data['error']}")
        return data.get("response", "")

    def stream_complete(self, prompt, max_new_tokens, **kwargs):
        yield self.complete(prompt, max_new_tokens, **kwargs)

    def complete_v2(self, *, system, tools, messages, max_tokens, cache_breakpoints=None):
        raw = self.complete(_flatten_request(system, messages), max_tokens)
        usage = dict(self.last_completion_metadata)
        return Response(
            stop_reason=StopReason.END_TURN,
            content=[{"type": "text", "text": str(raw)}],
            usage=usage,
        )
