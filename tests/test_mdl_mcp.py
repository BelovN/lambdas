"""Protocol, auth, tool mapping and upload safety for the mdl-mcp Function URL."""

import base64
import json
import socket

import pytest
import requests

from conftest import load

mcp = load("mdl-mcp")

KEY = "mdl_sk_AbCd1234_" + "x" * 43
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None, content=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        if content is not None:
            self.content = content
        else:
            self.content = b"" if body is None else json.dumps(body).encode()

    def json(self):
        if not self.content:
            raise ValueError("empty")
        return json.loads(self.content)

    @property
    def is_redirect(self):
        return self.status_code in (301, 302, 303, 307, 308) and "location" in self.headers

    def iter_content(self, size):
        for i in range(0, len(self.content), size):
            yield self.content[i : i + size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeApi:
    """Records every MDL API call and answers from a (method, path) -> response map."""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls = []

    def __call__(self, method, url, **kwargs):
        path = url.removeprefix(mcp.DEFAULT_BASE_URL + mcp.API_PREFIX)
        self.calls.append({"method": method, "path": path, **kwargs})
        answer = self.routes.get((method, path), FakeResponse(200, {}))
        return answer(kwargs) if callable(answer) else answer


@pytest.fixture
def api(monkeypatch):
    fake = FakeApi()
    monkeypatch.setattr(mcp.requests, "request", fake)
    monkeypatch.delenv("MDL_BASE_URL", raising=False)
    return fake


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


def rpc(method, params=None, request_id=1):
    request = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        request["params"] = params
    return request


def authed(body, **kw):
    return post(body, headers={"authorization": f"Bearer {KEY}"}, **kw)


def call(name, arguments=None):
    response = authed(rpc("tools/call", {"name": name, "arguments": arguments or {}}))
    assert response["statusCode"] == 200
    result = body_of(response)["result"]
    return result, result["content"][0]["text"]


# --- Auth --------------------------------------------------------------------


def test_missing_credentials_are_rejected():
    assert post(rpc("ping"))["statusCode"] == 401


def test_a_key_of_the_wrong_shape_is_rejected_before_any_api_call(api):
    response = post(rpc("ping"), headers={"authorization": "Bearer mdl_sk_short_key"})
    assert response["statusCode"] == 401
    assert api.calls == []


def test_bearer_key_is_accepted():
    assert authed(rpc("ping"))["statusCode"] == 200


def test_query_parameter_is_accepted_for_clients_that_cannot_set_headers():
    assert post(rpc("ping"), query={"k": KEY})["statusCode"] == 200


def test_a_wrong_header_does_not_fall_through_to_the_query_parameter():
    response = post(rpc("ping"), headers={"authorization": "Bearer nope"}, query={"k": KEY})
    assert response["statusCode"] == 401


def test_the_callers_key_is_forwarded_to_the_api(api):
    call("list_brands")
    assert api.calls[0]["headers"]["Authorization"] == f"Bearer {KEY}"


def test_the_key_never_reaches_a_response_or_the_log(api, capsys, monkeypatch):
    def boom(*args, **kwargs):
        raise requests.ConnectionError(f"failed with {KEY} inside")

    monkeypatch.setattr(mcp.requests, "request", boom)
    _, text = call("list_brands")
    assert KEY not in text
    assert "***" in text
    post(rpc("ping"), query={"k": KEY})
    assert KEY not in capsys.readouterr().out


def test_get_is_not_served():
    response = authed(rpc("ping"), method="GET")
    assert response["statusCode"] == 405


def test_a_base64_encoded_body_is_decoded():
    event = {
        "requestContext": {"http": {"method": "POST", "path": "/"}},
        "headers": {"authorization": f"Bearer {KEY}"},
        "body": base64.b64encode(json.dumps(rpc("ping")).encode()).decode(),
        "isBase64Encoded": True,
    }
    assert mcp.lambda_handler(event, None)["statusCode"] == 200


# --- Protocol ----------------------------------------------------------------


def test_server_discover_is_implemented():
    result = body_of(authed(rpc("server/discover")))["result"]
    assert result["resultType"] == "complete"
    assert "2026-07-28" in result["supportedVersions"]


def test_initialize_echoes_a_supported_version():
    params = {"protocolVersion": "2025-06-18"}
    result = body_of(authed(rpc("initialize", params)))["result"]
    assert result["protocolVersion"] == "2025-06-18"


def test_every_listed_tool_has_a_handler_and_a_closed_schema():
    result = body_of(authed(rpc("tools/list")))["result"]
    names = {tool["name"] for tool in result["tools"]}
    assert names == set(mcp.HANDLERS)
    assert result["ttlMs"] and result["cacheScope"] == "public"
    for tool in result["tools"]:
        assert tool["inputSchema"]["additionalProperties"] is False
        assert set(tool["inputSchema"].get("required", [])) <= set(
            tool["inputSchema"]["properties"]
        )


def test_destructive_tools_are_annotated_as_such():
    destructive = {t["name"] for t in mcp.TOOLS if t["annotations"]["destructiveHint"]}
    assert destructive == {"delete_doll", "delete_brand", "delete_photo"}


def test_api_key_management_is_not_exposed():
    assert not any("key" in name for name in mcp.TOOL_NAMES)


def test_batching_is_rejected():
    assert authed(json.dumps([rpc("ping")]))["statusCode"] == 400


def test_an_unknown_tool_is_a_protocol_error():
    response = authed(rpc("tools/call", {"name": "nope", "arguments": {}}))
    assert body_of(response)["error"]["code"] == mcp.INVALID_PARAMS


def test_the_doll_schema_matches_the_backend_nullable_columns():
    update = next(t for t in mcp.TOOLS if t["name"] == "update_doll")["inputSchema"]
    assert update["properties"]["year"]["type"] == ["integer", "null"]
    assert update["properties"]["name"]["type"] == "string"


# --- Tool mapping ------------------------------------------------------------


def test_search_maps_filters_to_query_parameters_and_summarises(api):
    doll = {
        "id": 7,
        "brand": "mizi",
        "name": "Luna",
        "description": "long text",
        "media": [{"id": 1, "thumbnail_url": "https://media/t.webp", "storage_key": "k"}],
    }
    api.routes[("GET", "/dolls")] = FakeResponse(
        200, {"items": [doll], "total": 1, "offset": 0, "limit": 20}
    )
    _, text = call("search_dolls", {"name": "lu", "brand": "mizi", "has_photos": True})
    params = api.calls[0]["params"]
    assert params == {"name": "lu", "brand": ["mizi"], "has_photos": "true", "limit": 20}
    item = json.loads(text)["items"][0]
    assert item["photo_count"] == 1
    assert item["cover_url"] == "https://media/t.webp"
    assert "description" not in item


def test_search_full_detail_keeps_every_field_but_trims_media(api):
    doll = {"id": 7, "description": "long", "media": [{"id": 1, "storage_key": "k"}]}
    api.routes[("GET", "/dolls")] = FakeResponse(200, {"items": [doll], "total": 1})
    _, text = call("search_dolls", {"detail": "full"})
    item = json.loads(text)["items"][0]
    assert item["description"] == "long"
    assert item["media"] == [{"id": 1}]


def test_create_doll_posts_only_known_fields(api):
    api.routes[("POST", "/dolls")] = FakeResponse(201, {"id": 9, "name": "Luna"})
    call("create_doll", {"brand": "mizi", "name": "Luna", "year": 2026})
    assert api.calls[0]["json"] == {"brand": "mizi", "name": "Luna", "year": 2026}


def test_update_doll_sends_a_partial_patch_with_nulls(api):
    call("update_doll", {"doll_id": 9, "price": None, "skin_tone": "Fair"})
    assert api.calls[0]["method"] == "PATCH"
    assert api.calls[0]["path"] == "/dolls/9"
    assert api.calls[0]["json"] == {"price": None, "skin_tone": "Fair"}


def test_update_doll_without_fields_is_refused_locally(api):
    result, text = call("update_doll", {"doll_id": 9})
    assert result["isError"] and "at least one field" in text
    assert api.calls == []


def test_unknown_doll_fields_are_named(api):
    result, text = call("create_doll", {"brand": "mizi", "name": "x", "colour": "red"})
    assert result["isError"] and "colour" in text
    assert api.calls == []


def test_a_non_integer_id_is_refused_locally(api):
    result, _ = call("get_doll", {"doll_id": "7"})
    assert result["isError"]
    assert api.calls == []


def test_delete_doll_reports_success(api):
    api.routes[("DELETE", "/dolls/3")] = FakeResponse(204)
    result, text = call("delete_doll", {"doll_id": 3})
    assert not result["isError"] and text == "Doll 3 deleted."


def test_reorder_photos_targets_the_doll(api):
    api.routes[("POST", "/media/reorder")] = FakeResponse(200, [{"id": 2, "position": 0}])
    call("reorder_doll_photos", {"doll_id": 4, "media_ids": [2, 1]})
    assert api.calls[0]["json"] == {"owner_type": "doll", "owner_id": 4, "media_ids": [2, 1]}


def test_set_photo_crop_sends_all_four_values(api):
    args = {"media_id": 5, "crop_x": 0, "crop_y": 10, "crop_width": 100, "crop_height": 80}
    call("set_photo_crop", args)
    assert api.calls[0]["json"] == {
        "crop_height": 80,
        "crop_width": 100,
        "crop_x": 0,
        "crop_y": 10,
    }


def test_translation_tools_use_the_russian_locale(api):
    call("update_doll_translation", {"doll_id": 2, "description": "\u041a\u0443\u043a\u043b\u0430"})
    call("refresh_doll_translation", {"doll_id": 2})
    assert api.calls[0]["path"] == "/dolls/2/translations/ru"
    assert api.calls[1]["path"] == "/dolls/2/translations/ru/refresh"
    assert api.calls[1]["json"] == {"include_reviewed": False}


def test_glossary_listing_drops_empty_filters(api):
    call("list_glossary_terms", {"field": "skin_tone", "q": ""})
    assert api.calls[0]["params"] == {"field": "skin_tone"}


# --- API errors --------------------------------------------------------------


def test_api_errors_are_tool_results_with_the_detail(api):
    api.routes[("DELETE", "/brands/1")] = FakeResponse(409, {"detail": "Brand still has dolls"})
    result, text = call("delete_brand", {"brand_id": 1})
    assert result["isError"]
    assert text == "MDL API 409: Brand still has dolls."


def test_validation_errors_are_flattened(api):
    detail = [{"loc": ["body", "year"], "msg": "Input should be less than 9999"}]
    api.routes[("POST", "/dolls")] = FakeResponse(422, {"detail": detail})
    _, text = call("create_doll", {"brand": "mizi", "name": "x", "year": 99999})
    assert "year: Input should be less than 9999" in text


@pytest.mark.parametrize(
    ("status", "hint"),
    [(401, "invalid, expired or revoked"), (403, "lacks the scope"), (429, "100 requests")],
)
def test_auth_and_rate_limit_errors_explain_what_to_do(api, status, hint):
    api.routes[("GET", "/brands")] = FakeResponse(status, {"detail": "x", "code": "y"})
    _, text = call("list_brands")
    assert hint in text


# --- Photo upload ------------------------------------------------------------


@pytest.fixture
def public_dns(monkeypatch):
    def resolve(host, port, **kwargs):
        address = {"localhost": "127.0.0.1", "internal": "10.0.0.5"}.get(host, "93.184.216.34")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]

    monkeypatch.setattr(mcp.socket, "getaddrinfo", resolve)


