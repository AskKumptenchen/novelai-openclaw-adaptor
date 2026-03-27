import argparse
import json
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


def normalize_tool_arguments(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


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


def parse_tool_block(block: str, fallback_id: str) -> dict[str, Any] | None:
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

    return {
        "id": fallback_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(args, ensure_ascii=False),
        },
    }


def extract_tool_calls_from_text(text: Any, base_id: str, choice_index: int) -> tuple[str, list[dict[str, Any]]]:
    source = content_to_text(text).replace("\r\n", "\n")
    if TOOL_CALL_MARKER not in source:
        return normalize_output_text(source), []

    parts = source.split(TOOL_CALL_MARKER)
    visible_parts = [parts[0]]
    tool_calls: list[dict[str, Any]] = []

    for block_index, block in enumerate(parts[1:], start=1):
        tool_call = parse_tool_block(block, f"call_{base_id}_{choice_index}_{block_index}")
        if tool_call is None:
            visible_parts.append(f"{TOOL_CALL_MARKER}{block}")
            continue
        tool_calls.append(tool_call)

    visible_text = "".join(visible_parts)
    return normalize_output_text(visible_text), tool_calls


def choice_tool_calls(choice: dict[str, Any], base_id: str, choice_index: int) -> list[dict[str, Any]]:
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
    _, parsed = extract_tool_calls_from_text(extract_choice_text(choice), base_id, choice_index)
    return parsed


def choice_visible_text(choice: dict[str, Any], base_id: str, choice_index: int) -> str:
    message = choice.get("message")
    if isinstance(message, dict) and isinstance(message.get("tool_calls"), list):
        return normalize_output_text(content_to_text(message.get("content", "")))
    text, _ = extract_tool_calls_from_text(extract_choice_text(choice), base_id, choice_index)
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
                "text": extract_choice_text(choice),
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


def upstream_to_openai_chat(chat_resp: dict[str, Any], model: str) -> dict[str, Any]:
    choices = []
    base_id = chat_resp.get("id", "chatcmpl-novelai-shim")
    for choice in chat_resp.get("choices", []):
        index = choice.get("index", 0)
        text = choice_visible_text(choice, base_id, index)
        tool_calls = choice_tool_calls(choice, base_id, index)
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
    }
    if accept_sse:
        headers["Accept"] = "text/event-stream"
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


def extract_stream_choice_text(choice: dict[str, Any]) -> str:
    if choice.get("text") is not None:
        return str(choice.get("text", "")).replace("\r\n", "\n")
    delta = choice.get("delta")
    if isinstance(delta, dict) and delta.get("content") is not None:
        return content_to_text(delta.get("content", "")).replace("\r\n", "\n")
    if choice.get("message") is not None:
        return content_to_text((choice.get("message") or {}).get("content", "")).replace("\r\n", "\n")
    return ""


def normalize_chat_messages(body: dict[str, Any]) -> list[dict[str, Any]]:
    messages = body.get("messages")
    if isinstance(messages, list) and messages:
        return messages
    if body.get("prompt") is not None:
        return prompt_to_messages(body.get("prompt"))
    return [{"role": "user", "content": ""}]


def messages_to_prompt(messages: list[dict[str, Any]]) -> str:
    lines = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") in ("text", "input_text"):
                    parts.append(part.get("text", ""))
                else:
                    parts.append(str(part))
            content = "".join(parts)
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


