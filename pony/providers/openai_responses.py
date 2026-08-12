"""OpenAI Responses native provider adapter."""

from copy import deepcopy
import json
import urllib.request

from .transport import (
    ProviderTransportError,
    _decode_json_object,
    _extract_usage_cache_details,
    _open_provider_request,
    _provider_auth_headers,
    _provider_protocol_error,
    _model_binding,
    _model_runtime_metadata,
    _record_effective_model,
    _resource_url,
    _validate_number,
)
from .response import Response, StopReason
from .openai_wire import (
    OPENAI_USER_AGENT,
    drop_optional_null_arguments,
    prepare_function_tools,
    render_system_instructions,
)


MAX_PROVIDER_STATE_ITEMS = 32
MAX_PROVIDER_STATE_BYTES = 1024 * 1024


def _responses_tools(tools, *, strict):
    prepared, optional_by_name = prepare_function_tools(tools, strict=strict)
    return [
        {
            "type": "function",
            **tool,
        }
        for tool in prepared
    ], optional_by_name


def _validated_provider_state(value):
    if value in (None, (), []):
        return []
    if not isinstance(value, (list, tuple)) or len(value) > MAX_PROVIDER_STATE_ITEMS:
        raise _provider_protocol_error(
            "OpenAI",
            stage="response_decode",
            reason="response_shape_invalid",
        )
    prepared = []
    for item in value:
        if not isinstance(item, dict) or item.get("type") != "reasoning":
            raise _provider_protocol_error(
                "OpenAI",
                stage="response_decode",
                reason="response_shape_invalid",
            )
        encrypted = item.get("encrypted_content")
        if not isinstance(encrypted, str) or not encrypted:
            raise _provider_protocol_error(
                "OpenAI",
                stage="response_decode",
                reason="response_shape_invalid",
            )
        if any(
            key
            not in {
                "id",
                "type",
                "encrypted_content",
                "summary",
                "content",
                "status",
            }
            for key in item
        ):
            raise _provider_protocol_error(
                "OpenAI",
                stage="response_decode",
                reason="response_shape_invalid",
            )
        if "content" in item and not isinstance(item["content"], list):
            raise _provider_protocol_error(
                "OpenAI",
                stage="response_decode",
                reason="response_shape_invalid",
            )
        copied = deepcopy(item)
        try:
            encoded = json.dumps(copied, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError):
            raise _provider_protocol_error(
                "OpenAI",
                stage="response_decode",
                reason="response_shape_invalid",
            ) from None
        prepared.append((copied, len(encoded)))
    if sum(size for _item, size in prepared) > MAX_PROVIDER_STATE_BYTES:
        raise _provider_protocol_error(
            "OpenAI",
            stage="response_decode",
            reason="response_shape_invalid",
        )
    return [item for item, _size in prepared]