@pytest.fixture
def upload_api(api, monkeypatch):
    api.routes[("GET", "/dolls/4")] = FakeResponse(200, {"id": 4})
    api.routes[("POST", "/uploads/presign")] = FakeResponse(
        201,
        {
            "upload_url": "https://s3.example/bucket/k?X-Amz-Signature=sig",
            "headers": {"Content-Type": "image/png"},
            "storage_key": "uploads/doll/4/abc.png",
        },
    )
    api.routes[("POST", "/uploads/confirm")] = FakeResponse(
        201, {"id": 11, "position": 0, "storage_key": "uploads/doll/4/abc.png"}
    )
    puts = []

    def put(url, data, headers, timeout):
        puts.append({"url": url, "data": data, "headers": headers})
        return FakeResponse(200)

    monkeypatch.setattr(mcp.requests, "put", put)
    return puts


def test_upload_from_base64_presigns_puts_and_confirms(api, upload_api):
    encoded = base64.b64encode(PNG).decode()
    result, text = call("upload_doll_photo", {"doll_id": 4, "image_base64": encoded})
    assert not result["isError"], text
    presign = next(c for c in api.calls if c["path"] == "/uploads/presign")
    assert presign["json"]["content_type"] == "image/png"
    assert presign["json"]["size"] == len(PNG)
    assert upload_api[0]["data"] == PNG
    assert "Authorization" not in upload_api[0]["headers"]
    assert json.loads(text) == {"id": 11, "position": 0}


