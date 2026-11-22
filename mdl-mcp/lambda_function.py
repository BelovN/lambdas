"""Stateless MCP server over a Lambda Function URL for the MDL doll catalogue admin API.

The caller's MDL service key (`mdl_sk_...`, issued in the portal's /admin/api-keys) is both
the MCP credential and the credential forwarded to the API, so this Lambda holds no secret
and the key's scopes decide which tools succeed.
"""

import base64
import binascii
import ipaddress
import json
import os
import re
import socket
import time
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests

SERVER_NAME = "mdl-mcp"
SERVER_VERSION = "1.0.0"
SERVER_INFO = {"name": SERVER_NAME, "version": SERVER_VERSION}
SERVER_INSTRUCTIONS = (
    "Tools for the MDL doll catalogue admin. Find dolls with search_dolls before creating one, "
    "so nothing is added twice. Brands are referenced by slug: take them from list_brands. "
    "Before filling attribute fields (skin_tone, body_type, item_type, ...) call "
    "get_field_values and reuse an existing spelling where one fits, because filters and the "
    "glossary match values exactly. Photos are attached with upload_doll_photo from a public "
    "image URL; position 0 is the cover. Russian translations are produced automatically after "
    "a doll write; review them with get_doll_translation and update_doll_translation. "
    "Write tools need the matching scope on the API key; a 403 names the missing one."
)

DEFAULT_BASE_URL = "https://medoley.ru"
API_PREFIX = "/api/v1"
LOCALE = "ru"

# 2026-07-28 requires ttlMs/cacheScope on list results. The tool set is static.
LIST_TTL_MS = 3_600_000

# Newest first: an unknown version from the client is answered with LATEST.
SUPPORTED_PROTOCOL_VERSIONS = ("2026-07-28", "2025-06-18", "2025-03-26")
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]

# Per outgoing request (connect and each read, not a deadline). A photo upload makes
# four API/storage requests in sequence plus the download, which has its own deadline
# across every redirect hop; UPLOAD_WORST_CASE_SECONDS must stay under deploy.json's timeout.
TIMEOUT_SECONDS = 15
DOWNLOAD_DEADLINE_SECONDS = 30
USER_AGENT = f"{SERVER_NAME}/{SERVER_VERSION}"

# Mirrors the backend's MEDIA_MAX_BYTES and MEDIA_ALLOWED_CONTENT_TYPES.
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_REDIRECTS = 3
# The last read of a download may block one TIMEOUT_SECONDS past its deadline.
UPLOAD_WORST_CASE_SECONDS = 4 * TIMEOUT_SECONDS + DOWNLOAD_DEADLINE_SECONDS + TIMEOUT_SECONDS

# Mirrors services/api_keys/keys.py in the backend.
API_KEY_RE = re.compile(r"mdl_sk_[A-Za-z0-9]{8}_[A-Za-z0-9]{43}")

# Clients that cannot set headers (the claude.ai connector form takes a URL and
# nothing else) may pass the key as ?k=<key> instead.
QUERY_TOKEN_PARAM = "k"

# JSON-RPC 2.0 error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class ToolInputError(Exception):
    """The arguments are wrong in a way worth telling the model about."""


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


# --- Schemas -----------------------------------------------------------------


def _str(max_length: int | None = None, description: str = "") -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "string", "description": description}
    if max_length:
        schema["maxLength"] = max_length
    return schema


def _str_list(description: str) -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}, "description": description}


