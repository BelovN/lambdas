"""Stateless MCP server exposing a single send_notification tool over a Lambda Function URL."""

import hmac
import json
import os
import re
from typing import Any

import requests

SERVER_NAME = "telegram-mcp"
SERVER_VERSION = "1.0.0"
SERVER_INFO = {"name": SERVER_NAME, "version": SERVER_VERSION}
SERVER_INSTRUCTIONS = (
    "Use send_notification to deliver a short Markdown-formatted message to "
    "the operator's Telegram chats."
)

# 2026-07-28 requires ttlMs/cacheScope on list results. The tool set is static.
LIST_TTL_MS = 3_600_000

# Newest first: an unknown version from the client is answered with LATEST.
SUPPORTED_PROTOCOL_VERSIONS = ("2026-07-28", "2025-06-18", "2025-03-26")
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
TIMEOUT_SECONDS = 15
TELEGRAM_MAX_MESSAGE_CHARS = 4096

# Clients that cannot set headers (the claude.ai connector form takes a URL and
# nothing else) may pass the same token as ?k=<token> instead.
QUERY_TOKEN_PARAM = "k"

# JSON-RPC 2.0 error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

TOOLS = [
    {
        "name": "send_notification",
        "title": "Send notification",
        "description": (
            "Send a notification to the Telegram chats this server is configured "
            "with. The message is written in Markdown: bold, italic, strikethrough, "
            "inline code, fenced code blocks, links, headings, bullet lists and "
            "block quotes are rendered. Returns the number of chats reached."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Notification text, in Markdown.",
                    "minLength": 1,
                    "maxLength": TELEGRAM_MAX_MESSAGE_CHARS,
                }
            },
            "required": ["message"],
            "additionalProperties": False,
        },
    }
]


_FENCED_RE = re.compile(r"```(\w+)?\n?(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_LINK_RE = re.compile(r"\[([^\]\n]*)\]\(([^)\s]+)\)")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*$")
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+")
_RULE_RE = re.compile(r"^\s*([-*_])\s*(?:\1\s*){2,}$")
# Runs after HTML escaping, so the marker is already "&gt;".
_QUOTE_RE = re.compile(r"^\s{0,3}&gt;\s?(.*)$")

# Telegram's HTML mode understands only a small tag set, so emphasis markers are
# mapped onto it and everything else (headings, lists, rules) degrades to text.
_STYLES = (
    (re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.DOTALL), "b"),
    (re.compile(r"(?<![\w*])__(?=\S)(.+?)(?<=\S)__(?![\w*])", re.DOTALL), "b"),
    (re.compile(r"~~(?=\S)(.+?)(?<=\S)~~", re.DOTALL), "s"),
    (re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])"), "i"),
    (re.compile(r"(?<![\w_])_(?=\S)([^_\n]+?)(?<=\S)_(?![\w_])"), "i"),
)


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def markdown_to_telegram_html(text: str) -> str:
    """Render a practical subset of Markdown into the HTML tags Telegram accepts."""
    text = text.replace("\x00", "")
    code: list[str] = []

    def stash_fenced(m: re.Match[str]) -> str:
        language, body = m.group(1), m.group(2).strip("\n")
        attr = f' class="language-{_escape(language)}"' if language else ""
        code.append(f"<pre><code{attr}>{_escape(body)}</code></pre>")
        return f"\x00{len(code) - 1}\x00"

    def stash_inline(m: re.Match[str]) -> str:
        code.append(f"<code>{_escape(m.group(1))}</code>")
        return f"\x00{len(code) - 1}\x00"

    text = _FENCED_RE.sub(stash_fenced, text)
    text = _INLINE_CODE_RE.sub(stash_inline, text)
    text = _escape(text)

    lines: list[str] = []
    quoted: list[str] = []

    def flush_quote() -> None:
        if quoted:
            lines.append("<blockquote>" + "\n".join(quoted) + "</blockquote>")
            quoted.clear()

    for line in text.split("\n"):
        quote = _QUOTE_RE.match(line)
        if quote:
            quoted.append(quote.group(1))
            continue
        flush_quote()

        heading = _HEADING_RE.match(line)
        if heading and heading.group(1):
            lines.append(f"<b>{heading.group(1)}</b>")
        elif _RULE_RE.match(line):
            lines.append("\u2500" * 12)
        else:
            lines.append(_BULLET_RE.sub(lambda m: f"{m.group(1)}\u2022 ", line))
    flush_quote()
    text = "\n".join(lines)

    urls: list[str] = []

    def stash_link(m: re.Match[str]) -> str:
        urls.append(m.group(2).replace('"', "&quot;"))
        return f'<a href="\x00u{len(urls) - 1}\x00">{m.group(1)}</a>'

    text = _LINK_RE.sub(stash_link, text)

    for pattern, tag in _STYLES:
        text = pattern.sub(lambda m, t=tag: f"<{t}>{m.group(1)}</{t}>", text)

    for index, url in enumerate(urls):
        text = text.replace(f"\x00u{index}\x00", url)
    for index, snippet in enumerate(code):
        text = text.replace(f"\x00{index}\x00", snippet)

    return text


