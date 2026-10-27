"""No-network contract tests for the optional Playerok adapter."""
import asyncio
import sys
import types
import pytest

from playerok_monitor import PlayerokMonitor


class StopAfterListenerStart(Exception):
    """Stops the adapter test after proving the async listener was started."""


def test_monitor_awaits_async_listener_and_supports_private_router(monkeypatch):
    state = {"listener": None, "started": False, "events": []}

    class FakeRouter:
        def __init__(self):
            self.handlers = {}

        def __getattr__(self, name):
            if not name.startswith("on_"):
                raise AttributeError(name)

            def decorator_factory():
                def decorator(handler):
                    self.handlers[name] = handler
                    return handler
                return decorator
            return decorator_factory

    class FakeAccountClient:
        def __init__(self, *, token, user_agent):
            assert token == "test-token"
            assert user_agent

        @property
        def me(self):
            async def load_profile():
                return types.SimpleNamespace(username="seller-test", id="seller-id")
            return load_profile()

        async def get_deals(self, *, count, statuses, direction, after_cursor):
            assert count == 24
            assert after_cursor is None
            deal = types.SimpleNamespace(
                id="open-deal-1",
                status="PAID",
                item=types.SimpleNamespace(id="one", name="One-set lot https://steamcommunity.com/tradeoffer/new/?partner=1&token=do-not-persist"),
                chat=types.SimpleNamespace(id="chat-1"),
            )
            page_info = types.SimpleNamespace(has_next_page=False, end_cursor=None)
            return types.SimpleNamespace(deals=[deal], page_info=page_info)

    class FakePlayerokListener:
        def __init__(self, account):
            self._router = FakeRouter()
            self.account = account
            state["listener"] = self

        async def start(self):
            state["started"] = True
            raise StopAfterListenerStart()

    package = types.ModuleType("PyPlayerokAPI")
    package.__path__ = []
    account_module = types.ModuleType("PyPlayerokAPI.account")
    account_module.AccountClient = FakeAccountClient
    stream_module = types.ModuleType("PyPlayerokAPI.stream")
    stream_module.PlayerokAccountListener = FakePlayerokListener
    types_package = types.ModuleType("PyPlayerokAPI.types")
    types_package.__path__ = []
    enums_module = types.ModuleType("PyPlayerokAPI.types.enums")
    enums_module.ItemDealStatuses = types.SimpleNamespace(PAID="PAID", PENDING="PENDING")
    enums_module.ItemDealDirections = types.SimpleNamespace(OUT="OUT")
    monkeypatch.setitem(sys.modules, "PyPlayerokAPI", package)
    monkeypatch.setitem(sys.modules, "PyPlayerokAPI.account", account_module)
    monkeypatch.setitem(sys.modules, "PyPlayerokAPI.stream", stream_module)
    monkeypatch.setitem(sys.modules, "PyPlayerokAPI.types", types_package)
    monkeypatch.setitem(sys.modules, "PyPlayerokAPI.types.enums", enums_module)

    monitor = PlayerokMonitor(
        token="test-token",
        one_set_item_id="one",
        five_set_item_id="five",
        on_event=state["events"].append,
    )
    with pytest.raises(StopAfterListenerStart):
        asyncio.run(monitor._run())

    assert state["started"] is True
    assert state["listener"] is not None
    assert "on_item_paid" in state["listener"]._router.handlers
    assert "on_new_message" in state["listener"]._router.handlers
    assert len(state["events"]) == 1
    event = state["events"][0]
    assert event["is_initial_sync"] is True
    assert event["deal_id"] == "open-deal-1"
    assert event["sets"] == 1
    assert "tradeoffer/new" not in event["item_name"]
    assert "do-not-persist" not in event["item_name"]
    assert "trade_url" not in event
    assert monitor.status()["last_sync_count"] == 1
