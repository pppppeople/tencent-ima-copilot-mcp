from types import SimpleNamespace

from fastmcp.client.transports import StreamableHttpTransport

import query_ima
from query_ima import content_to_text, mcp_transport


def test_content_to_text_joins_text_items_only():
    result = SimpleNamespace(
        content=[
            SimpleNamespace(text="first"),
            SimpleNamespace(text=None),
            SimpleNamespace(text="second"),
        ]
    )

    assert content_to_text(result) == "first\n\nsecond"


def test_content_to_text_handles_empty_result():
    assert content_to_text(SimpleNamespace(content=[])) == ""


def test_loopback_mcp_transport_disables_environment_proxy(monkeypatch):
    captured = {}
    sentinel = object()

    def fake_async_client(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(query_ima.httpx, "AsyncClient", fake_async_client)
    transport = mcp_transport("http://127.0.0.1:8081/mcp")

    assert isinstance(transport, StreamableHttpTransport)
    assert transport.httpx_client_factory(timeout=10) is sentinel
    assert captured == {
        "follow_redirects": True,
        "timeout": 10,
        "trust_env": False,
    }


def test_remote_mcp_transport_preserves_default_proxy_behavior():
    url = "https://mcp.example.com/mcp"

    assert mcp_transport(url) == url
