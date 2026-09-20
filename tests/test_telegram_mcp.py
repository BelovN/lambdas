"""Protocol, auth and redaction contract for the telegram-mcp Function URL."""

import json

import pytest
import requests

from conftest import load

mcp = load("telegram-mcp")

TOKEN = "s3cret-token"


def post(body, headers=None, query=None, method="POST"):
    event = {
        "requestContext": {"http": {"method": method, "path": "/"}},
        "headers": headers or {},
        "queryStringParameters": query or {},
        "body": body if isinstance(body, str) else json.dumps(body),
    }
    return mcp.lambda_handler(event, None)


def body_of(response):
    return json.loads(response["body"])


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setenv("MCP_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "111:bot-token")
    monkeypatch.setenv("TELEGRAM_CHAT_IDS", "1,2")


def authed(body, **kw):
    return post(body, headers={"authorization": f"Bearer {TOKEN}"}, **kw)


def rpc(method, params=None, request_id=1):
    request = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        request["params"] = params
    return request



def test_missing_credentials_are_rejected():
    response = post(rpc("ping"))
    assert response["statusCode"] == 401


def test_bearer_token_is_accepted():
    assert authed(rpc("ping"))["statusCode"] == 200


def test_query_parameter_is_accepted_for_clients_that_cannot_set_headers():
    assert post(rpc("ping"), query={"k": TOKEN})["statusCode"] == 200


def test_a_wrong_header_does_not_fall_through_to_the_query_parameter():
    response = post(rpc("ping"), headers={"authorization": "Bearer nope"}, query={"k": TOKEN})
    assert response["statusCode"] == 401


def test_unset_auth_token_fails_closed_without_describing_the_server(monkeypatch):
    # The endpoint is unauthenticated at this point: it must not explain itself.
    monkeypatch.delenv("MCP_AUTH_TOKEN")
    response = post(rpc("ping"))
    assert response["statusCode"] == 503
    assert "MCP_AUTH_TOKEN" not in response["body"]


def test_get_is_not_served():
    # Stateless: there is no SSE stream to open.
    response = post(rpc("ping"), headers={"authorization": f"Bearer {TOKEN}"}, method="GET")
    assert response["statusCode"] == 405
    assert response["headers"]["Allow"] == "POST"



def test_server_discover_is_implemented():
    # Answering -32601 here makes a modern client report the server unreachable.
    result = body_of(authed(rpc("server/discover")))["result"]
    assert result["supportedVersions"] == list(mcp.SUPPORTED_PROTOCOL_VERSIONS)
    assert result["capabilities"] == {"tools": {}}
    assert result["resultType"] == "complete"
    assert result["ttlMs"] and result["cacheScope"]


def test_every_result_identifies_the_server():
    for method in ("server/discover", "ping", "tools/list"):
        result = body_of(authed(rpc(method)))["result"]
        assert result["_meta"]["io.modelcontextprotocol/serverInfo"] == mcp.SERVER_INFO


@pytest.mark.parametrize("version", mcp.SUPPORTED_PROTOCOL_VERSIONS)
def test_initialize_echoes_a_supported_version(version):
    result = body_of(authed(rpc("initialize", {"protocolVersion": version})))["result"]
    assert result["protocolVersion"] == version


def test_initialize_falls_back_to_latest_for_an_unknown_version():
    result = body_of(authed(rpc("initialize", {"protocolVersion": "1999-01-01"})))["result"]
    assert result["protocolVersion"] == mcp.LATEST_PROTOCOL_VERSION


def test_tools_list_carries_the_cache_contract():
    result = body_of(authed(rpc("tools/list")))["result"]
    assert [tool["name"] for tool in result["tools"]] == ["send_notification"]
    assert result["ttlMs"] == mcp.LIST_TTL_MS
    assert result["cacheScope"] == "public"


def test_a_notification_gets_no_body():
    response = authed({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert response["statusCode"] == 202
    assert response["body"] == ""


def test_unknown_method_is_a_protocol_error():
    assert body_of(authed(rpc("nope")))["error"]["code"] == mcp.METHOD_NOT_FOUND


def test_malformed_json_is_a_parse_error():
    assert body_of(authed("{not json"))["error"]["code"] == mcp.PARSE_ERROR


def test_a_batch_is_rejected():
    # JSON-RPC batching was removed from MCP in 2025-06-18.
    response = authed([rpc("ping")])
    assert response["statusCode"] == 400
    assert body_of(response)["error"]["code"] == mcp.INVALID_REQUEST


def test_unknown_tool_is_invalid_params():
    error = body_of(authed(rpc("tools/call", {"name": "nope", "arguments": {}})))["error"]
    assert error["code"] == mcp.INVALID_PARAMS



class FakeResponse:
    def __init__(self, status=200, ok=True):
        self.status_code = status
        self._ok = ok

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} for url https://api.telegram.org/bot111:bot-token/sendMessage")

    def json(self):
        return {"ok": self._ok, "result": {"message_id": 7}}


def call_send(monkeypatch, responder):
    sent = []

    def fake_post(url, timeout=None, json=None):
        sent.append(json)
        return responder(json)

    monkeypatch.setattr(mcp.requests, "post", fake_post)
    response = authed(rpc("tools/call", {"name": "send_notification", "arguments": {"message": "**hi**"}}))
    return body_of(response)["result"], sent


def test_a_successful_send_reports_every_chat(monkeypatch):
    result, sent = call_send(monkeypatch, lambda _: FakeResponse())
    assert result["isError"] is False
    assert "2 chat(s)" in result["content"][0]["text"]
    assert [payload["chat_id"] for payload in sent] == ["1", "2"]
    assert sent[0]["parse_mode"] == "HTML"
    assert sent[0]["text"] == "<b>hi</b>"


def test_a_rejected_markup_is_retried_as_plain_text(monkeypatch):
    # A notification is worth more unformatted than lost.
    def responder(payload):
        return FakeResponse(400) if payload.get("parse_mode") else FakeResponse()

    result, sent = call_send(monkeypatch, responder)
    assert result["isError"] is False
    assert [payload.get("parse_mode") for payload in sent] == ["HTML", None, "HTML", None]
    assert sent[1]["text"] == "**hi**"


def test_partial_delivery_is_reported_as_partial(monkeypatch):
    def responder(payload):
        return FakeResponse(403) if payload["chat_id"] == "2" else FakeResponse()

    result, _ = call_send(monkeypatch, responder)
    text = result["content"][0]["text"]
    assert result["isError"] is True
    assert "delivered to 1 chat(s)" in text
    assert "1 failed" in text


def test_the_bot_token_never_reaches_a_response(monkeypatch):
    result, _ = call_send(monkeypatch, lambda _: FakeResponse(500))
    text = result["content"][0]["text"]
    assert "bot-token" not in text
    assert "***" in text


def test_an_empty_message_is_a_tool_error_not_a_protocol_error(monkeypatch):
    response = authed(rpc("tools/call", {"name": "send_notification", "arguments": {"message": "  "}}))
    result = body_of(response)["result"]
    assert response["statusCode"] == 200
    assert result["isError"] is True


def test_an_oversized_message_is_refused_before_any_send(monkeypatch):
    def explode(*a, **kw):
        raise AssertionError("must not reach Telegram")

    monkeypatch.setattr(mcp.requests, "post", explode)
    oversized = "x" * (mcp.TELEGRAM_MAX_MESSAGE_CHARS + 1)
    result = body_of(
        authed(rpc("tools/call", {"name": "send_notification", "arguments": {"message": oversized}}))
    )["result"]
    assert result["isError"] is True


def test_missing_configuration_is_a_tool_error(monkeypatch):
    monkeypatch.delenv("TELEGRAM_CHAT_IDS")
    result = body_of(
        authed(rpc("tools/call", {"name": "send_notification", "arguments": {"message": "hi"}}))
    )["result"]
    assert result["isError"] is True
