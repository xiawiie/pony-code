"""Ollama provider client."""

import json
import urllib.request

from ._shared import (
    _decode_json_object,
    _open_provider_request,
    _optional_int,
    _validate_number,
)
from .response import StopReason


class OllamaModelClient:
    def __init__(self, model, host, temperature, top_p, timeout):
        from pico.config import validate_provider_base_url

        self.model = model
        self.host = validate_provider_base_url(host).rstrip("/")
        self.temperature = _validate_number("temperature", temperature, minimum=0)
        self.top_p = _validate_number("top_p", top_p, minimum=0, maximum=1)
        self.timeout = _validate_number("timeout", timeout, minimum=0.001)
        self.last_completion_metadata = {}
        self.last_transport_attempts = 0
        self.last_stop_reason = StopReason.END_TURN

    def complete_text(self, prompt, max_tokens):
        self.last_completion_metadata = {}
        self.last_transport_attempts = 0
        self.last_stop_reason = StopReason.END_TURN
        _validate_number("max_tokens", max_tokens, minimum=1, integer=True)
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
        response_body, _ = _open_provider_request(
            self,
            request,
            family="Ollama",
            retryable=False,
        )

        try:
            data = _decode_json_object(response_body)
        except Exception:
            raise RuntimeError("Ollama error: invalid_response") from None

        if data.get("error"):
            raise RuntimeError("Ollama error: backend_error") from None
        if data.get("done") is not True:
            raise RuntimeError("Ollama error: invalid_response") from None
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
        done_reason = data.get("done_reason")
        if done_reason == "length":
            self.last_stop_reason = StopReason.MAX_TOKENS
        elif done_reason in {None, "stop"}:
            self.last_stop_reason = StopReason.END_TURN
        else:
            self.last_stop_reason = StopReason.UNKNOWN
        return response_text
