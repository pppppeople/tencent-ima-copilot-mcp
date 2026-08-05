import json

from src.config import ConfigManager
from src.ima_client import IMAAPIClient
from src.models import KnowledgeBaseMessage, MessageType, TextMessage


def make_client(monkeypatch):
    monkeypatch.setenv("IMA_X_IMA_COOKIE", "test-cookie")
    monkeypatch.setenv("IMA_X_IMA_BKN", "test-bkn")
    monkeypatch.setenv("IMA_KNOWLEDGE_BASE_ID", "kb-test")
    config = ConfigManager().get_config()
    return IMAAPIClient(config)


def as_sse(payload):
    return "data: " + json.dumps(payload, ensure_ascii=False)


def test_parses_current_block_message_envelope(monkeypatch):
    client = make_client(monkeypatch)

    message = client._parse_sse_message(as_sse({
        "Type": "blockMessage",
        "Data": {"text_message": {"Text": "最新回答"}},
        "Id": "message-id",
    }))

    assert isinstance(message, TextMessage)
    assert message.type == MessageType.TEXT
    assert message.text == "最新回答"


def test_ignores_current_loading_envelope(monkeypatch):
    client = make_client(monkeypatch)

    message = client._parse_sse_message(as_sse({
        "Type": "loading",
        "Data": {},
        "Id": "loading",
        "UpdateType": "create",
    }))

    assert message is None


def test_parses_current_file_list_envelope(monkeypatch):
    client = make_client(monkeypatch)

    message = client._parse_sse_message(as_sse({
        "Type": "fileList",
        "Data": {
            "file_list": {
                "description": "检索到的资料",
                "files": [{
                    "id": "file-1",
                    "type": 1,
                    "title": "资料标题",
                    "sourceType": 2,
                }],
            },
        },
        "Id": "tool-id",
        "UpdateType": "update",
    }))

    assert isinstance(message, KnowledgeBaseMessage)
    assert message.type == MessageType.KNOWLEDGE_BASE
    assert message.medias[0].title == "资料标题"
    assert message.medias[0].source_type == 2


def test_keeps_legacy_text_payload_support(monkeypatch):
    client = make_client(monkeypatch)

    message = client._parse_sse_message(as_sse({"Text": "旧格式回答"}))

    assert isinstance(message, TextMessage)
    assert message.text == "旧格式回答"


def test_preserves_api_code_frames_for_retry_logic(monkeypatch):
    client = make_client(monkeypatch)

    message = client._parse_sse_message(as_sse({"Code": 3, "Msg": ""}))

    assert message.type == MessageType.SYSTEM
    assert "'Code': 3" in message.content