def test_upload_from_url_downloads_the_image(api, upload_api, public_dns, monkeypatch):
    monkeypatch.setattr(mcp.requests, "get", lambda url, **kw: FakeResponse(200, content=PNG))
    result, text = call(
        "upload_doll_photo", {"doll_id": 4, "image_url": "https://cdn.example/a.png"}
    )
    assert not result["isError"], text


@pytest.mark.parametrize(
    "url", ["http://localhost:9001/2018-06-01/runtime", "https://internal/a.png", "file:///etc/x"]
)
def test_upload_refuses_non_public_urls(api, upload_api, public_dns, monkeypatch, url):
    monkeypatch.setattr(mcp.requests, "get", lambda *a, **k: pytest.fail("fetched " + url))
    result, _ = call("upload_doll_photo", {"doll_id": 4, "image_url": url})
    assert result["isError"]


def test_a_redirect_to_a_private_address_is_refused(api, upload_api, public_dns, monkeypatch):
    fetched = []

    def get(url, **kwargs):
        fetched.append(url)
        return FakeResponse(302, headers={"location": "http://internal/secret"})

    monkeypatch.setattr(mcp.requests, "get", get)
    result, text = call("upload_doll_photo", {"doll_id": 4, "image_url": "https://cdn.example/a"})
    assert result["isError"] and "not a public address" in text
    assert fetched == ["https://cdn.example/a"]


