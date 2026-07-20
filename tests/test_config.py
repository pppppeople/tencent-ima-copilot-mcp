from src.config import ConfigManager


def test_required_auth_and_knowledge_base_validate(monkeypatch):
    monkeypatch.setenv("IMA_X_IMA_COOKIE", "test-cookie")
    monkeypatch.setenv("IMA_X_IMA_BKN", "test-bkn")
    monkeypatch.setenv("IMA_KNOWLEDGE_BASE_ID", "kb-test")
    manager = ConfigManager()

    valid, error = manager.validate_config()

    assert valid is True
    assert error is None
    assert manager.get_config().knowledge_base_ids == ["kb-test"]


def test_multiple_knowledge_bases_are_deduplicated(monkeypatch):
    monkeypatch.setenv("IMA_X_IMA_COOKIE", "test-cookie")
    monkeypatch.setenv("IMA_X_IMA_BKN", "test-bkn")
    monkeypatch.setenv("IMA_KNOWLEDGE_BASE_IDS", "kb-a, kb-b, kb-a")
    manager = ConfigManager()

    assert manager.get_config().knowledge_base_id == "kb-a"
    assert manager.get_config().knowledge_base_ids == ["kb-a", "kb-b"]
