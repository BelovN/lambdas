import json
import os
from typing import Any

import requests

BASE_URL = "https://defalucy.com"

# Collections polled on every run, in the order their products appear in the
# message. Hardcoded: the store publishes six collections and only these two
# carry dolls. Handle -> the Russian label used in the Telegram message.
COLLECTIONS: dict[str, str] = {
    "dolls": "Куклы",
    "series-punk-1": "Series Punk",
}

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; AWSLambda/1.0; +https://aws.amazon.com/lambda/)"
    ),
    "Accept": "application/json",
}
JSON_HEADERS = {"Content-Type": "application/json; charset=utf-8"}

# One invocation now makes at least one request per collection before it sends
# anything, so the per-request budget has to leave room for all of them inside
# the Lambda timeout. Pinned by test_the_worst_case_request_time_fits_the_lambda_timeout.
TIMEOUT_SECONDS = 10
PAGE_LIMIT = 250


def collection_api_url(handle: str) -> str:
    return f"{BASE_URL}/collections/{handle}/products.json"


def collection_page_url(handle: str) -> str:
    return f"{BASE_URL}/collections/{handle}"


def json_response(status_code: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": JSON_HEADERS,
        "body": json.dumps(payload, ensure_ascii=False),
    }


def redact(text: str) -> str:
    # requests puts the full URL into HTTPError messages, and the Telegram URL
    # carries the bot token. Never let it reach the response body.
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    return text.replace(token, "***") if token else text


def load_telegram_config() -> tuple[str, list[str]]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ValueError("Environment variable TELEGRAM_BOT_TOKEN is not set")

    raw_chat_ids = os.environ.get("TELEGRAM_CHAT_IDS", "")
    chat_ids = [chat_id.strip() for chat_id in raw_chat_ids.split(",") if chat_id.strip()]
    if not chat_ids:
        raise ValueError("Environment variable TELEGRAM_CHAT_IDS is empty or not set")

    return token, chat_ids


def fetch_collection_products(handle: str) -> list[dict[str, Any]]:
    products: list[dict[str, Any]] = []
    page = 1

    while True:
        response = requests.get(
            collection_api_url(handle),
            headers=HEADERS,
            timeout=TIMEOUT_SECONDS,
            params={"limit": PAGE_LIMIT, "page": page},
        )
        response.raise_for_status()

        page_products = response.json().get("products", [])
        if not isinstance(page_products, list):
            raise ValueError(f"Invalid Shopify response for {handle}: 'products' is not a list")

        products.extend(page_products)

        # A short page is the last page.
        if len(page_products) < PAGE_LIMIT:
            break

        page += 1

    return products


def fetch_collections(handles: list[str]) -> dict[str, list[dict[str, Any]]]:
    # A failing collection aborts the run: a partial poll would report the
    # missing half as "nothing in stock", which is indistinguishable from a
    # real empty result.
    return {handle: fetch_collection_products(handle) for handle in handles}


def parse_price(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def first_image_src(product: dict[str, Any]) -> str | None:
    images = product.get("images")
    if not isinstance(images, list) or not images:
        return None

    first = images[0]
    return first.get("src") if isinstance(first, dict) else None


def extract_available_products(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    available: list[dict[str, Any]] = []

    for product in products:
        variants = product.get("variants")
        if not isinstance(variants, list):
            continue

        in_stock = [variant for variant in variants if variant.get("available") is True]
        if not in_stock:
            continue

        prices = [
            price
            for price in (parse_price(variant.get("price")) for variant in in_stock)
            if price is not None
        ]
        min_price = min(prices, default=None)
        handle = product.get("handle") or ""

        available.append(
            {
                "id": product.get("id"),
                "title": product.get("title"),
                "handle": product.get("handle"),
                "url": f"{BASE_URL}/products/{handle}",
                "price": f"{min_price:.2f} USD" if min_price is not None else None,
                "available_variants_count": len(in_stock),
                "image": first_image_src(product),
            }
        )

    return available


def product_key(product: dict[str, Any]) -> tuple[str, Any]:
    """Identify a product across collections.

    The feed is untyped, so an id that is not a plain scalar cannot be used as
    a dict key; fall back to the handle, and finally to the object itself so
    that two products we cannot identify are never merged into one.
    """
    product_id = product.get("id")
    if isinstance(product_id, int | str):
        return ("id", product_id)

    handle = product.get("handle")
    if isinstance(handle, str) and handle:
        return ("handle", handle)

    return ("object", id(product))


def merge_available_products(
    products_by_collection: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Available products across every polled collection, listed once each.

    A product can sit in more than one collection, so the collections it was
    found in are collected onto a single entry rather than repeated.
    """
    merged: dict[tuple[str, Any], dict[str, Any]] = {}

    for handle, products in products_by_collection.items():
        for product in extract_available_products(products):
            key = product_key(product)
            existing = merged.get(key)
            if existing is None:
                product["collections"] = [handle]
                merged[key] = product
            elif handle not in existing["collections"]:
                existing["collections"].append(handle)

    return list(merged.values())


def build_telegram_message(products: list[dict[str, Any]]) -> str:
    lines = ["Найдены доступные товары:", ""]

    for index, product in enumerate(products, start=1):
        lines.append(f"{index}. {product['title']}")

        labels = [COLLECTIONS.get(handle, handle) for handle in product.get("collections", [])]
        if labels:
            lines.append(f"Категория: {', '.join(labels)}")

        if product["price"]:
            lines.append(f"Цена: {product['price']}")
        lines.append(f"Ссылка: {product['url']}")
        lines.append(f"Доступных вариантов: {product['available_variants_count']}")
        lines.append("")

    return "\n".join(lines).strip()


def send_telegram_messages(
    token: str, chat_ids: list[str], text: str
) -> list[int | None]:
    url = TELEGRAM_API_URL.format(token=token)
    message_ids: list[int | None] = []

    for chat_id in chat_ids:
        response = requests.post(
            url,
            timeout=TIMEOUT_SECONDS,
            json={
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": False,
            },
        )
        response.raise_for_status()

        payload = response.json()
        if not payload.get("ok"):
            raise ValueError(f"Telegram API error for chat {chat_id}")

        message_ids.append(payload.get("result", {}).get("message_id"))

    return message_ids


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    try:
        token, chat_ids = load_telegram_config()

        handles = list(COLLECTIONS)
        products_by_collection = fetch_collections(handles)
        available_products = merge_available_products(products_by_collection)

        message_ids: list[int | None] = []
        if available_products:
            message = build_telegram_message(available_products)
            message_ids = send_telegram_messages(token, chat_ids, message)

        checked_by_collection = {
            handle: len(products) for handle, products in products_by_collection.items()
        }

        result: dict[str, Any] = {
            "source": "shopify_collection_api",
            "collections": handles,
            "source_urls": {handle: collection_page_url(handle) for handle in handles},
            "checked_products": sum(checked_by_collection.values()),
            "checked_products_by_collection": checked_by_collection,
            "available_count": len(available_products),
            "available_products": available_products,
            "telegram_sent": bool(message_ids),
        }
        if message_ids:
            result["telegram_message_ids"] = message_ids

        return json_response(200, result)

    except requests.HTTPError as exc:
        return json_response(502, {"error": "HTTP error", "details": redact(str(exc))})

    except requests.RequestException as exc:
        return json_response(502, {"error": "Network error", "details": redact(str(exc))})

    except Exception as exc:
        return json_response(500, {"error": "Unexpected error", "details": redact(str(exc))})
