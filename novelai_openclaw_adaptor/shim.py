import argparse
import json
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib import error, request

from .config import choose_text, ensure_config_file, ensure_persisted_api_key, get_language

SUPPORTED_TEXT_MODELS = [
    "glm-4-6",
    "erato",
    "kayra",
    "clio",
    "krake",
    "euterpe",
    "sigurd",
    "genji",
    "snek",
]

TOOL_CALL_MARKER = "<tool_call>"
TOOL_NAME_RE = re.compile(r"^[A-Za-z_][\w.-]*$")
TOOL_ARG_RE = re.compile(r"^([A-Za-z_][\w.-]*)\s*:\s*(.*)$")
PAREN_TOOL_CALL_RE = re.compile(r"\([^()\n]+\)")
FUNC_TOOL_CALL_RE = re.compile(r"^(?P<name>[A-Za-z_][\w.-]*)\s*\(\s*(?P<args>.*?)(?:\))?\s*$")
WRAPPED_FUNC_TOOL_CALL_RE = re.compile(r"\((?P<inner>[A-Za-z_][\w.-]*\([^()\n]*\))\)")
FILE_PATH_RE = re.compile(r"(?P<path>(?:[A-Za-z]:)?[A-Za-z0-9_./\\-]*[A-Za-z0-9_/-]\.(?:md|txt|json|toml|yaml|yml))")
DEBUG_ENV_VAR = "NOVELAI_SHIM_DEBUG"
ACTION_MODE_NATIVE = "native"
ACTION_MODE_SINGLE_STEP = "single-step"


