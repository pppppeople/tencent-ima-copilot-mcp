# IMA MCP Connector Rules

This project is the local Tencent IMA Copilot MCP connector and authentication
helper used by the private WeChat bridge.

## Run and verify

```bash
python -m pip install -e '.[dev]'
python -m pytest -q
python -m py_compile ima_server_simple.py query_ima.py src/*.py
./start-mcp.sh
```

## Boundaries

- Authentication values must come from `IMA_ENV_FILE` or environment variables.
- The default private env file is `~/.claude/ima/.env`; never commit it.
- Tests must use dummy credentials and must not contact the real IMA service.
- Keep the MCP endpoint on loopback unless the user explicitly changes the trust
  model.
- This is a maintained fork of the upstream project; keep `upstream` read-only and
  push local changes to the user's fork.

## Structure

- `ima_server_simple.py` exposes MCP tools/resources and the console entrypoint.
- `src/` contains configuration, models, and the IMA API client.
- `query_ima.py` is the local CLI consumer used by other projects.
- `chrome-extension/` and auth receiver update local credentials only.

Current status: active support component. Required next-state contract is a
reproducible dev install, offline tests, and CI before merge.

## Automatic file writes

MCP and auth-receiver logs stay under `IMA_RUNTIME_DIR` (default
`~/.claude/ima`); credential updates target `IMA_ENV_FILE` independently.
Keep these private runtime writes out of source and public artifact folders.
New one-off exports use `artifacts/<task>/` by purpose and update the maintained
README; the storage-path regression uses dummy credentials and an isolated home.

Container launchers must keep their `IMA_RUNTIME_DIR` equal to the persisted
container mount target; the host default belongs to the owner's private
`~/.claude/ima/container-runtime/`, separate from local-service logs.
