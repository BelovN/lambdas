"""Contract for the defa-luci stock poller.

The Shopify feed is third-party and untyped, so the parsing tests feed in
malformed shapes on purpose: a bad product must be skipped, never crash the run.
"""

import json

import pytest
import requests

from conftest import load

luci = load("defa-luci")


def product(**overrides):
    base = {
        "id": 1,
        "title": "Doll",
        "handle": "doll",
        "variants": [{"available": True, "price": "10.00"}],
        "images": [{"src": "https://cdn/1.jpg"}],
    }
    base.update(overrides)
    return base



def test_a_product_counts_when_any_variant_is_available():
    feed = [product(variants=[{"available": False, "price": "9"}, {"available": True, "price": "9"}])]
    assert len(luci.extract_available_products(feed)) == 1


def test_a_sold_out_product_is_skipped():
    assert luci.extract_available_products([product(variants=[{"available": False}])]) == []


def test_the_cheapest_available_variant_sets_the_price():
    feed = [
        product(
            variants=[
                {"available": True, "price": "10.50"},
                {"available": True, "price": "8"},
                # A cheaper variant that is out of stock must not win.
                {"available": False, "price": "1"},
            ]
        )
    ]
    assert luci.extract_available_products(feed)[0]["price"] == "8.00 USD"


def test_available_must_be_exactly_true():
    # The feed is untyped: a truthy string is not an availability signal.
    assert luci.extract_available_products([product(variants=[{"available": "yes"}])]) == []


@pytest.mark.parametrize(
    "broken",
    [
        {"id": 1, "variants": "not-a-list"},
        {"id": 1, "variants": None},
        {"id": 1},
    ],
)
def test_a_malformed_product_is_skipped_rather_than_raising(broken):
    assert luci.extract_available_products([broken]) == []


def test_an_unparseable_price_leaves_the_price_empty():
    feed = [product(variants=[{"available": True, "price": "n/a"}])]
    assert luci.extract_available_products(feed)[0]["price"] is None


def test_a_missing_image_is_not_an_error():
    for images in (None, [], "x", [{"no_src": 1}]):
        assert luci.extract_available_products([product(images=images)])[0]["image"] is None


def test_the_product_url_is_built_from_the_store_base():
    assert luci.extract_available_products([product(handle="rosa")])[0]["url"] == (
        f"{luci.BASE_URL}/products/rosa"
    )



def test_the_message_stays_russian_and_lists_every_product():
    available = luci.extract_available_products([product(title="A"), product(title="B", handle="b")])
    message = luci.build_telegram_message(available)
    assert message.startswith("Найдены доступные куклы:")
    assert "1. A" in message and "2. B" in message
    assert "Цена: 10.00 USD" in message


def test_a_product_without_a_price_omits_the_price_line():
    available = luci.extract_available_products([product(variants=[{"available": True}])])
    assert "Цена:" not in luci.build_telegram_message(available)



@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "111:bot-token")
    monkeypatch.setenv("TELEGRAM_CHAT_IDS", "1, 2 ,")


def test_chat_ids_are_split_and_trimmed():
    assert luci.load_telegram_config() == ("111:bot-token", ["1", "2"])


@pytest.mark.parametrize("missing", ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_IDS"])
def test_missing_configuration_raises(monkeypatch, missing):
    monkeypatch.setenv(missing, "")
    with pytest.raises(ValueError):
        luci.load_telegram_config()


def test_the_bot_token_is_redacted():
    assert luci.redact("https://api.telegram.org/bot111:bot-token/x") == (
        "https://api.telegram.org/bot***/x"
    )


def test_nothing_in_stock_sends_nothing(monkeypatch):
    monkeypatch.setattr(luci, "fetch_collection_products", lambda: [product(variants=[])])
    monkeypatch.setattr(
        luci.requests, "post", lambda *a, **kw: pytest.fail("must not message Telegram")
    )
    body = json.loads(luci.lambda_handler({}, None)["body"])
    assert body["available_count"] == 0
    assert body["telegram_sent"] is False


def test_an_upstream_http_error_maps_to_502(monkeypatch):
    def explode():
        raise requests.HTTPError("500 for url https://api.telegram.org/bot111:bot-token/x")

    monkeypatch.setattr(luci, "fetch_collection_products", explode)
    response = luci.lambda_handler({}, None)
    assert response["statusCode"] == 502
    # The same trap as everywhere else: requests puts the URL in the message.
    assert "bot-token" not in response["body"]


def test_missing_configuration_maps_to_500(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    assert luci.lambda_handler({}, None)["statusCode"] == 500


def test_a_failing_chat_currently_aborts_the_whole_send(monkeypatch):
    """Known gap, pinned so a fix is a deliberate change.

    telegram-mcp reports partial delivery; this function still raises on the
    first failing chat id, so chats already messaged are reported as a total
    failure and the next run re-sends to them.
    """
    calls = []

    class Response:
        def __init__(self, chat_id):
            self.chat_id = chat_id

        def raise_for_status(self):
            if self.chat_id == "1":
                raise requests.HTTPError("403 Forbidden")

        def json(self):
            return {"ok": True, "result": {"message_id": 1}}

    def fake_post(url, timeout=None, json=None):
        calls.append(json["chat_id"])
        return Response(json["chat_id"])

    monkeypatch.setattr(luci.requests, "post", fake_post)
    with pytest.raises(requests.HTTPError):
        luci.send_telegram_messages("t", ["1", "2"], "text")
    assert calls == ["1"], "chat 2 is never attempted"
