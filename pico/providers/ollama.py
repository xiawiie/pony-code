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