def chat_request_from_body(body: dict[str, Any]) -> dict[str, Any]:
    out = {
        "model": body.get("model", "glm-4-6"),
        "prompt": messages_to_prompt(normalize_chat_messages(body)),
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
    return out


def call_upstream(payload: dict[str, Any], upstream: str, api_key: str) -> tuple[int, str]:
    req = build_upstream_request(payload, upstream, api_key)
    try:
        with request.urlopen(req, timeout=120) as response:
            return response.status, response.read().decode("utf-8")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return exc.code, detail


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
    ) -> tuple[list[dict[str, Any]], bool]:
        base_id = upstream_chunk.get("id", "chatcmpl-novelai-shim")
        created = upstream_chunk.get("created") or int(time.time())
        out_events = []
        saw_non_whitespace = False
        for choice in upstream_chunk.get("choices") or []:
            index = choice.get("index", 0)
            state = stream_states.setdefault(index, {"buffer": "", "started_text": False, "tool_mode": False})
            delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
            delta_tool_calls = normalize_delta_tool_calls(delta.get("tool_calls") or choice.get("tool_calls"), base_id, index)
            text = extract_stream_choice_text(choice)
            finish_reason = choice.get("finish_reason")

            if delta_tool_calls:
                payload_delta: dict[str, Any] = {"tool_calls": delta_tool_calls}
                if index not in role_sent:
                    payload_delta["role"] = delta.get("role", "assistant")
                    role_sent.add(index)
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

            if text:
                state["buffer"] += text

            stripped_buffer = state["buffer"].lstrip()
            if TOOL_CALL_MARKER in stripped_buffer:
                state["tool_mode"] = True

            if state["tool_mode"] and finish_reason is not None:
                visible_text, tool_calls = extract_tool_calls_from_text(state["buffer"], base_id, index)
                payload_delta = {}
                if index not in role_sent:
                    payload_delta["role"] = delta.get("role", "assistant")
                    role_sent.add(index)
                if visible_text:
                    payload_delta["content"] = visible_text
                    if visible_text.strip():
                        saw_non_whitespace = True
                if tool_calls:
                    payload_delta["tool_calls"] = tool_calls
                if payload_delta:
                    out_events.append(
                        {
                            "id": base_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model,
                            "choices": [{"index": index, "delta": payload_delta, "finish_reason": None}],
                        }
                    )
                out_events.append(
                    {
                        "id": base_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [{"index": index, "delta": {}, "finish_reason": "tool_calls" if tool_calls else finish_reason}],
                    }
                )
                state["buffer"] = ""
                state["started_text"] = True
                continue

            if not state["tool_mode"] and not state["started_text"]:
                if stripped_buffer and (len(stripped_buffer) >= 24 or "\n" in stripped_buffer or finish_reason is not None):
                    payload_delta = {"content": state["buffer"]}
                    if index not in role_sent:
                        payload_delta["role"] = delta.get("role", "assistant")
                        role_sent.add(index)
                    if state["buffer"].strip():
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
                    state["buffer"] = ""
                    state["started_text"] = True
            elif not state["tool_mode"] and text:
                payload_delta = {"content": text}
                if index not in role_sent:
                    payload_delta["role"] = delta.get("role", "assistant")
                    role_sent.add(index)
                if text.strip():
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

            if finish_reason is not None and not state["tool_mode"]:
                if state["buffer"]:
                    payload_delta = {"content": state["buffer"]}
                    if index not in role_sent:
                        payload_delta["role"] = delta.get("role", "assistant")
                        role_sent.add(index)
                    if state["buffer"].strip():
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
                    state["buffer"] = ""
                    state["started_text"] = True
                out_events.append(
                    {
                        "id": base_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [{"index": index, "delta": {}, "finish_reason": finish_reason}],
                    }
                )
        return out_events, saw_non_whitespace

    def _normalize_upstream_completion_chunk(self, upstream_chunk: dict[str, Any], model: str) -> list[dict[str, Any]]:
        base_id = upstream_chunk.get("id", "cmpl-novelai-shim")
        created = upstream_chunk.get("created") or int(time.time())
        out_events = []
        for choice in upstream_chunk.get("choices") or []:
            text = extract_stream_choice_text(choice)
            finish_reason = choice.get("finish_reason")
            if text or finish_reason is not None:
                out_events.append(
                    {
                        "id": base_id,
                        "object": "text_completion",
                        "created": created,
                        "model": model,
                        "choices": [{"index": choice.get("index", 0), "text": text, "logprobs": None, "finish_reason": finish_reason}],
                    }
                )
        return out_events

    def _stream_plain_chat_response(self, response: Any, model: str) -> None:
        detail = response.read().decode("utf-8")
        chat_resp = upstream_to_openai_chat(json.loads(detail), model)
        self._send_chat_sse(chat_resp)

    def _stream_plain_completion_response(self, response: Any, model: str) -> None:
        detail = response.read().decode("utf-8")
        completion_resp = chat_to_completions(json.loads(detail), model)
        self._send_completions_sse(completion_resp)

    def _proxy_chat_stream(self, payload: dict[str, Any], settings: dict[str, Any], model: str) -> bool:
        role_sent: set[int] = set()
        stream_states: dict[int, dict[str, Any]] = {}
        pending_events: list[dict[str, Any]] = []
        saw_non_whitespace = False
        started = False
        with open_upstream_stream(payload, settings["upstream"], settings["api_key"]) as response:
            content_type = (response.headers.get("Content-Type") or "").lower()
            if "text/event-stream" not in content_type:
                self._stream_plain_chat_response(response, model)
                return True
            for raw_payload in iter_sse_payloads(response):
                if raw_payload == "[DONE]":
                    break
                upstream_chunk = json.loads(raw_payload)
                events, chunk_has_text = self._normalize_upstream_chat_chunk(upstream_chunk, model, role_sent, stream_states)
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
                for event_payload in self._normalize_upstream_completion_chunk(upstream_chunk, model):
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
        upstream_body = chat_request_from_body(body) if is_chat else body

        try:
            model = body.get("model", settings["model"])
            if wants_stream:
                if is_chat:
                    primary_streamed = self._proxy_chat_stream(upstream_body, settings, model)
                    if primary_streamed:
                        return
                    messages = normalize_chat_messages(body)
                    fallback_body = {
                        "model": model,
                        "prompt": last_user_text(messages) or messages_to_prompt(messages),
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
                    if self._proxy_chat_stream(fallback_body, settings, model):
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
            upstream_json = json.loads(detail)
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

            if is_chat:
                chat_resp = upstream_to_openai_chat(upstream_json, model)
                first_message = (((chat_resp.get("choices") or [{}])[0].get("message") or {}))
                content = first_message.get("content") or ""
                tool_calls = first_message.get("tool_calls") or []
                if not content.strip() and not tool_calls:
                    messages = normalize_chat_messages(body)
                    fallback_body = {
                        "model": model,
                        "prompt": last_user_text(messages) or messages_to_prompt(messages),
                        "temperature": body.get("temperature", 1),
                        "top_p": body.get("top_p", 1),
                        "max_tokens": body.get("max_tokens", body.get("max_completion_tokens", 1024)),
                        "stop": body.get("stop") if body.get("stop") is not None else ["\nuser:", "\nsystem:"],
                    }
                    status2, detail2 = call_upstream(fallback_body, settings["upstream"], settings["api_key"])
                    upstream_json2 = json.loads(detail2)
                    if 200 <= status2 < 300:
                        chat_resp = upstream_to_openai_chat(upstream_json2, model)
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
    settings = {
        "api_key": api_key,
        "host": args.host or config["shim"]["host"],
        "port": args.port if args.port is not None else int(config["shim"]["port"]),
        "upstream": args.upstream or config["shim"]["upstream"],
        "model": args.model or config["shim"]["model"],
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
            f"NovelAI shim listening on http://{settings['host']}:{settings['port']}",
            f"NovelAI shim 已启动: http://{settings['host']}:{settings['port']}",
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
