"""Synthetic response consumption, completeness and retry regressions (no IMA IO)."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from tenacity import wait_none

from src.ima_client import AuthenticationError, IMAAPIClient, StreamParseError
from src.models import IMAConfig, KnowledgeBaseMessage, MessageType, TextMessage


def text_frame(text):
    return {"Type": "blockMessage", "Data": {"text_message": {"Text": text}}}


def sse(*frames, trailing_newline=True):
    body = "\n\n".join("data: " + json.dumps(frame, ensure_ascii=False) for frame in frames)
    return (body + ("\n\n" if trailing_newline else "")).encode()


class Response:
    def __init__(self, chunks=(), error=None, content_type="text/event-stream", data=None):
        self.chunks = chunks
        self.error = error
        self.headers = {"content-type": content_type}
        self.data = data
        self.status = 200
        self.closed = False
        self.iterations = 0
        self.json_reads = 0

    @property
    def content(self):
        async def chunks():
            self.iterations += 1
            for chunk in self.chunks:
                yield chunk
            if self.error:
                raise self.error
        return chunks()

    async def json(self):
        self.json_reads += 1
        return self.data

    def close(self):
        self.closed = True


@pytest.fixture
def client():
    return IMAAPIClient(IMAConfig(
        x_ima_cookie="test-cookie", x_ima_bkn="test-bkn",
        knowledge_base_id="test-kb", client_id="test-client", retry_count=0,
    ))


def attach_response(client, monkeypatch, response):
    monkeypatch.setattr(client, "ensure_valid_token", AsyncMock(return_value=True))
    monkeypatch.setattr(client, "init_session", AsyncMock(return_value="test-session"))
    post = AsyncMock(return_value=response)
    monkeypatch.setattr(client, "_get_session", AsyncMock(return_value=SimpleNamespace(post=post)))
    return post


async def collect(client, response):
    return [msg async for msg in client._process_response(response, trace_id="test", question="test")]


@pytest.mark.parametrize("frames", [
    [text_frame("短回答")],
    [{"Text": "旧格式"}],
    [{"content": "旧内容"}],
    [text_frame("甲"), text_frame("乙")],
])
@pytest.mark.parametrize("trailing_newline", [False, True])
async def test_short_sse_consumed_once(client, frames, trailing_newline):
    response = Response([sse(*frames, trailing_newline=trailing_newline)])
    messages = await collect(client, response)
    assert len(messages) == len(frames)
    assert response.iterations == 1 and response.json_reads == 0
    assert response.closed


@pytest.mark.parametrize("chunk_size", [2, 65536])
async def test_references_and_multibyte_chunks_are_not_replayed(client, monkeypatch, chunk_size):
    wire = sse(text_frame("你好"), {
        "Type": "fileList", "Data": {"file_list": {"files": [
            {"id": "ref-1", "type": 1, "title": "合成资料"}
        ]}},
    }, text_frame("！"), trailing_newline=False)
    # 字节切片故意把中文字符拆开。
    response = Response([wire[i:i + chunk_size] for i in range(0, len(wire), chunk_size)])
    attach_response(client, monkeypatch, response)
    result = await client.ask_question_complete("test")
    assert result.status == "complete" and not result.truncated
    assert client._extract_text_content(result.messages) == "你好！"
    assert len(client._extract_knowledge_info(result.messages)) == 1
    assert sum(isinstance(msg, KnowledgeBaseMessage) for msg in result.messages) == 1


@pytest.mark.parametrize("trailing_newline", [False, True])
async def test_bad_frame_is_explicit_partial_without_replay(client, monkeypatch, trailing_newline):
    wire = sse(text_frame("前")) + b'data: {broken}\n\n' + sse(
        text_frame("后"), trailing_newline=trailing_newline
    )
    response = Response([wire])
    attach_response(client, monkeypatch, response)
    result = await client.ask_question_complete("test")
    assert result.status == "partial" and result.truncated
    assert result.error_category == "parse" and "截断" in result.error
    assert client._extract_text_content(result.messages) == "前后"
    assert response.iterations == 1


async def test_only_bad_frames_fail_explicitly(client):
    response = Response([b'data: {broken}'])
    with pytest.raises(StreamParseError):
        await collect(client, response)
    assert response.iterations == 1 and response.closed


async def test_sse_control_fields_and_done_are_not_bad_frames(client):
    response = Response([b': keepalive\nevent: update\nid: 1\nretry: 10\n' + sse(text_frame("ok")) + b'data: [DONE]'])
    messages = await collect(client, response)
    assert client._extract_text_content(messages) == "ok"


async def test_json_legacy_answer_and_reference_have_one_entrypoint(client, monkeypatch):
    response = Response(content_type="application/json; charset=utf-8", data={"msgs": [{
        "type": 3, "content": {
            "answer": json.dumps({"Text": "旧 JSON 回答"}),
            "context_refs": json.dumps({"medias": [{"id": "ref", "type": 1, "title": "资料"}]}),
        },
    }]})
    attach_response(client, monkeypatch, response)
    result = await client.ask_question_complete("test")
    assert result.status == "complete"
    assert client._extract_text_content(result.messages) == "旧 JSON 回答"
    assert len(client._extract_knowledge_info(result.messages)) == 1
    assert response.json_reads == 1 and response.iterations == 0 and response.closed


@pytest.mark.parametrize("error, category", [
    (asyncio.TimeoutError(), "timeout"),
    (aiohttp.ClientPayloadError("synthetic"), "payload"),
    (aiohttp.ClientConnectionError("synthetic"), "network"),
    (RuntimeError("synthetic"), "response"),
])
async def test_stream_interruption_never_becomes_complete(client, monkeypatch, error, category):
    response = Response([sse(text_frame("已收到"))], error=error)
    post = attach_response(client, monkeypatch, response)
    result = await client.ask_question_complete("test")
    assert result.status == "partial" and result.truncated
    assert result.error_category == category and "截断" in result.error
    assert client._extract_text_content(result.messages) == "已收到"
    assert post.await_count == 1 and response.closed


async def test_deadline_during_silent_stream_preserves_partial(client, monkeypatch):
    async def stream(*args, **kwargs):
        yield TextMessage(type=MessageType.TEXT, content="开头", text="开头")
        await asyncio.Event().wait()
    monkeypatch.setattr(client, "init_session", AsyncMock(return_value="session"))
    monkeypatch.setattr(client, "ask_question", stream)
    result = await client.ask_question_complete("test", timeout=0.02)
    assert result.status == "partial" and result.error_category == "timeout"
    assert client._extract_text_content(result.messages) == "开头"


async def test_no_text_network_failure_is_retried(client, monkeypatch):
    import src.ima_client as module
    monkeypatch.setattr(module, "wait_exponential", lambda **kwargs: wait_none())
    client.config.retry_count = 1
    responses = [Response(error=aiohttp.ClientPayloadError("synthetic")), Response([sse(text_frame("成功"))])]
    post = attach_response(client, monkeypatch, responses[0])
    post.side_effect = responses
    result = await client.ask_question_complete("test")
    assert result.status == "complete" and post.await_count == 2
    assert client._extract_text_content(result.messages) == "成功"


async def test_no_text_exhausted_network_failure_has_category(client, monkeypatch):
    attach_response(client, monkeypatch, Response(error=aiohttp.ClientConnectionError("synthetic")))
    result = await client.ask_question_complete("test")
    assert result.status == "failed" and result.error_category == "network"
    assert not result.messages and not result.truncated


@pytest.mark.parametrize("error", [asyncio.TimeoutError(), aiohttp.ClientConnectionError("synthetic")])
async def test_refresh_network_errors_reach_tenacity(client, monkeypatch, error):
    client.config.user_id = "test-user"
    client.config.refresh_token = "test-refresh"
    response = SimpleNamespace(status=200, text=AsyncMock(return_value='{}'), json=AsyncMock(return_value={
        "code": 0, "msg": "ok", "token": "test-access", "token_valid_time": "7200",
    }))
    calls = []

    @asynccontextmanager
    async def post(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise error
        yield response

    monkeypatch.setattr(client, "_get_session", AsyncMock(return_value=SimpleNamespace(post=post)))
    refresh = IMAAPIClient.refresh_token.retry_with(wait=wait_none())
    assert await refresh(client) is True
    assert len(calls) == 2 and client._last_auth_error is None


async def test_refresh_auth_failure_is_not_retried_as_network(client, monkeypatch):
    client.config.user_id = "test-user"
    client.config.refresh_token = "test-refresh"
    response = SimpleNamespace(status=200, text=AsyncMock(return_value='{}'), json=AsyncMock(return_value={
        "code": 41, "msg": "synthetic expired",
    }))
    calls = []

    @asynccontextmanager
    async def post(*args, **kwargs):
        calls.append(1)
        yield response

    monkeypatch.setattr(client, "_get_session", AsyncMock(return_value=SimpleNamespace(post=post)))
    assert await client.refresh_token() is False
    assert len(calls) == 1 and client._last_auth_error == "synthetic expired"
    assert not client._should_retry_ask_exception(AuthenticationError("synthetic"))


async def test_code3_exhaustion_is_failed_and_not_success(client, monkeypatch):
    import src.ima_client as module
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
    post = attach_response(client, monkeypatch, Response([sse({"Code": 3, "Msg": ""})]))
    result = await client.ask_question_complete("test")
    assert result.status == "failed" and result.error_category == "api"
    assert post.await_count == 3


async def test_refresh_network_exhaustion_is_not_authentication_failure(client, monkeypatch):
    client.config.user_id = "test-user"
    client.config.refresh_token = "test-refresh"
    calls = []

    @asynccontextmanager
    async def post(*args, **kwargs):
        calls.append(1)
        raise aiohttp.ClientConnectionError("synthetic")
        yield  # async context manager protocol

    monkeypatch.setattr(client, "_get_session", AsyncMock(return_value=SimpleNamespace(post=post)))
    refresh = IMAAPIClient.refresh_token.retry_with(wait=wait_none())
    monkeypatch.setattr(client, "refresh_token", lambda: refresh(client))
    result = await client.ask_question_complete("test")
    # init_session 与 refresh_token 各有 3 次既有重试，网络异常应穿透两层。
    assert len(calls) == 9
    assert result.status == "failed" and result.error_category == "network"
    assert client._last_auth_error is None


async def test_authentication_rejection_is_distinct_from_network(client, monkeypatch):
    monkeypatch.setattr(client, "ensure_valid_token", AsyncMock(return_value=False))
    refresh = AsyncMock(return_value=False)
    monkeypatch.setattr(client, "refresh_token", refresh)
    result = await client.ask_question_complete("test")
    assert result.status == "failed" and result.error_category == "authentication"
    assert refresh.await_count == 1


async def test_json_api_error_does_not_become_success(client, monkeypatch):
    response = Response(content_type="application/json", data={
        "code": 41, "msgs": [{"type": 3, "content": {"answer": "不可视为成功"}}],
    })
    attach_response(client, monkeypatch, response)
    result = await client.ask_question_complete("test")
    assert result.status == "failed" and result.error_category == "api"
    assert response.json_reads == 1 and response.iterations == 0
