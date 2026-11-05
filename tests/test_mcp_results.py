"""Exercise the actual in-memory FastMCP boundary and CLI result contract."""

import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client

import query_ima
from src.ima_client import IMAAPIClient
from src.models import AnswerResult, IMAConfig, KnowledgeBaseMessage, MediaInfo, MessageType, TextMessage


@pytest.fixture
def server(monkeypatch, tmp_path):
    from src import config

    monkeypatch.setenv("IMA_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setenv("IMA_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("IMA_X_IMA_COOKIE", "test-cookie")
    monkeypatch.setenv("IMA_X_IMA_BKN", "test-bkn")
    monkeypatch.setenv("IMA_KNOWLEDGE_BASE_ID", "test-kb")
    monkeypatch.setattr(config, "config_manager", config.ConfigManager())
    module = importlib.import_module("ima_server_simple")
    client = IMAAPIClient(IMAConfig(
        x_ima_cookie="test-cookie", x_ima_bkn="test-bkn",
        knowledge_base_id="test-kb", client_id="test-client",
    ))
    monkeypatch.setattr(module, "ima_client", client)
    monkeypatch.setattr(module, "ensure_client_ready", AsyncMock(return_value=True))
    return module


def answer(status="complete", category=None):
    return AnswerResult(
        status=status,
        messages=[
            TextMessage(type=MessageType.TEXT, content="第一行\n\n第二行。", text="第一行\n\n第二行。"),
            KnowledgeBaseMessage(type=MessageType.KNOWLEDGE_BASE, medias=[
                MediaInfo(id="test-ref", type=1, title="合成资料", introduction="合成简介"),
            ]),
        ] if status != "failed" else [],
        truncated=status == "partial", error_category=category,
        error="回答已截断，未完整接收" if status == "partial" else ("合成失败" if status == "failed" else None),
    )


@pytest.mark.parametrize("status, category", [("complete", None), ("partial", "timeout"), ("failed", "authentication")])
@pytest.mark.parametrize("tool, params", [("ask", {"question": "test"}), ("ask_with_kb", {"question": "test", "knowledge_base_id": "test-kb"})])
async def test_mcp_protocol_preserves_completeness_and_text(server, monkeypatch, status, category, tool, params):
    monkeypatch.setattr(server.ima_client, "ask_question_complete", AsyncMock(return_value=answer(status, category)))
    async with Client(server.mcp) as client:
        result = await client.call_tool(tool, params, raise_on_error=False)
    assert result.is_error is (status != "complete")
    assert result.structured_content == {
        "ok": status == "complete", "status": status, "truncated": status == "partial",
        "error_category": category, "error": answer(status, category).error,
    }
    if status == "complete":
        assert [item.text for item in result.content] == [
            "第一行\n\n第二行。", "### 📚 参考资料\n\n1. **合成资料**\n   > 合成简介\n\n",
        ]
    elif status == "partial":
        assert "第一行\n\n第二行。" in result.content[0].text
        assert "截断" in result.content[0].text and category in result.content[0].text


@pytest.mark.parametrize("params", [{"question": "", "knowledge_base_id": "test-kb"}, {"question": "test", "knowledge_base_id": "disallowed"}])
async def test_validation_errors_use_mcp_error_flag(server, params):
    async with Client(server.mcp) as client:
        result = await client.call_tool("ask_with_kb", params, raise_on_error=False)
    assert result.is_error and result.structured_content["status"] == "failed"
    assert result.structured_content["error_category"] == "validation"


@pytest.mark.parametrize("status, category", [("complete", None), ("partial", "payload"), ("failed", "network")])
async def test_query_reads_real_mcp_structured_result(server, monkeypatch, status, category):
    monkeypatch.setattr(server.ima_client, "ask_question_complete", AsyncMock(return_value=answer(status, category)))
    monkeypatch.setattr(query_ima, "mcp_transport", lambda url: server.mcp)
    payload = await query_ima.query("test")
    assert payload["ok"] is (status == "complete")
    assert payload["status"] == status
    assert payload["truncated"] is (status == "partial")
    assert payload["error_category"] == category
    if status == "partial":
        assert "截断" in payload["answer"]


async def test_query_does_not_infer_success_or_failure_from_text_prefix(monkeypatch):
    result = SimpleNamespace(
        content=[SimpleNamespace(text="[ERROR] 是需要解释的字面文本")],
        structured_content={"ok": True, "status": "complete", "truncated": False},
        is_error=False,
    )

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def call_tool(self, *args, **kwargs):
            assert kwargs["raise_on_error"] is False
            return result

    monkeypatch.setattr(query_ima, "Client", FakeClient)
    assert (await query_ima.query("test"))["ok"] is True
    result.structured_content = None
    payload = await query_ima.query("test")
    assert payload["ok"] is False and payload["status"] == "failed"
