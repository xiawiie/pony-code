import json
import re


def parse_model_output(raw):
    """Parse model text into a runtime action tuple."""
    raw = str(raw)
    if "<tool>" in raw and ("<final>" not in raw or raw.find("<tool>") < raw.find("<final>")):
        body = extract(raw, "tool")
        try:
            payload = json.loads(body)
        except Exception:
            return "retry", retry_notice("model returned malformed tool JSON")
        if not isinstance(payload, dict):
            return "retry", retry_notice("tool payload must be a JSON object")
        if not str(payload.get("name", "")).strip():
            return "retry", retry_notice("tool payload is missing a tool name")
        args = payload.get("args", {})
        if args is None:
            payload["args"] = {}
        elif not isinstance(args, dict):
            return "retry", retry_notice()
        return "tool", payload
    if "<tool" in raw and ("<final>" not in raw or raw.find("<tool") < raw.find("<final>")):
        payload = parse_xml_tool(raw)
        if payload is not None:
            return "tool", payload
        return "retry", retry_notice()
    if "<final>" in raw:
        final = extract(raw, "final").strip()
        if final:
            return "final", final
        return "retry", retry_notice("model returned an empty <final> answer")
    raw = raw.strip()
    if raw:
        return "final", raw
    return "retry", retry_notice("model returned an empty response")


def retry_notice(problem=None):
    prefix = "Runtime notice"
    if problem:
        prefix += f": {problem}"
    else:
        prefix += ": model returned malformed tool output"
    return (
        f"{prefix}. Reply with a valid <tool> call or a non-empty <final> answer. "
        'For multi-line files, prefer <tool name="write_file" path="file.py"><content>...</content></tool>.'
    )


def parse_xml_tool(raw):
    match = re.search(r"<tool(?P<attrs>[^>]*)>(?P<body>.*?)</tool>", raw, re.S)
    if match:
        return _parse_xml_tool_payload(match.group("attrs"), match.group("body"))

    match = re.search(r"<tool(?P<attrs>[^>]*)/\s*>", raw, re.S)
    if match:
        return _parse_xml_tool_payload(match.group("attrs"), "")
    return None


def _parse_xml_tool_payload(attrs_text, body):
    attrs = parse_attrs(attrs_text)
    name = str(attrs.pop("name", "")).strip()
    if not name:
        return None

    args = dict(attrs)
    for key in ("content", "old_text", "new_text", "command", "task", "pattern", "path"):
        if f"<{key}>" in body:
            args[key] = extract_raw(body, key)

    body_text = body.strip("\n")
    if name == "write_file" and "content" not in args and body_text:
        args["content"] = body_text
    if name == "delegate" and "task" not in args and body_text:
        args["task"] = body_text.strip()
    return {"name": name, "args": args}


def parse_attrs(text):
    attrs = {}
    for match in re.finditer(r"""([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""", text):
        attrs[match.group(1)] = match.group(2) if match.group(2) is not None else match.group(3)
    return attrs


def extract(text, tag):
    start_tag = f"<{tag}>"
    end_tag = f"</{tag}>"
    start = text.find(start_tag)
    if start == -1:
        return text
    start += len(start_tag)
    end = text.find(end_tag, start)
    if end == -1:
        return text[start:].strip()
    return text[start:end].strip()


def extract_raw(text, tag):
    start_tag = f"<{tag}>"
    end_tag = f"</{tag}>"
    start = text.find(start_tag)
    if start == -1:
        return text
    start += len(start_tag)
    end = text.find(end_tag, start)
    if end == -1:
        return text[start:]
    return text[start:end]
