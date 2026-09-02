import json
import os
from typing import Any

import requests

BASE_URL = "https://defalucy.com"
COLLECTION_HANDLE = "dolls"
COLLECTION_API_URL = f"{BASE_URL}/collections/{COLLECTION_HANDLE}/products.json"
COLLECTION_PAGE_URL = f"{BASE_URL}/collections/{COLLECTION_HANDLE}"
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; AWSLambda/1.0; +https://aws.amazon.com/lambda/)"
    ),
    "Accept": "application/json",
}
JSON_HEADERS = {"Content-Type": "application/json; charset=utf-8"}

TIMEOUT_SECONDS = 25
PAGE_LIMIT = 250


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


def fetch_collection_products() -> list[dict[str, Any]]:
    products: list[dict[str, Any]] = []
    page = 1

    while True:
        response = requests.get(
            COLLECTION_API_URL,
            headers=HEADERS,
            timeout=TIMEOUT_SECONDS,
            params={"limit": PAGE_LIMIT, "page": page},
        )
        response.raise_for_status()

        page_products = response.json().get("products", [])
        if not isinstance(page_products, list):
            raise ValueError("Invalid Shopify response: 'products' is not a list")

        products.extend(page_products)

        # A short page is the last page.
        if len(page_products) < PAGE_LIMIT:
            break

        page += 1

    return products


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


def build_telegram_message(products: list[dict[str, Any]]) -> str:
    lines = ["Найдены доступные куклы:", ""]

    for index, product in enumerate(products, start=1):
        lines.append(f"{index}. {product['title']}")
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

        products = fetch_collection_products()
        available_products = extract_available_products(products)

        message_ids: list[int | None] = []
        if available_products:
            message = build_telegram_message(available_products)
            message_ids = send_telegram_messages(token, chat_ids, message)

        result: dict[str, Any] = {
            "source": "shopify_collection_api",
            "collection": COLLECTION_HANDLE,
            "source_url": COLLECTION_PAGE_URL,
            "checked_products": len(products),
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