# DollCreate in backend/src/schemas/doll.py, with its column limits.
DOLL_FIELDS: dict[str, dict[str, Any]] = {
    "brand": _str(50, "Brand slug, as returned by list_brands."),
    "name": {"type": "string", "minLength": 1, "description": "Doll name."},
    "item_id": _str(20, "Manufacturer item number, e.g. '81110'."),
    "external_id": {"type": "integer", "description": "Id of the doll in an external catalogue."},
    "year": {"type": "integer", "minimum": 1000, "maximum": 9999, "description": "Release year."},
    "collection": _str(200, "Collection the doll belongs to."),
    "line": _str(200, "Product line."),
    "item_type": _str(200, "Item type, e.g. 'Limited Edition'."),
    "head_sculpt": _str(200, "Head sculpt."),
    "body_type": _str(200, "Body type and articulation."),
    "foot_sculpt": _str_list("Foot sculpts the doll comes with."),
    "skin_tone": _str(100, "Skin tone."),
    "hair_color": _str(100, "Hair colour."),
    "sculpt": _str(200, "Sculpt."),
    "eye_screening": _str(200, "Eye screening."),
    "eyebrow_screening": _str(200, "Eyebrow screening."),
    "eyelashes": _str(200, "Eyelashes."),
    "glance": _str(100, "Direction of the glance."),
    "edition_size": {"type": "integer", "minimum": 0, "description": "Edition size."},
    "sale_method": _str(200, "Where and how the doll is sold."),
    "price": {"type": "number", "minimum": 0, "description": "Retail price."},
    "price_currency": _str(3, "ISO 4217 currency of price, e.g. 'USD'."),
    "price_description": _str(description="Free-text note about the price."),
    "deposit_price": {"type": "number", "minimum": 0, "description": "Deposit amount."},
    "deposit_currency": _str(3, "ISO 4217 currency of deposit_price."),
    "deposit_note": _str(description="Free-text note about the deposit."),
    "expected_ship_date": {
        "type": "string",
        "format": "date",
        "description": "Expected shipping date, YYYY-MM-DD.",
    },
    "expected_ship_season": _str(50, "Expected shipping season."),
    "expected_ship_raw": _str(200, "The shipping estimate as the source worded it."),
    "description": _str(description="Description, in English."),
    "included_original": _str_list("What comes in the box, one item per entry."),
}

# The columns that accept NULL; DollUpdate rejects null for every other field.
NULLABLE_DOLL_FIELDS = frozenset(
    {
        "external_id",
        "year",
        "foot_sculpt",
        "edition_size",
        "price",
        "deposit_price",
        "expected_ship_date",
        "included_original",
    }
)

# Multi-valued filters of GET /dolls: values OR within a field, fields AND together.
LIST_FILTERS = (
    "brand",
    "collection",
    "line",
    "item_type",
    "head_sculpt",
    "body_type",
    "sculpt",
    "skin_tone",
    "hair_color",
    "eye_screening",
    "eyebrow_screening",
    "eyelashes",
    "glance",
    "sale_method",
    "expected_ship_season",
)

TRANSLATION_FIELDS: dict[str, dict[str, Any]] = {
    "description": {"type": "string", "minLength": 1, "maxLength": 20000},
    "price_description": {"type": "string", "minLength": 1, "maxLength": 20000},
    "deposit_note": {"type": "string", "minLength": 1, "maxLength": 20000},
    "expected_ship_raw": {"type": "string", "minLength": 1, "maxLength": 500},
    "included_original": {
        "type": "array",
        "items": {"type": "string", "minLength": 1, "maxLength": 500},
        "minItems": 1,
        "maxItems": 100,
    },
}

GLOSSARY_FIELDS = (
    "item_type",
    "body_type",
    "foot_sculpt",
    "skin_tone",
    "hair_color",
    "eye_screening",
    "eyebrow_screening",
    "eyelashes",
    "glance",
    "sale_method",
    "expected_ship_season",
)

ID = {"type": "integer", "minimum": 1}
CROP_VALUE = {"type": ["number", "null"], "minimum": 0, "maximum": 100}


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {**schema, "type": [schema["type"], "null"]}


def _update_doll_properties() -> dict[str, Any]:
    properties: dict[str, Any] = {"doll_id": ID}
    for name, schema in DOLL_FIELDS.items():
        properties[name] = _nullable(schema) if name in NULLABLE_DOLL_FIELDS else schema
    return properties


def _search_properties() -> dict[str, Any]:
    properties: dict[str, Any] = {
        "name": _str(description="Case-insensitive substring of the doll name."),
        "year": {"type": "integer"},
        "year_from": {"type": "integer"},
        "year_to": {"type": "integer"},
    }
    for name in LIST_FILTERS:
        properties[name] = {
            "type": "array",
            "items": {"type": "string"},
            "description": "Exact values; any of them matches."
            + (" Brand slugs." if name == "brand" else ""),
        }
    properties.update(
        {
            "synced": {"type": "boolean", "description": "Whether the doll is in the index."},
            "has_photos": {"type": "boolean"},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20},
            "detail": {
                "type": "string",
                "enum": ["summary", "full"],
                "default": "summary",
                "description": "summary: the identifying fields; full: every field.",
            },
        }
    )
    return properties


