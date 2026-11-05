#!/usr/bin/env python3
"""
Query local IMA MCP and print a compact JSON result.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

MCP_URL = os.environ.get("IMA_MCP_URL", "http://127.0.0.1:8081/mcp")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _direct_httpx_client_factory(
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
    follow_redirects: bool = True,
) -> httpx.AsyncClient:
    """Create a local MCP client that cannot inherit an OS-level proxy."""
    kwargs: dict[str, Any] = {
        "follow_redirects": follow_redirects,
        "trust_env": False,
    }
    if headers is not None:
        kwargs["headers"] = headers
    if timeout is not None:
        kwargs["timeout"] = timeout
    if auth is not None:
        kwargs["auth"] = auth
    return httpx.AsyncClient(**kwargs)


def mcp_transport(url: str) -> str | StreamableHttpTransport:
    """Bypass system proxies only for loopback MCP endpoints."""
    if urlsplit(url).hostname in LOOPBACK_HOSTS:
        return StreamableHttpTransport(
            url,
            httpx_client_factory=_direct_httpx_client_factory,
        )
    return url


def content_to_text(result: Any) -> str:
    parts: list[str] = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text:
            parts.append(text)
    return "\n\n".join(parts).strip()


async def query(question: str, knowledge_base_id: str | None = None) -> dict[str, Any]:
    async with Client(mcp_transport(MCP_URL), timeout=180) as client:
        if knowledge_base_id:
            result = await client.call_tool(
                "ask_with_kb",
                {"question": question, "knowledge_base_id": knowledge_base_id},
                raise_on_error=False,
            )
        else:
            result = await client.call_tool("ask", {"question": question}, raise_on_error=False)
    text = content_to_text(result)
    structured = getattr(result, "structured_content", None) or {}
    status = structured.get("status", "failed")
    ok = (
        status == "complete"
        and structured.get("ok") is True
        and not bool(getattr(result, "is_error", False))
    )
    payload = {
        "ok": ok, "question": question, "answer": text,
        "status": status, "truncated": bool(structured.get("truncated", False)),
        "error_category": structured.get("error_category"),
    }
    if not ok:
        payload["error"] = structured.get("error") or "MCP 未返回完整成功结果"
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Query IMA MCP")
    parser.add_argument("question", help="Question to ask IMA")
    parser.add_argument("--kb", help="Optional knowledge base ID")
    args = parser.parse_args()

    try:
        payload = asyncio.run(query(args.question, args.kb))
        print(json.dumps(payload, ensure_ascii=False))
        sys.exit(0 if payload["ok"] else 2)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "question": args.question,
                    "answer": "",
                    "error": str(exc),
                },
                ensure_ascii=False,
            )
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
