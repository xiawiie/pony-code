"""Ollama provider client."""

import json
import urllib.error
import urllib.request

from ._shared import _decode_json_object, _optional_int


class OllamaModelClient:
    def __init__(self, model, host, temperature, top_p, timeout):
        from pico.config import validate_provider_base_url

        self.model = model
        self.host = validate_provider_base_url(host).rstrip("/")
        self.temperature = temperature
        self.top_p = top_p
        self.timeout = timeout
        self.last_completion_metadata = {}

    def complete_text(self, prompt, max_tokens):
        self.last_completion_metadata = {}
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "raw": False,
            "think": False,
            "options": {
                "num_predict": max_tokens,
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
        except (urllib.error.URLError, TimeoutError):
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
        input_tokens = _optional_int(data.get("prompt_eval_count"))
        output_tokens = _optional_int(data.get("eval_count"))
        total_tokens = (
            input_tokens + output_tokens
            if input_tokens is not None and output_tokens is not None
            else None
        )
        self.last_completion_metadata = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "cached_tokens": 0,
            "cache_hit": False,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
        return response_text