def _tool(
    name: str,
    title: str,
    description: str,
    properties: dict[str, Any] | None = None,
    required: tuple[str, ...] = (),
    *,
    read_only: bool = False,
    destructive: bool = False,
    idempotent: bool = False,
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties or {},
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return {
        "name": name,
        "title": title,
        "description": description,
        "inputSchema": schema,
        "annotations": {
            "title": title,
            "readOnlyHint": read_only,
            "destructiveHint": destructive,
            "idempotentHint": read_only or idempotent,
            # The tools only ever talk to the MDL API (and fetch the photo URL given).
            "openWorldHint": False,
        },
    }


TOOLS = [
    _tool(
        "search_dolls",
        "Search dolls",
        "Search the catalogue, newest release first. Filters combine with AND; list filters "
        "match any of their values exactly. Returns total, offset, limit and items.",
        _search_properties(),
        read_only=True,
    ),
    _tool(
        "get_doll",
        "Get doll",
        "Every field of one doll, its photos and its current Russian translation.",
        {"doll_id": ID},
        ("doll_id",),
        read_only=True,
    ),
    _tool(
        "get_field_values",
        "Get field values",
        "The distinct values in use for each attribute field, optionally within one brand. "
        "Use them to spell new values consistently and to build search filters.",
        {"brand": _str(50, "Brand slug to scope the values to.")},
        read_only=True,
    ),
    _tool(
        "create_doll",
        "Create doll",
        "Add a doll. brand and name are required; omitted text fields are stored empty. "
        "Search first so the doll is not added twice. Needs scope dolls:write.",
        DOLL_FIELDS,
        ("brand", "name"),
    ),
    _tool(
        "update_doll",
        "Update doll",
        "Change some fields of a doll; fields not given stay as they are. Null clears the "
        "nullable fields (year, price, edition_size, ...); empty text clears text fields. "
        "Needs scope dolls:write.",
        _update_doll_properties(),
        ("doll_id",),
        idempotent=True,
    ),
    _tool(
        "delete_doll",
        "Delete doll",
        "Permanently delete a doll with its photos and translations. Cannot be undone. "
        "Needs scope dolls:write.",
        {"doll_id": ID},
        ("doll_id",),
        destructive=True,
        idempotent=True,
    ),
    _tool(
        "sync_doll_search",
        "Sync doll to search",
        "Push a doll to the public search index again, for a doll whose meilisearch_synced is "
        "false after an index outage. Needs scope dolls:write.",
        {"doll_id": ID},
        ("doll_id",),
        idempotent=True,
    ),
    _tool(
        "list_brands",
        "List brands",
        "Every brand in display order, with id, slug, name and doll count.",
        read_only=True,
    ),
    _tool(
        "create_brand",
        "Create brand",
        "Add a brand. The slug is permanent. Needs scope brands:write.",
        {
            "slug": {
                "type": "string",
                "pattern": "^[a-z0-9]+(?:-[a-z0-9]+)*$",
                "maxLength": 50,
                "description": "Lowercase words joined by hyphens, e.g. 'integrity-toys'.",
            },
            "name": {"type": "string", "minLength": 1, "maxLength": 100},
        },
        ("slug", "name"),
    ),
    _tool(
        "rename_brand",
        "Rename brand",
        "Change a brand's display name; the slug stays. Needs scope brands:write.",
        {"brand_id": ID, "name": {"type": "string", "minLength": 1, "maxLength": 100}},
        ("brand_id", "name"),
        idempotent=True,
    ),
    _tool(
        "reorder_brands",
        "Reorder brands",
        "Set the display order of brands. brand_ids must list every brand exactly once. "
        "Needs scope brands:write.",
        {"brand_ids": {"type": "array", "items": ID, "minItems": 1}},
        ("brand_ids",),
        idempotent=True,
    ),
    _tool(
        "delete_brand",
        "Delete brand",
        "Delete a brand. Refused while the brand still has dolls. Needs scope brands:write.",
        {"brand_id": ID},
        ("brand_id",),
        destructive=True,
        idempotent=True,
    ),
    _tool(
        "upload_doll_photo",
        "Upload doll photo",
        "Attach a JPEG, PNG or WebP photo (at most 10 MB) to a doll, from a public URL or "
        "from base64 data. Without position it is appended; position 0 is the cover. "
        "Thumbnails are generated in the background. Needs scope dolls:write.",
        {
            "doll_id": ID,
            "image_url": {"type": "string", "description": "Public http(s) URL of the image."},
            "image_base64": {
                "type": "string",
                "description": "The image bytes, base64-encoded, instead of image_url.",
            },
            "position": {"type": "integer", "minimum": 0},
        },
        ("doll_id",),
    ),
    _tool(
        "set_photo_crop",
        "Set photo crop",
        "Set the crop of a photo, as percentages (0-100) of the source image; x + width and "
        "y + height must not exceed 100. Null for all four removes the crop. "
        "Needs scope dolls:write.",
        {
            "media_id": ID,
            "crop_x": CROP_VALUE,
            "crop_y": CROP_VALUE,
            "crop_width": CROP_VALUE,
            "crop_height": CROP_VALUE,
        },
        ("media_id", "crop_x", "crop_y", "crop_width", "crop_height"),
        idempotent=True,
    ),
    _tool(
        "reorder_doll_photos",
        "Reorder doll photos",
        "Set the order of a doll's photos; the first becomes the cover. List every photo id "
        "of the doll (see get_doll). Needs scope dolls:write.",
        {"doll_id": ID, "media_ids": {"type": "array", "items": ID, "minItems": 1}},
        ("doll_id", "media_ids"),
        idempotent=True,
    ),
    _tool(
        "delete_photo",
        "Delete photo",
        "Permanently delete a photo and its stored files. Needs scope dolls:write.",
        {"media_id": ID},
        ("media_id",),
        destructive=True,
        idempotent=True,
    ),
    _tool(
        "get_doll_translation",
        "Get doll translation",
        "The Russian translation of a doll's prose fields: per field the English source, the "
        "translation, its status (missing, machine, reviewed, failed, not_needed) and whether "
        "it is stale. Needs scope translations:read.",
        {"doll_id": ID},
        ("doll_id",),
        read_only=True,
    ),
    _tool(
        "update_doll_translation",
        "Update doll translation",
        "Save reviewed Russian translations; each field given replaces the stored one and is "
        "marked reviewed, so automatic translation leaves it alone until the English changes. "
        "Needs scope translations:write.",
        {"doll_id": ID, **TRANSLATION_FIELDS},
        ("doll_id",),
        idempotent=True,
    ),
    _tool(
        "refresh_doll_translation",
        "Refresh doll translation",
        "Queue a new machine translation of a doll. Reviewed fields are kept unless "
        "include_reviewed is true. Runs in the background. Needs scope translations:write.",
        {"doll_id": ID, "include_reviewed": {"type": "boolean", "default": False}},
        ("doll_id",),
    ),
    _tool(
        "list_glossary_terms",
        "List glossary terms",
        "Russian translations of attribute values, shared by every doll. Filter by field, "
        "status (missing, machine, reviewed) or a substring q. Needs scope glossary:read.",
        {
            "field": {"type": "string", "enum": list(GLOSSARY_FIELDS)},
            "status": {"type": "string", "enum": ["missing", "machine", "reviewed"]},
            "q": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
        },
        read_only=True,
    ),
    _tool(
        "update_glossary_term",
        "Update glossary term",
        "Set the Russian translation of one glossary term; it is marked reviewed. "
        "Needs scope glossary:write.",
        {"term_id": ID, "value": {"type": "string", "minLength": 1, "maxLength": 200}},
        ("term_id", "value"),
        idempotent=True,
    ),
]
TOOL_NAMES = frozenset(tool["name"] for tool in TOOLS)


# --- MDL API -----------------------------------------------------------------


def base_url() -> str:
    url = os.environ.get("MDL_BASE_URL", "").strip() or DEFAULT_BASE_URL
    return url.rstrip("/")


def describe_error(status: int, body: Any) -> str:
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, list):
        # FastAPI validation errors: [{"loc": [...], "msg": "..."}].
        parts = []
        for item in detail:
            if not isinstance(item, dict):
                continue
            loc = ".".join(str(p) for p in item.get("loc") or () if p != "body")
            msg = str(item.get("msg", "invalid"))
            parts.append(f"{loc}: {msg}" if loc else msg)
        detail = "; ".join(parts) or None
    if not isinstance(detail, str) or not detail:
        detail = {
            401: "Unauthorized",
            403: "Forbidden",
            404: "Not found",
            429: "Too many requests",
        }.get(status, "Request failed")

    hint = {
        401: " The API key is invalid, expired or revoked: issue a new one in /admin/api-keys.",
        403: " The API key lacks the scope this tool needs.",
        429: " The API allows 100 requests a minute; wait and retry.",
    }.get(status, "")
    return f"MDL API {status}: {detail.rstrip('.')}.{hint}"


