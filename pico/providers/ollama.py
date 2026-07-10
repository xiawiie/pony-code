"""Ollama provider client."""

import json
import urllib.error
import urllib.request

from ._shared import _decode_json_object


class OllamaModelClient:
    def __init__(self, model, host, temperature, top_p, timeout):
        from pico.config import validate_provider_base_url

        self.model = model
        self.host = validate_provider_base_url(host).rstrip("/")
        self.temperature = temperature
        self.top_p = top_p
        self.timeout = timeout
        self.supports_prompt_cache = False
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
                response_body = response.read()
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"Ollama request failed with HTTP {exc.code}"
            ) from None
        except urllib.error.URLError:
            raise RuntimeError("Ollama request failed: network_error") from None

        try:
            data = _decode_json_object(response_body)
        except Exception:
            raise RuntimeError("Ollama error: invalid_response") from None

        if data.get("error"):
            raise RuntimeError("Ollama error: backend_error") from None
        response_text = data.get("response")
        if not isinstance(response_text, str):
            raise RuntimeError("Ollama error: invalid_response") from None
        return response_text

    def stream_complete(self, prompt, max_new_tokens, **kwargs):
        yield self.complete(prompt, max_new_tokens, **kwargs)
