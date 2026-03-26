import argparse
import json
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
    content = message.get("content", "")
    if isinstance(content, list):
        text_parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text_parts.append(part.get("text", ""))
            else:
                text_parts.append(str(part))
        content = "".join(text_parts)
    return str(content)


def normalize_output_text(text: Any) -> str:
    if text is None:
        return ""
    return str(text).replace("\r\n", "\n").lstrip()


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
    for choice in chat_resp.get("choices", []):
        text = normalize_output_text(extract_choice_text(choice))
        choices.append(
            {
                "index": choice.get("index", 0),
                "message": {"role": "assistant", "content": text},
                "finish_reason": choice.get("finish_reason", "stop"),
            }
        )
    usage = chat_resp.get("usage", {}) or {}
    return {
        "id": chat_resp.get("id", "chatcmpl-novelai-shim"),
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
    for key in ["presence_penalty", "frequency_penalty", "n"]:
        if body.get(key) is not None:
            out[key] = body[key]
    return out


def call_upstream(payload: dict[str, Any], upstream: str, api_key: str) -> tuple[int, str]:
    req = request.Request(
        upstream,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
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
            first = True
            for part in chunk_text(text):
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
            status, detail = call_upstream(upstream_body, settings["upstream"], settings["api_key"])
            upstream_json = json.loads(detail)
            model = body.get("model", settings["model"])
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
                content = (((chat_resp.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
                if not content.strip():
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

                if wants_stream:
                    self._send_chat_sse(chat_resp)
                else:
                    self._send_json(200, chat_resp)
                return

            completion_resp = chat_to_completions(upstream_json, model)
            if wants_stream:
                self._send_completions_sse(completion_resp)
            else:
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
