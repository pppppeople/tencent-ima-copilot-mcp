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
INVALID_CONTENT_MARKERS = (
    "加入知识库才能继续提问",
    "暂无权限",
    "无权限",
    "知识库不存在",
)
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
            )
        else:
            result = await client.call_tool("ask", {"question": question})
    text = content_to_text(result)
    marker = next((item for item in INVALID_CONTENT_MARKERS if item in text), None)
    is_error = (
        bool(getattr(result, "is_error", False))
        or text.startswith("[ERROR]")
        or bool(marker)
    )
    error = (
        f"IMA 返回提示：{marker}。请在浏览器打开目标 IMA 知识库，确认已加入/有权限后，再点“更新 IMA 登录态御札”书签并在知识库里问一句。"
        if marker
        else None
    )
    payload = {"ok": not is_error, "question": question, "answer": text}
    if error:
        payload["error"] = error
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