def test_upload_rejects_what_is_not_an_image(api, upload_api):
    encoded = base64.b64encode(b"<html>").decode()
    result, text = call("upload_doll_photo", {"doll_id": 4, "image_base64": encoded})
    assert result["isError"] and "JPEG, PNG or WebP" in text
    assert upload_api == []


def test_upload_rejects_an_oversized_download(api, upload_api, public_dns, monkeypatch):
    big = PNG + b"\x00" * mcp.MAX_IMAGE_BYTES
    monkeypatch.setattr(mcp.requests, "get", lambda url, **kw: FakeResponse(200, content=big))
    result, text = call("upload_doll_photo", {"doll_id": 4, "image_url": "https://cdn.example/a"})
    assert result["isError"] and "10 MB" in text


def test_upload_checks_the_doll_before_storing_anything(api, upload_api):
    api.routes[("GET", "/dolls/4")] = FakeResponse(404, {"detail": "Doll not found"})
    encoded = base64.b64encode(PNG).decode()
    result, _ = call("upload_doll_photo", {"doll_id": 4, "image_base64": encoded})
    assert result["isError"]
    assert upload_api == []


def test_a_storage_failure_does_not_leak_the_presigned_url(api, upload_api, monkeypatch):
    def put(*args, **kwargs):
        raise requests.ConnectionError("https://s3.example/bucket/k?X-Amz-Signature=sig")

    monkeypatch.setattr(mcp.requests, "put", put)
    encoded = base64.b64encode(PNG).decode()
    result, text = call("upload_doll_photo", {"doll_id": 4, "image_base64": encoded})
    assert result["isError"] and "Signature" not in text


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"\xff\xd8\xff\xe0rest", "image/jpeg"),
        (PNG, "image/png"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
        (b"GIF89a", None),
    ],
)
def test_image_types_are_sniffed_from_the_bytes(data, expected):
    assert mcp.sniff_image_type(data) == expected


def test_the_worst_case_upload_fits_the_lambda_timeout():
    config = json.loads((mcp_dir() / "deploy.json").read_text())
    assert config["timeout"] > mcp.UPLOAD_WORST_CASE_SECONDS


def mcp_dir():
    import pathlib

    return pathlib.Path(mcp.__file__).parent


def test_a_download_past_its_deadline_is_a_tool_error(api, upload_api, public_dns, monkeypatch):
    clock = iter([0, 0, 1000, 1000, 1000])
    monkeypatch.setattr(mcp.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(mcp.requests, "get", lambda url, **kw: FakeResponse(200, content=PNG))
    result, text = call("upload_doll_photo", {"doll_id": 4, "image_url": "https://cdn.example/a"})
    assert result["isError"] and "longer than" in text
    assert upload_api == []


def test_a_malformed_presign_answer_is_a_tool_error(api, upload_api):
    api.routes[("POST", "/uploads/presign")] = FakeResponse(201, {"upload_url": ""})
    encoded = base64.b64encode(PNG).decode()
    result, text = call("upload_doll_photo", {"doll_id": 4, "image_base64": encoded})
    assert result["isError"] and "unexpected presign" in text
    assert upload_api == []


def test_a_boolean_position_is_refused(api, upload_api):
    encoded = base64.b64encode(PNG).decode()
    result, _ = call("upload_doll_photo", {"doll_id": 4, "image_base64": encoded, "position": True})
    assert result["isError"]
