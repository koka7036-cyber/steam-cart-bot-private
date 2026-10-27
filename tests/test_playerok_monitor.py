"""No-network contract tests for the optional Playerok adapter."""
import asyncio
import sys
import types
import pytest

from playerok_monitor import PlayerokMonitor


class StopAfterListenerStart(Exception):
    """Stops the adapter test after proving the async listener was started."""


def test_monitor_awaits_async_listener_and_supports_private_router(monkeypatch):
    state = {"listener": None, "started": False}

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
    monkeypatch.setitem(sys.modules, "PyPlayerokAPI", package)
    monkeypatch.setitem(sys.modules, "PyPlayerokAPI.account", account_module)
    monkeypatch.setitem(sys.modules, "PyPlayerokAPI.stream", stream_module)

    monitor = PlayerokMonitor(token="test-token", one_set_item_id="one", five_set_item_id="five")
    with pytest.raises(StopAfterListenerStart):
        asyncio.run(monitor._run())

    assert state["started"] is True
    assert state["listener"] is not None
    assert "on_item_paid" in state["listener"]._router.handlers
    assert "on_new_message" in state["listener"]._router.handlers