def redact(text: str) -> str:
    # requests puts the request URL into HTTPError messages, and the Telegram
    # URL carries the bot token. Never let it escape the Lambda.
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    return text.replace(token, "***") if token else text


def http_response(status_code: int, payload: Any, extra_headers: dict[str, str] | None = None):
    headers = {
        "Content-Type": "application/json",
        "MCP-Protocol-Version": LATEST_PROTOCOL_VERSION,
    }
    if extra_headers:
        headers.update(extra_headers)
    return {
        "statusCode": status_code,
        "headers": headers,
        "body": "" if payload is None else json.dumps(payload, ensure_ascii=False),
    }


def rpc_result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    # 2026-07-28 requires resultType on every result and asks servers to
    # identify themselves in _meta. Legacy clients must ignore both.
    body: dict[str, Any] = {"resultType": "complete", **result}
    body.setdefault("_meta", {})["io.modelcontextprotocol/serverInfo"] = SERVER_INFO
    return {"jsonrpc": "2.0", "id": request_id, "result": body}


def rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def tool_result(text: str, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def is_authorized(headers: dict[str, str], query: dict[str, str]) -> bool:
    expected = os.environ.get("MCP_AUTH_TOKEN", "").strip()
    if not expected:
        # Fail closed: an unset token must never mean "open to everyone".
        raise ValueError("Environment variable MCP_AUTH_TOKEN is not set")

    scheme, _, value = headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return hmac.compare_digest(value.strip(), expected)

    # The header is preferred: unlike a query string it stays out of access logs.
    from_query = (query or {}).get(QUERY_TOKEN_PARAM, "")
    if from_query:
        return hmac.compare_digest(from_query, expected)

    return False


def send_telegram_message(text: str) -> tuple[int, list[str]]:
    """Deliver to every configured chat; return how many succeeded and why the rest failed."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ValueError("Environment variable TELEGRAM_BOT_TOKEN is not set")

    raw_chat_ids = os.environ.get("TELEGRAM_CHAT_IDS", "")
    chat_ids = [chat_id.strip() for chat_id in raw_chat_ids.split(",") if chat_id.strip()]
    if not chat_ids:
        raise ValueError("Environment variable TELEGRAM_CHAT_IDS is empty or not set")

    url = TELEGRAM_API_URL.format(token=token)
    html = markdown_to_telegram_html(text)
    delivered = 0
    failures: list[str] = []

    def post(chat_id: str, body: str, parse_mode: str | None) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": body,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        response = requests.post(url, timeout=TIMEOUT_SECONDS, json=payload)
        response.raise_for_status()
        if not response.json().get("ok"):
            raise ValueError("Telegram API rejected the message")

    # One bad chat id must not hide the deliveries that did succeed.
    for chat_id in chat_ids:
        try:
            try:
                post(chat_id, html, "HTML")
            except requests.HTTPError:
                # A notification is worth more unformatted than not at all, and
                # only Telegram can judge whether the markup parses.
                post(chat_id, text, None)
        except (requests.RequestException, ValueError) as exc:
            failures.append(f"{chat_id}: {redact(str(exc))}")
        else:
            delivered += 1

    return delivered, failures


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name != "send_notification":
        raise LookupError(name)

    message = arguments.get("message")
    if not isinstance(message, str) or not message.strip():
        return tool_result("'message' must be a non-empty string.", is_error=True)
    if len(message) > TELEGRAM_MAX_MESSAGE_CHARS:
        return tool_result(
            f"Message is {len(message)} characters; Telegram accepts at most "
            f"{TELEGRAM_MAX_MESSAGE_CHARS}.",
            is_error=True,
        )

    try:
        delivered, failures = send_telegram_message(message)
    except ValueError as exc:
        # Tool failures belong in the result, not in a JSON-RPC protocol error.
        return tool_result(f"Failed to send notification: {redact(str(exc))}", is_error=True)

    if not failures:
        return tool_result(f"Notification delivered to {delivered} chat(s).")

    detail = "; ".join(failures)
    if delivered:
        return tool_result(
            f"Notification delivered to {delivered} chat(s); {len(failures)} failed: {detail}",
            is_error=True,
        )
    return tool_result(f"Failed to send notification: {detail}", is_error=True)


def dispatch(request: dict[str, Any]) -> dict[str, Any] | None:
    """Return a JSON-RPC response, or None for a notification that needs no reply."""
    request_id = request.get("id")
    method = request.get("method")
    is_notification = "id" not in request

    if request.get("jsonrpc") != "2.0" or not isinstance(method, str):
        return None if is_notification else rpc_error(request_id, INVALID_REQUEST, "Invalid JSON-RPC request")

    if is_notification:
        # notifications/initialized and friends: accepted, nothing to answer.
        return None

    if method == "server/discover":
        # Servers MUST implement this: it replaced the initialize handshake.
        return rpc_result(
            request_id,
            {
                "supportedVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
                "capabilities": {"tools": {}},
                "instructions": SERVER_INSTRUCTIONS,
                "ttlMs": LIST_TTL_MS,
                "cacheScope": "public",
            },
        )

    if method == "initialize":
        requested = (request.get("params") or {}).get("protocolVersion")
        negotiated = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
        return rpc_result(
            request_id,
            {
                "protocolVersion": negotiated,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
                "instructions": SERVER_INSTRUCTIONS,
            },
        )

    if method == "ping":
        return rpc_result(request_id, {})

    if method == "tools/list":
        return rpc_result(
            request_id,
            {"tools": TOOLS, "ttlMs": LIST_TTL_MS, "cacheScope": "public"},
        )

    if method == "tools/call":
        params = request.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return rpc_error(request_id, INVALID_PARAMS, "Invalid tools/call params")
        try:
            return rpc_result(request_id, call_tool(name, arguments))
        except LookupError:
            return rpc_error(request_id, INVALID_PARAMS, f"Unknown tool: {name}")

    return rpc_error(request_id, METHOD_NOT_FOUND, f"Unknown method: {method}")


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    try:
        headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
        http = (event.get("requestContext") or {}).get("http") or {}
        method = http.get("method", "")

        # Request line for debugging clients. Never log the Authorization header
        # or the query string: both carry the token.
        print(
            f"{method} {http.get('path', '')} "
            f"accept={headers.get('accept', '-')!r} ua={headers.get('user-agent', '-')!r}"
        )

        if method.upper() != "POST":
            # Stateless server: no SSE stream to open, so GET has nothing to serve.
            return http_response(405, {"error": "Only POST is supported"}, {"Allow": "POST"})

        try:
            authorized = is_authorized(headers, event.get("queryStringParameters") or {})
        except ValueError as exc:
            # Misconfiguration is the operator's problem, not the caller's:
            # log the detail, tell an unauthenticated client nothing.
            print(f"Configuration error: {exc}")
            return http_response(503, {"error": "Server is not configured"})

        if not authorized:
            return http_response(
                401,
                {"error": "Unauthorized"},
                {"WWW-Authenticate": 'Bearer realm="mcp"'},
            )

        try:
            request = json.loads(event.get("body") or "")
        except (TypeError, ValueError):
            return http_response(400, rpc_error(None, PARSE_ERROR, "Malformed JSON"))

        if not isinstance(request, dict):
            # JSON-RPC batching was removed from MCP in 2025-06-18.
            return http_response(400, rpc_error(None, INVALID_REQUEST, "Expected a single JSON-RPC object"))

        print(f"rpc method={request.get('method')!r} id={request.get('id')!r}")

        response = dispatch(request)
        if response is None:
            return http_response(202, None)
        return http_response(200, response)

    except Exception as exc:
        return http_response(500, rpc_error(None, INTERNAL_ERROR, redact(str(exc))))
