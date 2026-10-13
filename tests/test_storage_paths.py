"""Exercise actual private writers with dummy auth and an isolated home."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("custom_paths", [False, True])
def test_runtime_and_credentials_stay_in_their_selected_private_directories(tmp_path, custom_paths):
    project = Path(__file__).parents[1]
    home = tmp_path / "home"
    working = tmp_path / "working"
    working.mkdir()
    env = {key: value for key, value in os.environ.items() if not key.startswith("IMA_")}
    env.update(
        PYTHONPATH=str(project),
        PYTHONDONTWRITEBYTECODE="1",
        IMA_X_IMA_COOKIE="test-cookie",
        IMA_X_IMA_BKN="test-bkn",
        IMA_KNOWLEDGE_BASE_ID="test-kb",
    )
    runtime = home / ("chosen-runtime" if custom_paths else ".claude/ima")
    credential = home / ("chosen-auth/.env" if custom_paths else ".claude/ima/.env")
    if custom_paths:
        env["IMA_RUNTIME_DIR"] = str(runtime)
        env["IMA_ENV_FILE"] = str(credential)
    script = """
import json, sys
from pathlib import Path
from unittest.mock import patch
with patch('pathlib.Path.home', return_value=Path(sys.argv[1])):
    import ima_server_simple as server
    import ima_auth_receiver as receiver
    import update_cookie
    update_cookie.update_env('test-bkn', 'test-cookie')
    receiver.update_knowledge_base_env('test-kb')
    receiver.log('synthetic auth receiver event')
    print(json.dumps({
        'runtime': str(server.runtime_dir),
        'debug': str(server.log_dir),
        'log': str(server.log_file),
        'receiver': str(receiver.LOG_FILE),
        'credential': str(update_cookie.ENV_FILE),
    }))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(home)],
        cwd=working,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    paths = json.loads(result.stdout.splitlines()[-1])

    assert paths["runtime"] == str(runtime)
    assert paths["debug"] == str(runtime / "logs/debug")
    assert Path(paths["log"]).parent == runtime / "logs/debug"
    assert Path(paths["log"]).is_file()
    assert paths["receiver"] == str(runtime / "logs/ima_auth_receiver.log")
    assert Path(paths["receiver"]).read_text().endswith("synthetic auth receiver event\n")
    assert paths["credential"] == str(credential)
    assert credential.is_file() and credential.stat().st_mode & 0o777 == 0o600
    assert "IMA_KNOWLEDGE_BASE_ID=test-kb" in credential.read_text()
    assert list(working.iterdir()) == []