def prompt_to_messages(prompt: Any) -> list[dict[str, Any]]:
    if isinstance(prompt, str):
        return [{"role": "user", "content": prompt}]
    if isinstance(prompt, list):
        parts = []
        for item in prompt:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and "text" in item:
                parts.append(str(item["text"]))
            else:
                parts.append(json.dumps(item, ensure_ascii=False))
        return [{"role": "user", "content": "\n".join(parts)}]
    return [{"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}]


def extract_choice_text(choice: dict[str, Any]) -> str:
    if choice.get("text") is not None:
        return choice.get("text", "")
    message = choice.get("message", {}) or {}
    return content_to_text(message.get("content", ""))


def content_to_text(content: Any) -> str:
    if isinstance(content, list):
        text_parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") in ("text", "input_text"):
                text_parts.append(part.get("text", ""))
            else:
                text_parts.append(str(part))
        return "".join(text_parts)
    return str(content)


def normalize_output_text(text: Any) -> str:
    if text is None:
        return ""
    return str(text).replace("\r\n", "\n").lstrip()


def collapse_repeated_tail(text: str, *, min_block_chars: int = 24) -> str:
    collapsed = text
    while len(collapsed) >= min_block_chars * 2:
        changed = False
        for block_len in range(len(collapsed) // 2, min_block_chars - 1, -1):
            block = collapsed[-block_len:]
            if collapsed.endswith(block * 2):
                collapsed = collapsed[:-block_len]
                changed = True
                break
        if not changed:
            break
    return collapsed


def sanitize_generated_text(text: Any) -> str:
    return collapse_repeated_tail(normalize_output_text(text))


def strip_think_markup(text: str) -> str:
    without_blocks = re.sub(r"<think>.*?</think>", "", text, flags=re.IGNORECASE | re.DOTALL)
    without_tags = re.sub(r"</?think>", "", without_blocks, flags=re.IGNORECASE)
    return without_tags


def compute_incremental_text(previous: str, current: str) -> tuple[str, str]:
    if not current:
        return "", previous
    if current.startswith(previous):
        return current[len(previous) :], current
    if previous.startswith(current) or current in previous:
        return "", previous
    prefix_len = 0
    for left, right in zip(previous, current):
        if left != right:
            break
        prefix_len += 1
    if prefix_len:
        return current[prefix_len:], current
    return current, current


def debug_enabled() -> bool:
    value = os.environ.get(DEBUG_ENV_VAR, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def truncate_debug_text(value: Any, limit: int = 1200) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...<truncated {len(text) - limit} chars>"


def debug_event(event: str, **payload: Any) -> None:
    if not debug_enabled():
        return
    record = {
        "ts": int(time.time()),
        "event": event,
        **payload,
    }
    print(f"[novelai-shim-debug] {json.dumps(record, ensure_ascii=False)}", flush=True)


def normalize_tool_arguments(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def build_tool_argument_hints(tools: Any) -> dict[str, str]:
    if not isinstance(tools, list):
        return {}
    hints: dict[str, str] = {}
    for entry in tools:
        if not isinstance(entry, dict) or entry.get("type") != "function":
            continue
        function = entry.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        parameters = function.get("parameters")
        if not isinstance(name, str) or not TOOL_NAME_RE.fullmatch(name) or not isinstance(parameters, dict):
            continue
        properties = parameters.get("properties")
        required = parameters.get("required")
        if isinstance(properties, dict):
            for preferred_name in ("path", "url", "query", "command", "text"):
                if preferred_name in properties:
                    hints[name] = preferred_name
                    break
            if name in hints:
                continue
        if isinstance(required, list) and len(required) == 1 and isinstance(required[0], str):
            hints[name] = required[0]
            continue
        if isinstance(properties, dict) and len(properties) == 1:
            only_key = next(iter(properties.keys()))
            if isinstance(only_key, str):
                hints[name] = only_key
    return hints


def build_tool_name_set(tools: Any) -> set[str]:
    return {tool["name"] for tool in normalize_tool_specs(tools) if isinstance(tool.get("name"), str)}


def parse_tool_argument_value(value: str) -> Any:
    text = value.strip()
    if not text:
        return ""
    if text[0] in "[{\"" or text in {"true", "false", "null"} or re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        try:
            return json.loads(text)
        except Exception:
            return text
    return text


def parse_inline_named_arguments(value: str) -> dict[str, Any] | None:
    text = value.strip()
    if not text:
        return {}
    if text.startswith('"') and '":' in text:
        try:
            parsed_fragment = json.loads("{" + text + "}")
        except Exception:
            parsed_fragment = None
        if isinstance(parsed_fragment, dict):
            return parsed_fragment
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    match = TOOL_ARG_RE.match(text)
    if not match:
        return None
    return {match.group(1): parse_tool_argument_value(match.group(2))}


def strip_tool_line_prefix(line: str) -> str:
    return re.sub(r"^(?:assistant|asistant)\s*:\s*", "", line.strip(), flags=re.IGNORECASE)


def strip_tool_noise(text: str) -> str:
    lines: list[str] = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            lines.append(raw_line)
            continue
        lowered = stripped.lower()
        if lowered.startswith("tool["):
            break
        if lowered.startswith("assistant:"):
            break
        if lowered.startswith("</think"):
            break
        lines.append(raw_line)
    return "\n".join(lines).strip()


def normalize_tool_args_for_name(name: str, args: dict[str, Any], tool_argument_hints: dict[str, str] | None) -> dict[str, Any]:
    normalized = dict(args)
    primary_arg = (tool_argument_hints or {}).get(name)
    if primary_arg and primary_arg not in normalized:
        for alias in ("args", "input", "value"):
            if alias in normalized and normalized[alias] not in (None, ""):
                normalized[primary_arg] = normalized.pop(alias)
                break
    for key, value in list(normalized.items()):
        if isinstance(value, str):
            normalized[key] = strip_tool_noise(value)
    return {key: value for key, value in normalized.items() if value not in (None, "")}


def has_required_primary_arg(name: str, args: dict[str, Any], tool_argument_hints: dict[str, str] | None) -> bool:
    primary_arg = (tool_argument_hints or {}).get(name)
    if not primary_arg:
        return True
    value = args.get(primary_arg)
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return True


def first_path_like_text(text: str) -> str | None:
    match = FILE_PATH_RE.search(text)
    if not match:
        return None
    return str(match.group("path")).strip()


def synthesize_read_tool_call_from_text(
    text: str,
    fallback_id: str,
    tool_argument_hints: dict[str, str] | None,
    valid_tool_names: set[str] | None,
) -> dict[str, Any] | None:
    if valid_tool_names and "read" not in valid_tool_names:
        return None
    arg_name = (tool_argument_hints or {}).get("read", "path")
    if arg_name != "path":
        return None
    path = first_path_like_text(text)
    if not path:
        return None
    return {
        "id": fallback_id,
        "type": "function",
        "function": {
            "name": "read",
            "arguments": json.dumps({"path": path}, ensure_ascii=False),
        },
    }


def normalize_tool_call_entry(entry: dict[str, Any], fallback_id: str) -> dict[str, Any] | None:
    function = entry.get("function") if isinstance(entry.get("function"), dict) else {}
    name = function.get("name") or entry.get("name") or ""
    if not isinstance(name, str) or not TOOL_NAME_RE.fullmatch(name):
        return None
    arguments = function.get("arguments")
    if arguments is None:
        arguments = entry.get("arguments", {})
    return {
        "id": str(entry.get("id") or fallback_id),
        "type": "function",
        "function": {
            "name": name,
            "arguments": normalize_tool_arguments(arguments),
        },
    }


def parse_tool_block(
    block: str,
    fallback_id: str,
    tool_argument_hints: dict[str, str] | None = None,
    valid_tool_names: set[str] | None = None,
) -> dict[str, Any] | None:
    lines = [line.rstrip() for line in block.strip().splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return None

    first = lines[0].strip()
    if first.lower().startswith("name:"):
        name = first.split(":", 1)[1].strip()
        body_lines = lines[1:]
    else:
        name = first
        body_lines = lines[1:]
    if not TOOL_NAME_RE.fullmatch(name):
        return None
    if valid_tool_names and name not in valid_tool_names:
        return None
    if not body_lines:
        return None

    args: dict[str, Any] = {}
    current_key: str | None = None
    current_lines: list[str] = []
    trailing_lines: list[str] = []

    def flush_current() -> None:
        nonlocal current_key, current_lines
        if current_key is None:
            return
        args[current_key] = parse_tool_argument_value("\n".join(current_lines))
        current_key = None
        current_lines = []

    for line in body_lines:
        match = TOOL_ARG_RE.match(line)
        if match:
            flush_current()
            current_key = match.group(1)
            current_lines = [match.group(2)]
            continue
        if current_key is None:
            if line.strip():
                trailing_lines.append(line)
            continue
        current_lines.append(line)
    flush_current()

    if trailing_lines and not args:
        maybe_json = "\n".join(trailing_lines).strip()
        try:
            parsed = json.loads(maybe_json)
        except Exception:
            return None
        if not isinstance(parsed, dict):
            return None
        args = parsed
    args = normalize_tool_args_for_name(name, args, tool_argument_hints)
    if not args and (tool_argument_hints or {}).get(name):
        return None
    if not has_required_primary_arg(name, args, tool_argument_hints):
        return None

    return {
        "id": fallback_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(args, ensure_ascii=False),
        },
    }


def parse_parenthesized_tool_call(
    line: str,
    fallback_id: str,
    tool_argument_hints: dict[str, str] | None,
    valid_tool_names: set[str] | None = None,
) -> dict[str, Any] | None:
    normalized_line = strip_tool_line_prefix(line)
    if not (normalized_line.startswith("(") and normalized_line.endswith(")")):
        return None
    body = normalized_line[1:-1].strip()
    if not body:
        return None
    name = ""
    raw_args = ""
    colon_match = re.match(r"^(?P<name>[A-Za-z_][\w.-]*)\s*:\s*(?P<args>.+)$", body)
    if colon_match:
        name = str(colon_match.group("name"))
        raw_args = str(colon_match.group("args")).strip()
    else:
        space_match = re.match(r"^(?P<name>[A-Za-z_][\w.-]*)\s+(?P<args>.+)$", body)
        if not space_match:
            return None
        name = str(space_match.group("name"))
        raw_args = str(space_match.group("args")).strip()
    if not TOOL_NAME_RE.fullmatch(name):
        return None
    if valid_tool_names and name not in valid_tool_names:
        return None
    if not raw_args:
        return None
    parsed_args = parse_inline_named_arguments(raw_args)
    if isinstance(parsed_args, dict):
        arguments = normalize_tool_args_for_name(name, parsed_args, tool_argument_hints)
    else:
        parsed_value = parse_tool_argument_value(raw_args)
        arg_name = (tool_argument_hints or {}).get(name)
        if arg_name:
            arguments = {arg_name: parsed_value}
        else:
            arguments = {"input": parsed_value}
        arguments = normalize_tool_args_for_name(name, arguments, tool_argument_hints)
    if not has_required_primary_arg(name, arguments, tool_argument_hints):
        return None
    return {
        "id": fallback_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments, ensure_ascii=False),
        },
    }


def parse_function_style_tool_call(
    line: str,
    fallback_id: str,
    tool_argument_hints: dict[str, str] | None,
    valid_tool_names: set[str] | None = None,
) -> dict[str, Any] | None:
    normalized_line = strip_tool_line_prefix(line)
    match = FUNC_TOOL_CALL_RE.match(normalized_line)
    if not match:
        return None
    name = match.group("name")
    if not TOOL_NAME_RE.fullmatch(name):
        return None
    if valid_tool_names and name not in valid_tool_names:
        return None
    raw_args = match.group("args").strip()
    if not raw_args:
        return None
    parsed_args = parse_inline_named_arguments(raw_args)
    if isinstance(parsed_args, dict):
        arguments = normalize_tool_args_for_name(name, parsed_args, tool_argument_hints)
    else:
        parsed_value = parse_tool_argument_value(raw_args)
        arg_name = (tool_argument_hints or {}).get(name)
        if arg_name:
            arguments = {arg_name: parsed_value}
        else:
            arguments = {"input": parsed_value}
        arguments = normalize_tool_args_for_name(name, arguments, tool_argument_hints)
    if not has_required_primary_arg(name, arguments, tool_argument_hints):
        return None
    return {
        "id": fallback_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments, ensure_ascii=False),
        },
    }


def extract_parenthesized_tool_calls_from_text(
    text: str,
    base_id: str,
    choice_index: int,
    tool_argument_hints: dict[str, str] | None,
    valid_tool_names: set[str] | None = None,
) -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    item_index = 0
    for raw_line in text.splitlines():
        normalized_line = strip_tool_line_prefix(raw_line)
        for match in WRAPPED_FUNC_TOOL_CALL_RE.finditer(normalized_line):
            item_index += 1
            tool_call = parse_function_style_tool_call(
                match.group("inner"),
                f"call_{base_id}_{choice_index}_{item_index}",
                tool_argument_hints,
                valid_tool_names,
            )
            if tool_call is not None:
                parsed.append(tool_call)
        for match in PAREN_TOOL_CALL_RE.finditer(normalized_line):
            item_index += 1
            chunk = match.group(0)
            tool_call = parse_parenthesized_tool_call(
                chunk,
                f"call_{base_id}_{choice_index}_{item_index}",
                tool_argument_hints,
                valid_tool_names,
            )
            if tool_call is not None:
                parsed.append(tool_call)
        if "(" not in normalized_line:
            item_index += 1
            tool_call = parse_function_style_tool_call(
                normalized_line,
                f"call_{base_id}_{choice_index}_{item_index}",
                tool_argument_hints,
                valid_tool_names,
            )
            if tool_call is not None:
                parsed.append(tool_call)
    return parsed


def extract_tool_calls_from_text(
    text: Any,
    base_id: str,
    choice_index: int,
    tool_argument_hints: dict[str, str] | None = None,
    valid_tool_names: set[str] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    source = strip_think_markup(sanitize_generated_text(content_to_text(text)))
    if TOOL_CALL_MARKER not in source:
        parenthesized = extract_parenthesized_tool_calls_from_text(
            source,
            base_id,
            choice_index,
            tool_argument_hints,
            valid_tool_names,
        )
        if parenthesized:
            # Force OpenClaw back into its normal multi-turn tool loop.
            return "", parenthesized[:1]
        synthesized_read = synthesize_read_tool_call_from_text(
            source,
            f"call_{base_id}_{choice_index}_read_fallback",
            tool_argument_hints,
            valid_tool_names,
        )
        if synthesized_read is not None:
            return "", [synthesized_read]
        return source, []

    parts = source.split(TOOL_CALL_MARKER)
    visible_parts = [parts[0]]
    tool_calls: list[dict[str, Any]] = []

    for block_index, block in enumerate(parts[1:], start=1):
        tool_call = parse_tool_block(
            block,
            f"call_{base_id}_{choice_index}_{block_index}",
            tool_argument_hints,
            valid_tool_names,
        )
        if tool_call is None:
            visible_parts.append(f"{TOOL_CALL_MARKER}{block}")
            continue
        tool_calls.append(tool_call)

    visible_text = "".join(visible_parts)
    normalized_visible = sanitize_generated_text(visible_text)
    if tool_calls:
        return "", tool_calls[:1]
    parenthesized = extract_parenthesized_tool_calls_from_text(
        normalized_visible,
        base_id,
        choice_index,
        tool_argument_hints,
        valid_tool_names,
    )
    if parenthesized:
        return "", parenthesized[:1]
    synthesized_read = synthesize_read_tool_call_from_text(
        normalized_visible,
        f"call_{base_id}_{choice_index}_read_fallback",
        tool_argument_hints,
        valid_tool_names,
    )
    if synthesized_read is not None:
        return "", [synthesized_read]
    return normalized_visible, []


def choice_tool_calls(
    choice: dict[str, Any],
    base_id: str,
    choice_index: int,
    tool_argument_hints: dict[str, str] | None = None,
    valid_tool_names: set[str] | None = None,
) -> list[dict[str, Any]]:
    message = choice.get("message")
    if isinstance(message, dict) and isinstance(message.get("tool_calls"), list):
        out = []
        for tool_index, entry in enumerate(message.get("tool_calls") or [], start=1):
            if not isinstance(entry, dict):
                continue
            normalized = normalize_tool_call_entry(entry, f"call_{base_id}_{choice_index}_{tool_index}")
            if normalized is not None:
                out.append(normalized)
        if out:
            return out
    if isinstance(choice.get("tool_calls"), list):
        out = []
        for tool_index, entry in enumerate(choice.get("tool_calls") or [], start=1):
            if not isinstance(entry, dict):
                continue
            normalized = normalize_tool_call_entry(entry, f"call_{base_id}_{choice_index}_{tool_index}")
            if normalized is not None:
                out.append(normalized)
        if out:
            return out
    _, parsed = extract_tool_calls_from_text(
        extract_choice_text(choice),
        base_id,
        choice_index,
        tool_argument_hints,
        valid_tool_names,
    )
    return parsed


def choice_visible_text(
    choice: dict[str, Any],
    base_id: str,
    choice_index: int,
    tool_argument_hints: dict[str, str] | None = None,
    valid_tool_names: set[str] | None = None,
) -> str:
    message = choice.get("message")
    if isinstance(message, dict) and isinstance(message.get("tool_calls"), list):
        return sanitize_generated_text(content_to_text(message.get("content", "")))
    text, _ = extract_tool_calls_from_text(
        extract_choice_text(choice),
        base_id,
        choice_index,
        tool_argument_hints,
        valid_tool_names,
    )
    return text


def normalize_delta_tool_calls(delta_tool_calls: Any, base_id: str, choice_index: int) -> list[dict[str, Any]]:
    if not isinstance(delta_tool_calls, list):
        return []
    out = []
    for tool_index, entry in enumerate(delta_tool_calls, start=1):
        if not isinstance(entry, dict):
            continue
        normalized = normalize_tool_call_entry(entry, f"call_{base_id}_{choice_index}_{tool_index}")
        if normalized is not None:
            out.append(normalized)
    return out


def chunk_text(text: str) -> list[str]:
    normalized = normalize_output_text(text)
    if not normalized:
        return [""]
    chunk_size = 1024
    return [normalized[i : i + chunk_size] for i in range(0, len(normalized), chunk_size)]


def chat_to_completions(chat_resp: dict[str, Any], model: str) -> dict[str, Any]:
    choices = []
    for choice in chat_resp.get("choices", []):
        choices.append(
            {
                "text": sanitize_generated_text(extract_choice_text(choice)),
                "index": choice.get("index", 0),
                "logprobs": None,
                "finish_reason": choice.get("finish_reason", "stop"),
            }
        )
    usage = chat_resp.get("usage", {}) or {}
    return {
        "id": chat_resp.get("id", "cmpl-novelai-shim"),
        "object": "text_completion",
        "created": chat_resp.get("created", 0),
        "model": model,
        "choices": choices,
        "usage": {
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
    }


def upstream_to_openai_chat(
    chat_resp: dict[str, Any],
    model: str,
    tool_argument_hints: dict[str, str] | None = None,
    valid_tool_names: set[str] | None = None,
) -> dict[str, Any]:
    choices = []
    base_id = chat_resp.get("id", "chatcmpl-novelai-shim")
    for choice in chat_resp.get("choices", []):
        index = choice.get("index", 0)
        text = choice_visible_text(choice, base_id, index, tool_argument_hints, valid_tool_names)
        tool_calls = choice_tool_calls(choice, base_id, index, tool_argument_hints, valid_tool_names)
        finish_reason = choice.get("finish_reason")
        if tool_calls and finish_reason in (None, "stop"):
            finish_reason = "tool_calls"
        elif finish_reason is None:
            finish_reason = "stop"
        message: dict[str, Any] = {"role": "assistant", "content": text}
        if tool_calls:
            message["tool_calls"] = tool_calls
        choices.append({"index": index, "message": message, "finish_reason": finish_reason})
    usage = chat_resp.get("usage", {}) or {}
    return {
        "id": base_id,
        "object": "chat.completion",
        "created": chat_resp.get("created", 0),
        "model": model,
        "choices": choices,
        "usage": {
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
    }


def write_sse_event(handler: BaseHTTPRequestHandler, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False)
    handler.wfile.write(f"data: {data}\n\n".encode("utf-8"))
    handler.wfile.flush()


def finish_sse(handler: BaseHTTPRequestHandler) -> None:
    handler.wfile.write(b"data: [DONE]\n\n")
    handler.wfile.flush()
    handler.close_connection = True


def build_upstream_request(payload: dict[str, Any], upstream: str, api_key: str, *, accept_sse: bool = False) -> request.Request:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        # NovelAI's edge may reject the default Python urllib signature with Cloudflare 1010.
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        "Origin": "https://novelai.net",
        "Referer": "https://novelai.net/",
    }
    if accept_sse:
        headers["Accept"] = "text/event-stream, application/json, */*"
    else:
        headers["Accept"] = "application/json, text/plain, */*"
    return request.Request(
        upstream,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )


def open_upstream_stream(payload: dict[str, Any], upstream: str, api_key: str) -> Any:
    req = build_upstream_request(payload, upstream, api_key, accept_sse=True)
    return request.urlopen(req, timeout=120)


def iter_sse_payloads(response: Any) -> Any:
    data_lines: list[str] = []
    while True:
        raw_line = response.readline()
        if not raw_line:
            break
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            if data_lines:
                yield "\n".join(data_lines)
                data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        yield "\n".join(data_lines)


def extract_stream_choice_text(choice: dict[str, Any]) -> tuple[str, bool]:
    if choice.get("text") is not None:
        return str(choice.get("text", "")).replace("\r\n", "\n"), False
    delta = choice.get("delta")
    if isinstance(delta, dict) and delta.get("content") is not None:
        return content_to_text(delta.get("content", "")).replace("\r\n", "\n"), True
    if choice.get("message") is not None:
        return content_to_text((choice.get("message") or {}).get("content", "")).replace("\r\n", "\n"), False
    return "", True


def coerce_stream_text_delta(raw_text: str, state: dict[str, Any], *, is_delta: bool) -> str:
    if not raw_text:
        return ""
    assembled = str(state.get("assembled_text", ""))
    if is_delta:
        state["assembled_text"] = assembled + raw_text
        return raw_text
    if raw_text.startswith(assembled):
        new_text = raw_text[len(assembled) :]
        state["assembled_text"] = raw_text
        return new_text
    if assembled.endswith(raw_text):
        return ""
    state["assembled_text"] = assembled + raw_text
    return raw_text


def normalize_chat_messages(body: dict[str, Any]) -> list[dict[str, Any]]:
    messages = body.get("messages")
    if isinstance(messages, list) and messages:
        return messages
    if body.get("prompt") is not None:
        return prompt_to_messages(body.get("prompt"))
    return [{"role": "user", "content": ""}]


def render_tool_call_block(tool_call: dict[str, Any]) -> str:
    function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
    name = str(function.get("name", "")).strip()
    arguments = normalize_tool_arguments(function.get("arguments"))
    return f"{TOOL_CALL_MARKER}\nname: {name}\n{arguments}"


def normalize_tool_specs(tools: Any) -> list[dict[str, Any]]:
    if not isinstance(tools, list):
        return []
    normalized: list[dict[str, Any]] = []
    for entry in tools:
        if not isinstance(entry, dict) or entry.get("type") != "function":
            continue
        function = entry.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not isinstance(name, str) or not TOOL_NAME_RE.fullmatch(name):
            continue
        normalized.append(
            {
                "name": name,
                "description": str(function.get("description", "") or "").strip(),
                "parameters": function.get("parameters", {}),
            }
        )
    return normalized


def compact_tool_schema(tool: dict[str, Any]) -> str:
    name = str(tool.get("name", "")).strip()
    description = str(tool.get("description", "") or "").strip()
    parameters = tool.get("parameters")
    if not isinstance(parameters, dict):
        return f"- {name}: {description or '(no description)'}"
    required = parameters.get("required")
    properties = parameters.get("properties")
    arg_text = ""
    if isinstance(required, list) and required:
        arg_text = ", ".join(str(item) for item in required[:3] if isinstance(item, str))
    elif isinstance(properties, dict) and properties:
        arg_text = ", ".join(str(item) for item in list(properties.keys())[:3] if isinstance(item, str))
    if arg_text:
        return f"- {name}({arg_text}): {description or '(no description)'}"
    return f"- {name}: {description or '(no description)'}"


def messages_to_prompt(messages: list[dict[str, Any]]) -> str:
    lines = []
    for msg in messages:
        role = msg.get("role", "user")
        content = content_to_text(msg.get("content", ""))
        if role == "assistant":
            if content:
                lines.append(f"assistant: {content}")
            tool_calls = msg.get("tool_calls")
            if isinstance(tool_calls, list):
                if not content:
                    lines.append("assistant:")
                for entry in tool_calls:
                    if not isinstance(entry, dict):
                        continue
                    normalized = normalize_tool_call_entry(entry, str(entry.get("id") or "call_history"))
                    if normalized is not None:
                        lines.append(render_tool_call_block(normalized))
            elif not content:
                lines.append("assistant:")
            continue
        if role == "tool":
            tool_name = str(msg.get("name") or msg.get("tool_call_id") or "tool")
            lines.append(f"tool[{tool_name}]: {content}")
            continue
        lines.append(f"{role}: {content}")
    lines.append("assistant:")
    return "\n".join(lines)


def last_user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content", "")
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") in ("text", "input_text"):
                    parts.append(part.get("text", ""))
                else:
                    parts.append(str(part))
            content = "".join(parts)
        return str(content)
    return ""


def build_single_step_action_prompt(messages: list[dict[str, Any]], tools: Any, tool_choice: Any) -> str:
    normalized_tools = normalize_tool_specs(tools)
    lines = [
        "system: You are a single-step action router for an OpenClaw session.",
        "system: Decide exactly one next step.",
        "system: If a tool is needed, output exactly one tool call and nothing else.",
        'system: Use this exact tool format on a single line: (tool_name: {"arg":"value"})',
        "system: If a tool has a single obvious argument, plain text is also allowed: (tool_name: value)",
        "system: Never narrate plans, startup steps, reasoning, or intentions.",
        "system: Never output more than one tool call in a single turn.",
        "system: If the instructions require reading files or checking state first, do that tool call now instead of greeting.",
        "system: If no tool is needed, reply with the final assistant message only.",
    ]
    if tool_choice not in (None, "auto"):
        lines.append(f"system: tool_choice={json.dumps(tool_choice, ensure_ascii=False)}")
    if normalized_tools:
        lines.append("system: Available tools:")
        for tool in normalized_tools:
            lines.append(f"system: {compact_tool_schema(tool)}")
    lines.append("system: Conversation transcript follows.")
    lines.append(messages_to_prompt(messages))
    return "\n".join(lines)


def prompt_from_body(body: dict[str, Any], *, action_mode: str = ACTION_MODE_NATIVE) -> str:
    messages = normalize_chat_messages(body)
    if action_mode == ACTION_MODE_SINGLE_STEP and normalize_tool_specs(body.get("tools")):
        return build_single_step_action_prompt(messages, body.get("tools"), body.get("tool_choice"))
    return messages_to_prompt(messages)


def chat_request_from_body(body: dict[str, Any], *, action_mode: str = ACTION_MODE_NATIVE) -> dict[str, Any]:
    messages = normalize_chat_messages(body)
    out = {
        "model": body.get("model", "glm-4-6"),
        "prompt": prompt_from_body(body, action_mode=action_mode),
        "temperature": body.get("temperature", 1),
        "top_p": body.get("top_p", 1),
        "max_tokens": body.get("max_tokens", body.get("max_completion_tokens", 1024)),
    }
    if body.get("stop") is not None:
        out["stop"] = body["stop"]
    else:
        out["stop"] = ["\nuser:", "\nsystem:"]
    for key in ["presence_penalty", "frequency_penalty", "top_k", "n"]:
        if body.get(key) is not None:
            out[key] = body[key]
    if body.get("stream") is not None:
        out["stream"] = body["stream"]
    debug_event(
        "chat_request_from_body",
        model=out["model"],
        message_count=len(messages),
        tool_count=len(body.get("tools") or []),
        tool_choice=body.get("tool_choice"),
        action_mode=action_mode,
        stream=body.get("stream"),
        prompt_preview=truncate_debug_text(out["prompt"]),
    )
    return out


def fallback_prompt_from_body(body: dict[str, Any], *, action_mode: str = ACTION_MODE_NATIVE) -> str:
    if action_mode == ACTION_MODE_SINGLE_STEP and normalize_tool_specs(body.get("tools")):
        prompt = prompt_from_body(body, action_mode=action_mode)
        debug_event(
            "fallback_prompt_single_step",
            message_count=len(normalize_chat_messages(body)),
            tool_count=len(body.get("tools") or []),
            prompt_preview=truncate_debug_text(prompt),
        )
        return prompt
    messages = normalize_chat_messages(body)
    last_user = last_user_text(messages)
    if not last_user:
        prompt = messages_to_prompt(messages)
        debug_event(
            "fallback_prompt_full_history",
            message_count=len(messages),
            tool_count=len(body.get("tools") or []),
            prompt_preview=truncate_debug_text(prompt),
        )
        return prompt
    lines = []
    lines.append(f"user: {last_user}")
    lines.append("assistant:")
    prompt = "\n".join(lines)
    debug_event(
        "fallback_prompt_last_user",
        message_count=len(messages),
        tool_count=len(body.get("tools") or []),
        prompt_preview=truncate_debug_text(prompt),
    )
    return prompt


def should_buffer_tool_stream(body: dict[str, Any], settings: dict[str, Any], *, is_chat: bool, wants_stream: bool) -> bool:
    if not is_chat or not wants_stream:
        return False
    if settings.get("action_mode") != ACTION_MODE_SINGLE_STEP:
        return False
    return bool(normalize_tool_specs(body.get("tools")))


def call_upstream(payload: dict[str, Any], upstream: str, api_key: str) -> tuple[int, str]:
    req = build_upstream_request(payload, upstream, api_key)
    try:
        with request.urlopen(req, timeout=120) as response:
            return response.status, response.read().decode("utf-8")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return exc.code, detail


def try_parse_json(detail: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(detail)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


class ShimHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], handler_class: type[BaseHTTPRequestHandler], settings: dict[str, Any]):
        super().__init__(server_address, handler_class)
        self.settings = settings


class Handler(BaseHTTPRequestHandler):
    server: ShimHTTPServer

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Connection", "close")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        self.wfile.flush()
        self.close_connection = True

    def _start_sse(self, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()

    def _send_chat_sse(self, chat_resp: dict[str, Any]) -> None:
        self._start_sse(200)
        base_id = chat_resp.get("id", "chatcmpl-novelai-shim")
        created = chat_resp.get("created") or int(time.time())
        model = chat_resp.get("model", "glm-4-6")
        choices = chat_resp.get("choices") or []
        if not choices:
            write_sse_event(
                self,
                {
                    "id": base_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                },
            )
            finish_sse(self)
            return
        for choice in choices:
            index = choice.get("index", 0)
            message = choice.get("message") or {}
            text = message.get("content", "")
            tool_calls = normalize_delta_tool_calls(message.get("tool_calls"), base_id, index)
            first = True
            if tool_calls:
                delta: dict[str, Any] = {"tool_calls": tool_calls}
                if text:
                    delta["content"] = text
                if first:
                    delta["role"] = message.get("role", "assistant")
                    first = False
                write_sse_event(
                    self,
                    {
                        "id": base_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [{"index": index, "delta": delta, "finish_reason": None}],
                    },
                )
            for part in chunk_text(text) if not tool_calls else []:
                delta = {"content": part}
                if first:
                    delta["role"] = message.get("role", "assistant")
                    first = False
                write_sse_event(
                    self,
                    {
                        "id": base_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [{"index": index, "delta": delta, "finish_reason": None}],
                    },
                )
            write_sse_event(
                self,
                {
                    "id": base_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{"index": index, "delta": {}, "finish_reason": choice.get("finish_reason", "stop")}],
                },
            )
        finish_sse(self)

    def _send_completions_sse(self, completion_resp: dict[str, Any]) -> None:
        self._start_sse(200)
        base_id = completion_resp.get("id", "cmpl-novelai-shim")
        created = completion_resp.get("created") or int(time.time())
        model = completion_resp.get("model", "glm-4-6")
        choices = completion_resp.get("choices") or []
        if not choices:
            write_sse_event(
                self,
                {
                    "id": base_id,
                    "object": "text_completion",
                    "created": created,
                    "model": model,
                    "choices": [{"index": 0, "text": "", "logprobs": None, "finish_reason": "stop"}],
                },
            )
            finish_sse(self)
            return
        for choice in choices:
            index = choice.get("index", 0)
            text = choice.get("text", "")
            for part in chunk_text(text):
                write_sse_event(
                    self,
                    {
                        "id": base_id,
                        "object": "text_completion",
                        "created": created,
                        "model": model,
                        "choices": [{"index": index, "text": part, "logprobs": None, "finish_reason": None}],
                    },
                )
            write_sse_event(
                self,
                {
                    "id": base_id,
                    "object": "text_completion",
                    "created": created,
                    "model": model,
                    "choices": [{"index": index, "text": "", "logprobs": None, "finish_reason": choice.get("finish_reason", "stop")}],
                },
            )
        finish_sse(self)

    def _normalize_upstream_chat_chunk(
        self,
        upstream_chunk: dict[str, Any],
        model: str,
        role_sent: set[int],
        stream_states: dict[int, dict[str, Any]],
        tool_argument_hints: dict[str, str] | None = None,
        valid_tool_names: set[str] | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        base_id = upstream_chunk.get("id", "chatcmpl-novelai-shim")
        created = upstream_chunk.get("created") or int(time.time())
        out_events = []
        saw_non_whitespace = False
        for choice in upstream_chunk.get("choices") or []:
            index = choice.get("index", 0)
            state = stream_states.setdefault(
                index,
                {
                    "buffer": "",
                    "assembled_text": "",
                    "emitted_text": "",
                    "tool_sent": False,
                    "suppress_after_tool": False,
                },
            )
            delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
            delta_tool_calls = normalize_delta_tool_calls(delta.get("tool_calls") or choice.get("tool_calls"), base_id, index)
            raw_text, is_delta_text = extract_stream_choice_text(choice)
            text = coerce_stream_text_delta(raw_text, state, is_delta=is_delta_text)
            finish_reason = choice.get("finish_reason")

            if delta_tool_calls:
                payload_delta: dict[str, Any] = {"tool_calls": delta_tool_calls}
                if index not in role_sent:
                    payload_delta["role"] = delta.get("role", "assistant")
                    role_sent.add(index)
                state["tool_sent"] = True
                state["suppress_after_tool"] = True
                out_events.append(
                    {
                        "id": base_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [{"index": index, "delta": payload_delta, "finish_reason": finish_reason}],
                    }
                )
                continue

            if text and not state.get("suppress_after_tool"):
                state["buffer"] += text

            visible_text, parsed_tool_calls = extract_tool_calls_from_text(
                state["buffer"],
                base_id,
                index,
                tool_argument_hints,
                valid_tool_names,
            )
            new_text, emitted_snapshot = compute_incremental_text(str(state.get("emitted_text", "")), visible_text)
            state["emitted_text"] = emitted_snapshot

            if new_text and not state.get("suppress_after_tool"):
                payload_delta = {"content": new_text}
                if index not in role_sent:
                    payload_delta["role"] = delta.get("role", "assistant")
                    role_sent.add(index)
                if new_text.strip():
                    saw_non_whitespace = True
                out_events.append(
                    {
                        "id": base_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [{"index": index, "delta": payload_delta, "finish_reason": None}],
                    }
                )

            if parsed_tool_calls and not state.get("tool_sent"):
                payload_delta = {"tool_calls": parsed_tool_calls}
                if index not in role_sent:
                    payload_delta["role"] = delta.get("role", "assistant")
                    role_sent.add(index)
                out_events.append(
                    {
                        "id": base_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [{"index": index, "delta": payload_delta, "finish_reason": None}],
                    }
                )
                state["tool_sent"] = True
                state["suppress_after_tool"] = True
                state["buffer"] = ""

            if finish_reason is not None:
                out_events.append(
                    {
                        "id": base_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [
                            {
                                "index": index,
                                "delta": {},
                                "finish_reason": "tool_calls" if state.get("tool_sent") else finish_reason,
                            }
                        ],
                    }
                )
        return out_events, saw_non_whitespace

    def _normalize_upstream_completion_chunk(
        self,
        upstream_chunk: dict[str, Any],
        model: str,
        stream_states: dict[int, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        base_id = upstream_chunk.get("id", "cmpl-novelai-shim")
        created = upstream_chunk.get("created") or int(time.time())
        out_events = []
        for choice in upstream_chunk.get("choices") or []:
            index = choice.get("index", 0)
            state = stream_states.setdefault(index, {"assembled_text": ""})
            raw_text, is_delta_text = extract_stream_choice_text(choice)
            text = coerce_stream_text_delta(raw_text, state, is_delta=is_delta_text)
            finish_reason = choice.get("finish_reason")
            if text or finish_reason is not None:
                out_events.append(
                    {
                        "id": base_id,
                        "object": "text_completion",
                        "created": created,
                        "model": model,
                        "choices": [{"index": index, "text": text, "logprobs": None, "finish_reason": finish_reason}],
                    }
                )
        return out_events

    def _stream_plain_chat_response(
        self,
        response: Any,
        model: str,
        tool_argument_hints: dict[str, str] | None = None,
        valid_tool_names: set[str] | None = None,
    ) -> None:
        detail = response.read().decode("utf-8")
        chat_resp = upstream_to_openai_chat(json.loads(detail), model, tool_argument_hints, valid_tool_names)
        self._send_chat_sse(chat_resp)

    def _stream_plain_completion_response(self, response: Any, model: str) -> None:
        detail = response.read().decode("utf-8")
        completion_resp = chat_to_completions(json.loads(detail), model)
        self._send_completions_sse(completion_resp)

    def _proxy_chat_stream(
        self,
        payload: dict[str, Any],
        settings: dict[str, Any],
        model: str,
        tool_argument_hints: dict[str, str] | None = None,
        valid_tool_names: set[str] | None = None,
    ) -> bool:
        role_sent: set[int] = set()
        stream_states: dict[int, dict[str, Any]] = {}
        pending_events: list[dict[str, Any]] = []
        saw_non_whitespace = False
        started = False
        with open_upstream_stream(payload, settings["upstream"], settings["api_key"]) as response:
            content_type = (response.headers.get("Content-Type") or "").lower()
            if "text/event-stream" not in content_type:
                self._stream_plain_chat_response(response, model, tool_argument_hints, valid_tool_names)
                return True
            for raw_payload in iter_sse_payloads(response):
                if raw_payload == "[DONE]":
                    break
                upstream_chunk = json.loads(raw_payload)
                events, chunk_has_text = self._normalize_upstream_chat_chunk(
                    upstream_chunk,
                    model,
                    role_sent,
                    stream_states,
                    tool_argument_hints,
                    valid_tool_names,
                )
                saw_non_whitespace = saw_non_whitespace or chunk_has_text
                if not events:
                    continue
                if not started:
                    pending_events.extend(events)
                if (saw_non_whitespace or pending_events) and not started:
                    self._start_sse(200)
                    started = True
                    for event_payload in pending_events:
                        write_sse_event(self, event_payload)
                    pending_events = []
                    continue
                if started:
                    for event_payload in events:
                        write_sse_event(self, event_payload)
        if not saw_non_whitespace and not pending_events:
            return False
        if not started:
            self._start_sse(200)
            for event_payload in pending_events:
                write_sse_event(self, event_payload)
        finish_sse(self)
        return True

    def _proxy_completion_stream(self, payload: dict[str, Any], settings: dict[str, Any], model: str) -> None:
        stream_states: dict[int, dict[str, Any]] = {}
        with open_upstream_stream(payload, settings["upstream"], settings["api_key"]) as response:
            content_type = (response.headers.get("Content-Type") or "").lower()
            if "text/event-stream" not in content_type:
                self._stream_plain_completion_response(response, model)
                return
            self._start_sse(200)
            for raw_payload in iter_sse_payloads(response):
                if raw_payload == "[DONE]":
                    break
                upstream_chunk = json.loads(raw_payload)
                for event_payload in self._normalize_upstream_completion_chunk(upstream_chunk, model, stream_states):
                    write_sse_event(self, event_payload)
            finish_sse(self)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/v1/models":
            model = self.server.settings["model"]
            model_ids = [model, *[item for item in SUPPORTED_TEXT_MODELS if item != model]]
            self._send_json(
                200,
                {
                    "object": "list",
                    "data": [{"id": item, "object": "model", "owned_by": "novelai-shim"} for item in model_ids],
                },
            )
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path not in ["/v1/completions", "/completions", "/v1/chat/completions", "/chat/completions"]:
            self._send_json(404, {"error": "not found", "path": self.path})
            return

        settings = self.server.settings
        if not settings["api_key"]:
            self._send_json(500, {"error": "NovelAI api_key missing. Run `novelai-config set --api-key ...` first."})
            return

        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception as exc:  # pragma: no cover
            self._send_json(400, {"error": f"invalid json: {exc}"})
            return

        is_chat = self.path in ["/v1/chat/completions", "/chat/completions"]
        wants_stream = bool(body.get("stream"))
        upstream_body = chat_request_from_body(body, action_mode=settings.get("action_mode", ACTION_MODE_NATIVE)) if is_chat else body
        debug_event(
            "incoming_request",
            path=self.path,
            is_chat=is_chat,
            wants_stream=wants_stream,
            body_preview=truncate_debug_text(json.dumps(body, ensure_ascii=False)),
        )

        try:
            model = body.get("model", settings["model"])
            tool_argument_hints = build_tool_argument_hints(body.get("tools"))
            valid_tool_names = build_tool_name_set(body.get("tools"))
            buffered_tool_stream = should_buffer_tool_stream(body, settings, is_chat=is_chat, wants_stream=wants_stream)
            if wants_stream:
                if is_chat:
                    if buffered_tool_stream:
                        debug_event(
                            "buffered_tool_stream_enabled",
                            model=model,
                            action_mode=settings.get("action_mode"),
                            tool_count=len(valid_tool_names),
                        )
                        buffered_upstream_body = dict(upstream_body)
                        buffered_upstream_body.pop("stream", None)
                        status, detail = call_upstream(buffered_upstream_body, settings["upstream"], settings["api_key"])
                        debug_event(
                            "buffered_tool_stream_response",
                            status=status,
                            detail_preview=truncate_debug_text(detail),
                        )
                        if not 200 <= status < 300:
                            self._send_json(
                                status,
                                {
                                    "error": "upstream http error",
                                    "status": status,
                                    "detail": detail,
                                    "upstream_request": buffered_upstream_body,
                                    "path": self.path,
                                },
                            )
                            return
                        upstream_json = try_parse_json(detail)
                        if upstream_json is None:
                            self._send_json(
                                502,
                                {
                                    "error": "upstream returned non-json success response",
                                    "status": status,
                                    "detail": detail,
                                    "upstream_request": buffered_upstream_body,
                                    "path": self.path,
                                },
                            )
                            return
                        chat_resp = upstream_to_openai_chat(upstream_json, model, tool_argument_hints, valid_tool_names)
                        first_message = (((chat_resp.get("choices") or [{}])[0].get("message") or {}))
                        content = first_message.get("content") or ""
                        tool_calls = first_message.get("tool_calls") or []
                        if not content.strip() and not tool_calls:
                            fallback_body = {
                                "model": model,
                                "prompt": fallback_prompt_from_body(body, action_mode=settings.get("action_mode", ACTION_MODE_NATIVE)),
                                "temperature": body.get("temperature", 1),
                                "top_p": body.get("top_p", 1),
                                "max_tokens": body.get("max_tokens", body.get("max_completion_tokens", 1024)),
                                "stop": body.get("stop") if body.get("stop") is not None else ["\nuser:", "\nsystem:"],
                            }
                            if body.get("presence_penalty") is not None:
                                fallback_body["presence_penalty"] = body["presence_penalty"]
                            if body.get("frequency_penalty") is not None:
                                fallback_body["frequency_penalty"] = body["frequency_penalty"]
                            if body.get("n") is not None:
                                fallback_body["n"] = body["n"]
                            status2, detail2 = call_upstream(fallback_body, settings["upstream"], settings["api_key"])
                            debug_event(
                                "buffered_tool_stream_fallback_response",
                                status=status2,
                                detail_preview=truncate_debug_text(detail2),
                            )
                            if not 200 <= status2 < 300:
                                self._send_json(
                                    status2,
                                    {
                                        "error": "upstream http error",
                                        "status": status2,
                                        "detail": detail2,
                                        "upstream_request": fallback_body,
                                        "path": self.path,
                                        "fallback": True,
                                    },
                                )
                                return
                            upstream_json2 = try_parse_json(detail2)
                            if upstream_json2 is None:
                                self._send_json(
                                    502,
                                    {
                                        "error": "upstream returned non-json success response",
                                        "status": status2,
                                        "detail": detail2,
                                        "upstream_request": fallback_body,
                                        "path": self.path,
                                        "fallback": True,
                                    },
                                )
                                return
                            chat_resp = upstream_to_openai_chat(upstream_json2, model, tool_argument_hints, valid_tool_names)
                        self._send_chat_sse(chat_resp)
                        return
                    if self._proxy_chat_stream(upstream_body, settings, model, tool_argument_hints, valid_tool_names):
                        return
                    fallback_body = {
                        "model": model,
                        "prompt": fallback_prompt_from_body(body, action_mode=settings.get("action_mode", ACTION_MODE_NATIVE)),
                        "temperature": body.get("temperature", 1),
                        "top_p": body.get("top_p", 1),
                        "max_tokens": body.get("max_tokens", body.get("max_completion_tokens", 1024)),
                        "stop": body.get("stop") if body.get("stop") is not None else ["\nuser:", "\nsystem:"],
                        "stream": True,
                    }
                    if body.get("presence_penalty") is not None:
                        fallback_body["presence_penalty"] = body["presence_penalty"]
                    if body.get("frequency_penalty") is not None:
                        fallback_body["frequency_penalty"] = body["frequency_penalty"]
                    if body.get("n") is not None:
                        fallback_body["n"] = body["n"]
                    if self._proxy_chat_stream(fallback_body, settings, model, tool_argument_hints, valid_tool_names):
                        return
                    self._start_sse(200)
                    write_sse_event(
                        self,
                        {
                            "id": "chatcmpl-novelai-shim",
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": model,
                            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": "stop"}],
                        },
                    )
                    finish_sse(self)
                    return
                self._proxy_completion_stream(upstream_body, settings, model)
                return

            status, detail = call_upstream(upstream_body, settings["upstream"], settings["api_key"])
            debug_event(
                "upstream_response",
                status=status,
                detail_preview=truncate_debug_text(detail),
            )
            if not 200 <= status < 300:
                self._send_json(
                    status,
                    {
                        "error": "upstream http error",
                        "status": status,
                        "detail": detail,
                        "upstream_request": upstream_body,
                        "path": self.path,
                    },
                )
                return
            upstream_json = try_parse_json(detail)
            if upstream_json is None:
                self._send_json(
                    502,
                    {
                        "error": "upstream returned non-json success response",
                        "status": status,
                        "detail": detail,
                        "upstream_request": upstream_body,
                        "path": self.path,
                    },
                )
                return

            if is_chat:
                chat_resp = upstream_to_openai_chat(upstream_json, model, tool_argument_hints, valid_tool_names)
                first_message = (((chat_resp.get("choices") or [{}])[0].get("message") or {}))
                content = first_message.get("content") or ""
                tool_calls = first_message.get("tool_calls") or []
                if not content.strip() and not tool_calls:
                    fallback_body = {
                        "model": model,
                        "prompt": fallback_prompt_from_body(body, action_mode=settings.get("action_mode", ACTION_MODE_NATIVE)),
                        "temperature": body.get("temperature", 1),
                        "top_p": body.get("top_p", 1),
                        "max_tokens": body.get("max_tokens", body.get("max_completion_tokens", 1024)),
                        "stop": body.get("stop") if body.get("stop") is not None else ["\nuser:", "\nsystem:"],
                    }
                    status2, detail2 = call_upstream(fallback_body, settings["upstream"], settings["api_key"])
                    if 200 <= status2 < 300:
                        upstream_json2 = try_parse_json(detail2)
                        if upstream_json2 is None:
                            self._send_json(
                                502,
                                {
                                    "error": "upstream returned non-json success response",
                                    "status": status2,
                                    "detail": detail2,
                                    "upstream_request": fallback_body,
                                    "path": self.path,
                                    "fallback": True,
                                },
                            )
                            return
                        chat_resp = upstream_to_openai_chat(upstream_json2, model, tool_argument_hints, valid_tool_names)
                    else:
                        self._send_json(
                            status2,
                            {
                                "error": "upstream http error",
                                "status": status2,
                                "detail": detail2,
                                "upstream_request": fallback_body,
                                "path": self.path,
                                "fallback": True,
                            },
                        )
                        return
                self._send_json(200, chat_resp)
                return

            completion_resp = chat_to_completions(upstream_json, model)
            self._send_json(200, completion_resp)
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            self._send_json(
                exc.code,
                {
                    "error": "upstream http error",
                    "status": exc.code,
                    "detail": detail,
                    "upstream_request": upstream_body,
                    "path": self.path,
                },
            )
        except Exception as exc:  # pragma: no cover
            self._send_json(
                500,
                {
                    "error": f"upstream failure: {exc}",
                    "upstream_request": upstream_body,
                    "path": self.path,
                },
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Start a local NovelAI shim with an OpenAI-compatible interface.")
    parser.add_argument("--config", default=None, help="Path to the TOML config file")
    parser.add_argument("--api-key", default=None, help="NovelAI API key; overrides the config file")
    parser.add_argument("--host", default=None, help="Listen host")
    parser.add_argument("--port", type=int, default=None, help="Listen port")
    parser.add_argument("--upstream", default=None, help="NovelAI upstream completions endpoint")
    parser.add_argument("--model", default=None, help="Model ID exposed to OpenClaw")
    parser.add_argument("--action-mode", choices=[ACTION_MODE_NATIVE, ACTION_MODE_SINGLE_STEP], default=None, help="Tool orchestration mode: native or single-step")
    return parser


def resolve_runtime_settings(args: argparse.Namespace) -> tuple[dict[str, Any], Any]:
    config, config_path, created = ensure_config_file(args.config)
    lang = get_language(config)
    if created:
        print(choose_text(lang, f"No config file found. Created default config: {config_path}", f"未发现配置文件，已自动创建默认配置: {config_path}"))
    config, api_key, _ = ensure_persisted_api_key(
        config,
        config_path,
        args.api_key,
        prompt_text=choose_text(lang, "NovelAI API key not found. Enter it to start the shim: ", "未检测到 NovelAI API Key，请输入后启动 shim: "),
        lang=lang,
    )
    action_mode = str(args.action_mode or config["shim"].get("action_mode", ACTION_MODE_SINGLE_STEP)).strip().lower()
    if action_mode not in {ACTION_MODE_NATIVE, ACTION_MODE_SINGLE_STEP}:
        action_mode = ACTION_MODE_NATIVE
    settings = {
        "api_key": api_key,
        "host": args.host or config["shim"]["host"],
        "port": args.port if args.port is not None else int(config["shim"]["port"]),
        "upstream": args.upstream or config["shim"]["upstream"],
        "model": args.model or config["shim"]["model"],
        "action_mode": action_mode,
        "language": lang,
    }
    return settings, config_path


def run_server(settings: dict[str, Any]) -> None:
    server = ShimHTTPServer((settings["host"], settings["port"]), Handler, settings)
    lang = settings["language"]
    print(choose_text(lang, f"Config: {settings['config_path']}", f"Config: {settings['config_path']}"))
    print(
        choose_text(
            lang,
            f"NovelAI shim listening on http://{settings['host']}:{settings['port']} (mode: {settings['action_mode']})",
            f"NovelAI shim 已启动: http://{settings['host']}:{settings['port']} (mode: {settings['action_mode']})",
        )
    )
    server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings, config_path = resolve_runtime_settings(args)
    settings["config_path"] = str(config_path)
    run_server(settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
