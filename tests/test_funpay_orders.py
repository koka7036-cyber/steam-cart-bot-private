"""Regression tests for safe FunPay order matching."""
from funpay_orders import find_open_order_for_chat, mark_input_received


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
