"""Stateless MCP server exposing a single send_notification tool over a Lambda Function URL."""

import hmac
import json
import os
from typing import Any

import requests

SERVER_NAME = "telegram-mcp"
SERVER_VERSION = "1.0.0"

# Newest first: an unknown version from the client is answered with LATEST.
SUPPORTED_PROTOCOL_VERSIONS = ("2026-07-28", "2025-06-18", "2025-03-26")
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
TIMEOUT_SECONDS = 15
TELEGRAM_MAX_MESSAGE_CHARS = 4096

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
            "Send a plain-text notification to the Telegram chats this server "
            "is configured with. Returns the number of chats reached."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Notification text.",
                    "minLength": 1,
                    "maxLength": TELEGRAM_MAX_MESSAGE_CHARS,
                }
            },
            "required": ["message"],
            "additionalProperties": False,
        },
    }
]


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


def rpc_result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def tool_result(text: str, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def is_authorized(headers: dict[str, str]) -> bool:
    expected = os.environ.get("MCP_AUTH_TOKEN", "").strip()
    if not expected:
        # Fail closed: an unset token must never mean "open to everyone".
        raise ValueError("Environment variable MCP_AUTH_TOKEN is not set")

    presented = headers.get("authorization", "")
    scheme, _, value = presented.partition(" ")
    if scheme.lower() != "bearer":
        return False
    return hmac.compare_digest(value.strip(), expected)


def send_telegram_message(text: str) -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ValueError("Environment variable TELEGRAM_BOT_TOKEN is not set")

    raw_chat_ids = os.environ.get("TELEGRAM_CHAT_IDS", "")
    chat_ids = [chat_id.strip() for chat_id in raw_chat_ids.split(",") if chat_id.strip()]
    if not chat_ids:
        raise ValueError("Environment variable TELEGRAM_CHAT_IDS is empty or not set")

    url = TELEGRAM_API_URL.format(token=token)
    for chat_id in chat_ids:
        response = requests.post(
            url,
            timeout=TIMEOUT_SECONDS,
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        )
        response.raise_for_status()

        payload = response.json()
        if not payload.get("ok"):
            raise ValueError(f"Telegram API rejected the message for chat {chat_id}")

    return len(chat_ids)


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
        delivered = send_telegram_message(message)
    except (requests.RequestException, ValueError) as exc:
        # Tool failures belong in the result, not in a JSON-RPC protocol error.
        return tool_result(f"Failed to send notification: {redact(str(exc))}", is_error=True)

    return tool_result(f"Notification delivered to {delivered} chat(s).")


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

    if method == "initialize":
        requested = (request.get("params") or {}).get("protocolVersion")
        negotiated = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
        return rpc_result(
            request_id,
            {
                "protocolVersion": negotiated,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )

    if method == "ping":
        return rpc_result(request_id, {})

    if method == "tools/list":
        return rpc_result(request_id, {"tools": TOOLS})

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
        method = ((event.get("requestContext") or {}).get("http") or {}).get("method", "")

        if method.upper() != "POST":
            # Stateless server: no SSE stream to open, so GET has nothing to serve.
            return http_response(405, {"error": "Only POST is supported"}, {"Allow": "POST"})

        if not is_authorized(headers):
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

        response = dispatch(request)
        if response is None:
            return http_response(202, None)
        return http_response(200, response)

    except Exception as exc:
        return http_response(500, rpc_error(None, INTERNAL_ERROR, redact(str(exc))))
