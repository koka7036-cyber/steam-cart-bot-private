"""Regression tests for safe FunPay order matching."""
from funpay_orders import extract_steam_trade_url, find_open_order_for_chat, mark_input_received, register_payment


def make_order(key, node_id="123", status="awaiting_input", updated_at=1):
    return {
        "key": key,
        "node_id": node_id,
        "status": status,
        "updated_at": updated_at,
        "input_kind": None,
        "input_received_at": None,
    }


def test_no_open_order_returns_none():
    assert find_open_order_for_chat({}, "123") is None


def test_single_open_order_is_returned():
    order = make_order("only")
    assert find_open_order_for_chat({"only": order}, "123") is order


def test_multiple_open_orders_in_same_chat_are_ambiguous():
    older = make_order("older", updated_at=10)
    newer = make_order("newer", updated_at=20)
    result = find_open_order_for_chat({"older": older, "newer": newer}, "123")
    assert result is None


def test_closed_order_is_not_considered_open():
    order = make_order("done", status="delivered")
    assert find_open_order_for_chat({"done": order}, "123") is None


def test_orders_from_another_chat_are_ignored():
    order = make_order("other", node_id="999")
    assert find_open_order_for_chat({"other": order}, "123") is None


def test_input_received_updates_status_and_input_kind():
    order = make_order("only")
    mark_input_received(order, "file_attachment")
    assert order["status"] == "input_received"
    assert order["input_kind"] == "file_attachment"
    assert isinstance(order["input_received_at"], int)


def test_repeated_input_does_not_overwrite_first_input():
    order = make_order("only")
    mark_input_received(order, "file_attachment")
    first_received_at = order["input_received_at"]
    mark_input_received(order, "chat_trade_url")
    assert order["input_kind"] == "file_attachment"
    assert order["input_received_at"] == first_received_at


def test_delivered_order_cannot_be_moved_back_by_late_input():
    order = make_order("done", status="delivered")
    mark_input_received(order, "file_attachment")
    assert order["status"] == "delivered"
    assert order["input_kind"] is None



def test_review_order_is_not_overwritten_by_late_input():
    order = make_order("review", status="review")
    mark_input_received(order, "chat_trade_url")
    assert order["status"] == "review"
    assert order["input_kind"] is None



def test_payment_registration_is_idempotent_for_order_number():
    orders = {}
    first, created_first = register_payment(
        orders, node_id="123", buyer="Buyer", text="Заказ #123456 оплачен",
        sets_per_item=5, quantity=2,
    )
    second, created_second = register_payment(
        orders, node_id="123", buyer="Buyer", text="Заказ #123456 оплачен повторно",
        sets_per_item=5, quantity=2,
    )
    assert created_first is True
    assert created_second is False
    assert first is second
    assert first["sets_total"] == 10
    assert len(orders) == 1


def test_different_explicit_order_numbers_do_not_collapse():
    orders = {}
    first, created_first = register_payment(
        orders, node_id="123", buyer="Buyer", text="Заказ #123456 оплачен",
        sets_per_item=1, quantity=1,
    )
    second, created_second = register_payment(
        orders, node_id="123", buyer="Buyer", text="Заказ #123457 оплачен",
        sets_per_item=1, quantity=1,
    )
    assert created_first and created_second
    assert first is not second
    assert len(orders) == 2


def test_message_signature_is_stable_and_does_not_expose_preview():
    from funpay_orders import message_signature
    preview = "https://steamcommunity.com/tradeoffer/new/?partner=123&token=secret"
    first = message_signature("Buyer", preview)
    assert first == message_signature("Buyer", preview)
    assert preview not in first
    assert "secret" not in first


def test_message_signature_changes_when_preview_changes():
    from funpay_orders import message_signature
    assert message_signature("Buyer", "payment") != message_signature("Buyer", "file attached")


def test_extract_steam_trade_url_from_plain_text():
    url = "https://steamcommunity.com/tradeoffer/new/?partner=123456&token=AbCdEf12"
    assert extract_steam_trade_url("Steam trade link: " + url) == url


def test_extract_steam_trade_url_rejects_non_steam_hosts():
    assert extract_steam_trade_url("https://example.com/tradeoffer/new/?partner=123&token=abc") is None


def test_extract_steam_trade_url_rejects_missing_token():
    assert extract_steam_trade_url("https://steamcommunity.com/tradeoffer/new/?partner=123") is None


def test_extract_steam_trade_url_rejects_ambiguous_multiple_links():
    a = "https://steamcommunity.com/tradeoffer/new/?partner=123&token=abc"
    b = "https://steamcommunity.com/tradeoffer/new/?partner=456&token=def"
    assert extract_steam_trade_url(a + "\\n" + b) is None


def test_alphanumeric_funpay_order_number_is_preserved():
    from funpay_orders import extract_order_number, payment_key
    text = "Заказ #DCQQQ6U7H оплачен"
    assert extract_order_number(text) == "DCQQQ6U7H"
    assert payment_key("297759975", text) == "fp-order-DCQQQ6U7H"