def api_request(
    key: str,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    payload: Any = None,
) -> Any:
    kwargs: dict[str, Any] = {
        "headers": {
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        "timeout": TIMEOUT_SECONDS,
        "allow_redirects": False,
    }
    if params:
        kwargs["params"] = params
    if payload is not None:
        kwargs["json"] = payload
    response = requests.request(method, f"{base_url()}{API_PREFIX}{path}", **kwargs)

    try:
        body = response.json() if response.content else None
    except ValueError:
        body = None
    if response.status_code >= 400:
        raise ApiError(response.status_code, describe_error(response.status_code, body))
    if response.status_code >= 300:
        raise ApiError(response.status_code, f"MDL API {response.status_code}: unexpected redirect")
    return body


# --- Photo upload ------------------------------------------------------------


def sniff_image_type(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def ensure_public_url(url: str) -> None:
    """Refuse anything but http(s) to a globally routable address.

    The Lambda Runtime API listens on 127.0.0.1, so a fetch of a caller-supplied URL must
    never reach loopback or private ranges. Every resolved address is checked; a DNS
    answer that changes between this check and the request is not defended against.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ToolInputError("image_url must be an http(s) URL")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError) as exc:
        raise ToolInputError(f"cannot resolve {parts.hostname}") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise ToolInputError(f"{parts.hostname} is not a public address")


def download_image(url: str) -> bytes:
    deadline = time.monotonic() + DOWNLOAD_DEADLINE_SECONDS

    def remaining() -> float:
        left = deadline - time.monotonic()
        if left <= 0:
            raise ToolInputError(f"image_url took longer than {DOWNLOAD_DEADLINE_SECONDS} s")
        return min(TIMEOUT_SECONDS, left)

    for _ in range(MAX_REDIRECTS + 1):
        ensure_public_url(url)
        with requests.get(
            url,
            stream=True,
            timeout=remaining(),
            allow_redirects=False,
            headers={"User-Agent": USER_AGENT, "Accept": "image/*"},
        ) as response:
            if response.is_redirect:
                url = urljoin(url, response.headers.get("location", ""))
                continue
            if response.status_code != 200:
                raise ToolInputError(f"image_url answered HTTP {response.status_code}")
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > MAX_IMAGE_BYTES:
                raise ToolInputError("the image is larger than 10 MB")
            data = bytearray()
            for chunk in response.iter_content(64 * 1024):
                data.extend(chunk)
                if len(data) > MAX_IMAGE_BYTES:
                    raise ToolInputError("the image is larger than 10 MB")
                # A server trickling bytes must not hold the invocation past the deadline.
                remaining()
            return bytes(data)
    raise ToolInputError(f"image_url redirected more than {MAX_REDIRECTS} times")


def image_from_arguments(arguments: dict[str, Any]) -> bytes:
    url, encoded = arguments.get("image_url"), arguments.get("image_base64")
    if bool(url) == bool(encoded):
        raise ToolInputError("give exactly one of image_url and image_base64")
    if url:
        if not isinstance(url, str):
            raise ToolInputError("image_url must be a string")
        return download_image(url.strip())
    if not isinstance(encoded, str):
        raise ToolInputError("image_base64 must be a string")
    if encoded.startswith("data:"):
        encoded = encoded.partition(",")[2]
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ToolInputError("image_base64 is not valid base64") from exc
    if len(data) > MAX_IMAGE_BYTES:
        raise ToolInputError("the image is larger than 10 MB")
    return data


def expect_fields(what: str, body: Any, *names: str) -> dict[str, Any]:
    """The API answer as a dict carrying each named non-empty string, or an ApiError."""
    if not isinstance(body, dict) or not all(
        isinstance(body.get(n), str) and body.get(n) for n in names
    ):
        raise ApiError(502, f"MDL API returned an unexpected {what} response")
    return body


def upload_doll_photo(key: str, arguments: dict[str, Any]) -> Any:
    doll_id = require_id(arguments, "doll_id")
    position = arguments.get("position")
    if position is not None and (
        isinstance(position, bool) or not isinstance(position, int) or position < 0
    ):
        raise ToolInputError("position must be a non-negative integer")

    # Confirm the doll first: an upload confirmed against a missing doll is
    # refused only after the object is already in storage.
    api_request(key, "GET", f"/dolls/{doll_id}")

    data = image_from_arguments(arguments)
    if not data:
        raise ToolInputError("the image is empty")
    content_type = sniff_image_type(data)
    if content_type is None:
        raise ToolInputError("the image is not JPEG, PNG or WebP")

    presign = expect_fields(
        "presign",
        api_request(
        key,
        "POST",
        "/uploads/presign",
        payload={
            "owner_type": "doll",
            "owner_id": doll_id,
            "content_type": content_type,
            "size": len(data),
        },
        ),
        "upload_url",
        "storage_key",
    )
    try:
        stored = requests.put(
            presign["upload_url"],
            data=data,
            headers=presign.get("headers")
            if isinstance(presign.get("headers"), dict)
            else {"Content-Type": content_type},
            timeout=TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        # The message would carry the presigned URL and its signature.
        raise ApiError(502, f"Upload to storage failed: {type(exc).__name__}") from exc
    if not 200 <= stored.status_code < 300:
        raise ApiError(502, f"Upload to storage failed: HTTP {stored.status_code}")

    confirm: dict[str, Any] = {
        "owner_type": "doll",
        "owner_id": doll_id,
        "storage_key": presign["storage_key"],
    }
    if position is not None:
        confirm["position"] = position
    return compact_media(api_request(key, "POST", "/uploads/confirm", payload=confirm))


# --- Tool handlers -----------------------------------------------------------


def require_id(arguments: dict[str, Any], name: str) -> int:
    value = arguments.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ToolInputError(f"{name} must be a positive integer")
    return value


def pick_fields(
    arguments: dict[str, Any], allowed: set[str] | frozenset[str] | dict[str, Any]
) -> dict[str, Any]:
    unknown = sorted(set(arguments) - set(allowed))
    if unknown:
        raise ToolInputError(f"unknown field(s): {', '.join(unknown)}")
    return dict(arguments)


def compact_media(media: Any) -> Any:
    if not isinstance(media, dict):
        return media
    keep = (
        "id",
        "position",
        "preview_url",
        "thumbnail_url",
        "derivatives_status",
        "crop_x",
        "crop_y",
        "crop_width",
        "crop_height",
    )
    return {k: media.get(k) for k in keep if k in media}


def full_doll(doll: Any) -> Any:
    if isinstance(doll, dict) and isinstance(doll.get("media"), list):
        return {**doll, "media": [compact_media(m) for m in doll["media"]]}
    return doll


def doll_summary(doll: Any) -> Any:
    if not isinstance(doll, dict):
        return doll
    media = doll.get("media") if isinstance(doll.get("media"), list) else []
    cover = compact_media(media[0]) if media else None
    summary = {
        k: doll.get(k)
        for k in (
            "id",
            "brand",
            "name",
            "year",
            "item_id",
            "external_id",
            "collection",
            "line",
            "item_type",
            "edition_size",
            "price",
            "price_currency",
            "meilisearch_synced",
        )
    }
    summary["photo_count"] = len(media)
    summary["cover_url"] = cover.get("thumbnail_url") if isinstance(cover, dict) else None
    return summary


def query_value(value: Any) -> Any:
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def search_dolls(key: str, arguments: dict[str, Any]) -> Any:
    allowed = set(_search_properties())
    args = pick_fields(arguments, allowed)
    detail = args.pop("detail", "summary")
    if detail not in ("summary", "full"):
        raise ToolInputError("detail must be 'summary' or 'full'")
    args.setdefault("limit", 20)

    params: dict[str, Any] = {}
    for name, value in args.items():
        if value is None or value == "" or value == []:
            continue
        if name in LIST_FILTERS:
            value = [value] if isinstance(value, str) else value
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ToolInputError(f"{name} must be a list of strings")
        params[name] = query_value(value)

    page = api_request(key, "GET", "/dolls", params=params)
    if isinstance(page, dict) and isinstance(page.get("items"), list):
        shape = doll_summary if detail == "summary" else full_doll
        page = {**page, "items": [shape(d) for d in page["items"]]}
    return page


def get_doll(key: str, arguments: dict[str, Any]) -> Any:
    return full_doll(api_request(key, "GET", f"/dolls/{require_id(arguments, 'doll_id')}"))


def get_field_values(key: str, arguments: dict[str, Any]) -> Any:
    args = pick_fields(arguments, {"brand"})
    params = {"brand": args["brand"]} if args.get("brand") else None
    return api_request(key, "GET", "/dolls/field-values", params=params)


def create_doll(key: str, arguments: dict[str, Any]) -> Any:
    payload = pick_fields(arguments, DOLL_FIELDS)
    return full_doll(api_request(key, "POST", "/dolls", payload=payload))


def update_doll(key: str, arguments: dict[str, Any]) -> Any:
    doll_id = require_id(arguments, "doll_id")
    payload = pick_fields({k: v for k, v in arguments.items() if k != "doll_id"}, DOLL_FIELDS)
    if not payload:
        raise ToolInputError("give at least one field to change")
    return full_doll(api_request(key, "PATCH", f"/dolls/{doll_id}", payload=payload))


def delete_doll(key: str, arguments: dict[str, Any]) -> Any:
    doll_id = require_id(arguments, "doll_id")
    api_request(key, "DELETE", f"/dolls/{doll_id}")
    return f"Doll {doll_id} deleted."


def sync_doll_search(key: str, arguments: dict[str, Any]) -> Any:
    doll = api_request(key, "POST", f"/dolls/{require_id(arguments, 'doll_id')}/sync")
    return doll_summary(doll)


def list_brands(key: str, arguments: dict[str, Any]) -> Any:
    pick_fields(arguments, set())
    return api_request(key, "GET", "/brands")


def create_brand(key: str, arguments: dict[str, Any]) -> Any:
    return api_request(key, "POST", "/brands", payload=pick_fields(arguments, {"slug", "name"}))


def rename_brand(key: str, arguments: dict[str, Any]) -> Any:
    brand_id = require_id(arguments, "brand_id")
    pick_fields(arguments, {"brand_id", "name"})
    return api_request(key, "PATCH", f"/brands/{brand_id}", payload={"name": arguments.get("name")})


def reorder_brands(key: str, arguments: dict[str, Any]) -> Any:
    pick_fields(arguments, {"brand_ids"})
    payload = {"brand_ids": arguments.get("brand_ids")}
    return api_request(key, "POST", "/brands/reorder", payload=payload)


def delete_brand(key: str, arguments: dict[str, Any]) -> Any:
    brand_id = require_id(arguments, "brand_id")
    api_request(key, "DELETE", f"/brands/{brand_id}")
    return f"Brand {brand_id} deleted."


def set_photo_crop(key: str, arguments: dict[str, Any]) -> Any:
    media_id = require_id(arguments, "media_id")
    crop = {"crop_x", "crop_y", "crop_width", "crop_height"}
    pick_fields(arguments, crop | {"media_id"})
    payload = {name: arguments.get(name) for name in sorted(crop)}
    return compact_media(api_request(key, "PATCH", f"/media/{media_id}", payload=payload))


def reorder_doll_photos(key: str, arguments: dict[str, Any]) -> Any:
    doll_id = require_id(arguments, "doll_id")
    pick_fields(arguments, {"doll_id", "media_ids"})
    payload = {"owner_type": "doll", "owner_id": doll_id, "media_ids": arguments.get("media_ids")}
    media = api_request(key, "POST", "/media/reorder", payload=payload)
    return [compact_media(m) for m in media] if isinstance(media, list) else media


def delete_photo(key: str, arguments: dict[str, Any]) -> Any:
    media_id = require_id(arguments, "media_id")
    api_request(key, "DELETE", f"/media/{media_id}")
    return f"Photo {media_id} deleted."


def get_doll_translation(key: str, arguments: dict[str, Any]) -> Any:
    doll_id = require_id(arguments, "doll_id")
    return api_request(key, "GET", f"/dolls/{doll_id}/translations/{LOCALE}")


def update_doll_translation(key: str, arguments: dict[str, Any]) -> Any:
    doll_id = require_id(arguments, "doll_id")
    payload = pick_fields(
        {k: v for k, v in arguments.items() if k != "doll_id"}, TRANSLATION_FIELDS
    )
    if not payload:
        raise ToolInputError("give at least one field to change")
    return api_request(key, "PATCH", f"/dolls/{doll_id}/translations/{LOCALE}", payload=payload)


def refresh_doll_translation(key: str, arguments: dict[str, Any]) -> Any:
    doll_id = require_id(arguments, "doll_id")
    pick_fields(arguments, {"doll_id", "include_reviewed"})
    payload = {"include_reviewed": bool(arguments.get("include_reviewed", False))}
    return api_request(
        key, "POST", f"/dolls/{doll_id}/translations/{LOCALE}/refresh", payload=payload
    )


def list_glossary_terms(key: str, arguments: dict[str, Any]) -> Any:
    args = pick_fields(arguments, {"field", "status", "q", "offset", "limit"})
    params = {k: v for k, v in args.items() if v not in (None, "")}
    return api_request(key, "GET", "/glossary", params=params)


def update_glossary_term(key: str, arguments: dict[str, Any]) -> Any:
    term_id = require_id(arguments, "term_id")
    pick_fields(arguments, {"term_id", "value"})
    return api_request(
        key, "PATCH", f"/glossary/{term_id}", payload={"value": arguments.get("value")}
    )


HANDLERS = {
    "search_dolls": search_dolls,
    "get_doll": get_doll,
    "get_field_values": get_field_values,
    "create_doll": create_doll,
    "update_doll": update_doll,
    "delete_doll": delete_doll,
    "sync_doll_search": sync_doll_search,
    "list_brands": list_brands,
    "create_brand": create_brand,
    "rename_brand": rename_brand,
    "reorder_brands": reorder_brands,
    "delete_brand": delete_brand,
    "upload_doll_photo": upload_doll_photo,
    "set_photo_crop": set_photo_crop,
    "reorder_doll_photos": reorder_doll_photos,
    "delete_photo": delete_photo,
    "get_doll_translation": get_doll_translation,
    "update_doll_translation": update_doll_translation,
    "refresh_doll_translation": refresh_doll_translation,
    "list_glossary_terms": list_glossary_terms,
    "update_glossary_term": update_glossary_term,
}


# --- MCP plumbing ------------------------------------------------------------


def redact(text: str, key: str) -> str:
    return text.replace(key, "***") if key else text


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


def tool_result(value: Any, is_error: bool = False) -> dict[str, Any]:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def extract_key(headers: dict[str, str], query: dict[str, str]) -> str | None:
    """The caller's MDL key, or None when none is presented or it is malformed.

    The key is only checked for shape here; the MDL API authenticates it. A present
    Authorization header decides on its own and never falls through to ?k=.
    """
    authorization = headers.get("authorization", "").strip()
    if authorization:
        scheme, _, value = authorization.partition(" ")
        candidate = value.strip() if scheme.lower() == "bearer" else ""
    else:
        candidate = (query or {}).get(QUERY_TOKEN_PARAM, "").strip()
    return candidate if API_KEY_RE.fullmatch(candidate) else None


def call_tool(key: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    handler = HANDLERS.get(name)
    if handler is None:
        raise LookupError(name)
    # Tool failures are results the model can read, not JSON-RPC errors.
    try:
        return tool_result(handler(key, arguments))
    except ToolInputError as exc:
        return tool_result(f"Invalid arguments: {redact(str(exc), key)}", is_error=True)
    except ApiError as exc:
        return tool_result(redact(str(exc), key), is_error=True)
    except requests.RequestException as exc:
        return tool_result(
            f"Request failed: {redact(type(exc).__name__ + ': ' + str(exc), key)}",
            is_error=True,
        )


def dispatch(key: str, request: dict[str, Any]) -> dict[str, Any] | None:
    """Return a JSON-RPC response, or None for a notification that needs no reply."""
    request_id = request.get("id")
    method = request.get("method")
    is_notification = "id" not in request

    if request.get("jsonrpc") != "2.0" or not isinstance(method, str):
        if is_notification:
            return None
        return rpc_error(request_id, INVALID_REQUEST, "Invalid JSON-RPC request")

    if is_notification:
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
        supported = requested in SUPPORTED_PROTOCOL_VERSIONS
        negotiated = requested if supported else LATEST_PROTOCOL_VERSION
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
        print(f"tool={name!r}")
        try:
            return rpc_result(request_id, call_tool(key, name, arguments))
        except LookupError:
            return rpc_error(request_id, INVALID_PARAMS, f"Unknown tool: {name}")

    return rpc_error(request_id, METHOD_NOT_FOUND, f"Unknown method: {method}")


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    key = ""
    try:
        headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
        http = (event.get("requestContext") or {}).get("http") or {}
        method = http.get("method", "")

        # Never log the Authorization header or the query string: both carry the key.
        print(
            f"{method} {http.get('path', '')} "
            f"accept={headers.get('accept', '-')!r} ua={headers.get('user-agent', '-')!r}"
        )

        if method.upper() != "POST":
            # Stateless server: no SSE stream to open, so GET has nothing to serve.
            return http_response(405, {"error": "Only POST is supported"}, {"Allow": "POST"})

        key = extract_key(headers, event.get("queryStringParameters") or {}) or ""
        if not key:
            return http_response(
                401,
                {"error": "Unauthorized"},
                {"WWW-Authenticate": 'Bearer realm="mcp"'},
            )

        try:
            raw = event.get("body") or ""
            if event.get("isBase64Encoded"):
                raw = base64.b64decode(raw).decode("utf-8")
            request = json.loads(raw)
        except (TypeError, ValueError):
            return http_response(400, rpc_error(None, PARSE_ERROR, "Malformed JSON"))

        if not isinstance(request, dict):
            # JSON-RPC batching was removed from MCP in 2025-06-18.
            error = rpc_error(None, INVALID_REQUEST, "Expected a single JSON-RPC object")
            return http_response(400, error)

        print(f"rpc method={request.get('method')!r} id={request.get('id')!r}")

        response = dispatch(key, request)
        if response is None:
            return http_response(202, None)
        return http_response(200, response)

    except Exception as exc:
        print(f"Unhandled {type(exc).__name__}: {redact(str(exc), key)}")
        return http_response(500, rpc_error(None, INTERNAL_ERROR, "Internal error"))