def _canonical_input(messages, *, replay_reasoning):
    output = []
    for message in list(messages or []):
        if not isinstance(message, dict):
            raise ValueError("message must be an object")
        role = message.get("role")
        content = message.get("content")
        if isinstance(content, str):
            if role not in {"user", "assistant"}:
                raise ValueError("invalid message role")
            output.append({"role": role, "content": content})
            continue
        if not isinstance(content, list) or len(content) != 1:
            raise ValueError("canonical tool message must have one block")
        block = content[0]
        if not isinstance(block, dict):
            raise ValueError("message block must be an object")
        if role == "assistant" and block.get("type") == "tool_use":
            if replay_reasoning:
                output.extend(
                    _validated_provider_state(message.get("_pony_provider_state", []))
                )
            output.append(
                {
                    "type": "function_call",
                    "call_id": block.get("id"),
                    "name": block.get("name"),
                    "arguments": json.dumps(
                        block.get("input"),
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                }
            )
            continue
        if role == "user" and block.get("type") == "tool_result":
            output.append(
                {
                    "type": "function_call_output",
                    "call_id": block.get("tool_use_id"),
                    "output": str(block.get("content", "")),
                }
            )
            continue
        raise ValueError("unsupported canonical message")
    return output


def _response_content(data, *, optional_by_name, preserve_reasoning):
    output = data.get("output")
    if not isinstance(output, list) or not all(
        isinstance(item, dict) for item in output
    ):
        raise ValueError("output must be a list of objects")
    content = []
    provider_state = []
    refusal = False
    for item in output:
        item_type = item.get("type")
        if item_type == "reasoning":
            encrypted = item.get("encrypted_content")
            if preserve_reasoning and encrypted is not None:
                provider_state.extend(_validated_provider_state([item]))
            continue
        if item_type == "function_call":
            name = item.get("name")
            call_id = item.get("call_id")
            arguments = item.get("arguments")
            if (
                not isinstance(name, str)
                or not name
                or not isinstance(call_id, str)
                or not call_id
            ):
                raise _provider_protocol_error(
                    "OpenAI",
                    stage="tool_call",
                    reason="tool_call_shape_invalid",
                )
            if not isinstance(arguments, str):
                raise _provider_protocol_error(
                    "OpenAI",
                    stage="tool_call",
                    reason="tool_arguments_invalid",
                )
            try:
                parsed = json.loads(arguments)
            except (TypeError, ValueError):
                raise _provider_protocol_error(
                    "OpenAI",
                    stage="tool_call",
                    reason="tool_arguments_invalid",
                ) from None
            if not isinstance(parsed, dict):
                raise _provider_protocol_error(
                    "OpenAI",
                    stage="tool_call",
                    reason="tool_arguments_invalid",
                )
            drop_optional_null_arguments(parsed, optional_by_name.get(name, set()))
            content.append(
                {
                    "type": "tool_use",
                    "id": call_id,
                    "name": name,
                    "input": parsed,
                }
            )
            continue
        if item_type != "message":
            raise ValueError("unsupported output item")
        blocks = item.get("content")
        if not isinstance(blocks, list) or not all(
            isinstance(block, dict) for block in blocks
        ):
            raise ValueError("message content must be a list")
        for block in blocks:
            block_type = block.get("type")
            if block_type == "output_text":
                text = block.get("text")
                if not isinstance(text, str):
                    raise ValueError("output text must be text")
                content.append({"type": "text", "text": text})
            elif block_type == "refusal":
                text = block.get("refusal")
                if not isinstance(text, str):
                    raise ValueError("refusal must be text")
                content.append({"type": "text", "text": text})
                refusal = True
            else:
                raise ValueError("unsupported message content")
    return content, provider_state, refusal


def _stop_reason(data, content, refusal):
    status = data.get("status")
    if status is not None and not isinstance(status, str):
        raise ValueError("status must be a string")
    incomplete = data.get("incomplete_details")
    if incomplete is not None and not isinstance(incomplete, dict):
        raise ValueError("incomplete details must be an object")
    reason = (incomplete or {}).get("reason")
    if reason == "max_output_tokens":
        return StopReason.MAX_TOKENS
    if reason == "content_filter" or refusal:
        return StopReason.REFUSAL
    if incomplete is not None or status == "incomplete":
        return StopReason.UNKNOWN
    if status not in {None, "completed"}:
        return StopReason.UNKNOWN
    if any(block.get("type") == "tool_use" for block in content):
        return StopReason.TOOL_USE
    return StopReason.END_TURN


class OpenAIResponsesModelClient:
    def __init__(
        self,
        model,
        base_url,
        api_key,
        temperature,
        timeout,
        *,
        auth_mode=None,
        capabilities=None,
    ):
        from pony.config.model import validate_api_base

        self.model = str(model)
        self.base_url = validate_api_base(base_url)
        self.api_key = str(api_key or "")
        self.auth_mode = auth_mode or "bearer"
        self.capabilities = dict(capabilities or {})
        self.temperature = (
            None
            if temperature is None
            else _validate_number("temperature", temperature, minimum=0, maximum=2)
        )
        self.timeout = _validate_number("timeout", timeout, minimum=0.001)
        self.supports_prompt_cache = False
        self.last_completion_metadata = {}
        self.last_transport_attempts = 0
        self.provider_binding = _model_binding(
            "openai_responses",
            self.model,
            self.base_url,
        )
        self.provider_metadata = _model_runtime_metadata(
            "openai_responses",
            self.model,
        )

    def complete(
        self,
        *,
        system,
        tools,
        messages,
        max_tokens,
        cache_breakpoints=None,
    ):
        del cache_breakpoints
        self.last_completion_metadata = {}
        self.last_transport_attempts = 0
        _validate_number("max_tokens", max_tokens, minimum=1, integer=True)
        auth_headers = _provider_auth_headers(
            self.base_url,
            self.api_key,
            auth_mode=self.auth_mode,
            family="OpenAI",
        )
        strict = bool(self.capabilities.get("strict_tools"))
        prepared_tools, optional_by_name = _responses_tools(
            tools,
            strict=strict,
        )
        replay_reasoning = bool(self.capabilities.get("reasoning_replay"))
        payload = {
            "model": self.model,
            "instructions": render_system_instructions(system),
            "input": _canonical_input(
                messages,
                replay_reasoning=replay_reasoning,
            ),
            "max_output_tokens": max_tokens,
            "store": False,
            "stream": False,
        }
        if prepared_tools:
            payload["tools"] = prepared_tools
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.capabilities.get("parallel_tool_control"):
            payload["parallel_tool_calls"] = False
        if replay_reasoning:
            payload["include"] = ["reasoning.encrypted_content"]
        request = urllib.request.Request(
            _resource_url(self.base_url, "responses"),
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": OPENAI_USER_AGENT,
                **auth_headers,
            },
            method="POST",
        )
        response_body, response_headers = _open_provider_request(
            self,
            request,
            family="OpenAI",
            retryable=True,
        )
        try:
            data = _decode_json_object(response_body)
            if "choices" in data or data.get("error") or data.get("status") == "failed":
                raise ValueError("not a successful Responses object")
            content, provider_state, refusal = _response_content(
                data,
                optional_by_name=optional_by_name,
                preserve_reasoning=replay_reasoning,
            )
            usage = _extract_usage_cache_details(data)
            _record_effective_model(self, data)
            request_id = response_headers.get("x-request-id") or data.get("id")
            if isinstance(request_id, str) and request_id:
                usage["request_id"] = request_id
            response = Response(
                stop_reason=_stop_reason(data, content, refusal),
                content=content,
                usage=usage,
                provider_state=provider_state,
            )
        except ProviderTransportError:
            raise
        except Exception:
            raise _provider_protocol_error(
                "OpenAI",
                stage="response_decode",
                reason="response_shape_invalid",
            ) from None
        self.last_completion_metadata = usage
        return response
