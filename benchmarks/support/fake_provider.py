"""Structured fake provider for tests and offline benchmarks."""

from pico.providers.response import Response, StopReason


class FakeModelClient:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self._tool_index = 0
        self.requests = []
        self.supports_prompt_cache = False
        self.last_completion_metadata = {}
        self.last_transport_attempts = 0

    def complete(
        self,
        *,
        system,
        tools,
        messages,
        max_tokens,
        cache_breakpoints=None,
    ):
        self.requests.append(
            {
                "system": system,
                "tools": tools,
                "messages": messages,
                "max_tokens": max_tokens,
                "cache_breakpoints": list(cache_breakpoints or []),
            }
        )
        if not self.outputs:
            raise RuntimeError("fake model ran out of outputs")
        output = self.outputs.pop(0)
        if isinstance(output, Response):
            self.last_completion_metadata = dict(output.usage or {})
            return output
        if isinstance(output, dict):
            name = output.get("name")
            arguments = output.get("arguments", output.get("args"))
            self._tool_index += 1
            return Response(
                stop_reason=StopReason.TOOL_USE,
                content=[
                    {
                        "type": "tool_use",
                        "id": f"fake_call_{self._tool_index}",
                        "name": name,
                        "input": (
                            dict(arguments)
                            if isinstance(arguments, dict)
                            else arguments
                        ),
                    }
                ],
            )
        self.last_completion_metadata = {}
        return Response(
            stop_reason=StopReason.END_TURN,
            content=[{"type": "text", "text": str(output)}],
        )
