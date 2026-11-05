#!/usr/bin/env python3
"""
IMA Copilot MCP 服务器 - 基于环境变量的简化版本
专注于 MCP 协议实现，配置通过环境变量管理
"""

import sys
import asyncio
import os
from datetime import datetime
from pathlib import Path

from fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent
from fastmcp.tools.tool import ToolResult
from loguru import logger

from src.config import config_manager, get_config, get_app_config
from src.ima_client import IMAAPIClient
from src.models import AnswerResult

# 配置详细的调试日志
app_config = get_app_config()

# 创建日志目录
runtime_dir = Path(os.environ.get("IMA_RUNTIME_DIR", Path.home() / ".claude/ima")).expanduser()
log_dir = runtime_dir / "logs/debug"
log_dir.mkdir(parents=True, exist_ok=True)

# 生成带时间戳的日志文件
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
log_file = log_dir / f"ima_server_{timestamp}.log"

# 配置 loguru
logger.remove()  # 移除默认的 sink
logger.add(
    sys.stderr,
    level="INFO",
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level> | <magenta>{extra}</magenta>"
)
logger.add(
    log_file,
    level=app_config.log_level.upper(),
    rotation="10 MB",
    retention="1 week",
    encoding="utf-8",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message} | {extra}"
)

logger.info(f"调试日志已启用，日志文件: {log_file}")

# 创建 FastMCP 实例
mcp = FastMCP("IMA Copilot")

# 全局变量
ima_client: IMAAPIClient = None
_client_init_lock = asyncio.Lock()


def _validate_startup_config() -> tuple[bool, str]:
    """启动配置校验：缺少必需环境变量时阻止服务运行"""
    is_valid, error_message = config_manager.validate_config()
    if is_valid:
        return True, ""

    return False, error_message or "环境变量配置不完整"


_startup_ok, _startup_error = _validate_startup_config()
if not _startup_ok:
    logger.error(f"❌ 启动配置校验失败: {_startup_error}")
    raise SystemExit(1)


def _get_knowledge_base_ids() -> list[str]:
    """获取当前配置中的知识库 ID 列表"""
    config = get_config()
    if not config:
        return []

    kb_ids = [kb_id for kb_id in (config.knowledge_base_ids or []) if kb_id]
    if kb_ids:
        return kb_ids

    return [config.knowledge_base_id] if config.knowledge_base_id else []


def _is_multi_knowledge_base_mode() -> bool:
    return len(_get_knowledge_base_ids()) > 1


def _validate_knowledge_base_id(knowledge_base_id: str) -> tuple[bool, str]:
    kb_id = (knowledge_base_id or "").strip()
    if not kb_id:
        return False, "knowledge_base_id 不能为空"

    allowed_ids = _get_knowledge_base_ids()
    if kb_id not in allowed_ids:
        return False, (
            "knowledge_base_id 不在允许列表中，"
            f"可用值: {', '.join(allowed_ids)}"
        )

    return True, ""


class AnswerToolResult(ToolResult):
    """保留原有文本块，同时把问答终态传到 MCP 协议边界。"""

    def to_mcp_result(self) -> CallToolResult:
        return CallToolResult(
            content=self.content,
            structuredContent=self.structured_content,
            isError=self.structured_content["status"] != "complete",
        )


def _answer_tool_result(result: AnswerResult, content: list[TextContent]) -> AnswerToolResult:
    return AnswerToolResult(
        content=content,
        structured_content={
            "ok": result.status == "complete",
            "status": result.status,
            "truncated": result.truncated,
            "error_category": result.error_category,
            "error": result.error,
        },
    )


def _failed_answer(error: str, category: str) -> AnswerToolResult:
    result = AnswerResult(status="failed", error=error, error_category=category)
    return _answer_tool_result(result, [TextContent(type="text", text=error)])


async def _ask_with_target_kb(question: str, knowledge_base_id: str) -> AnswerToolResult:
    """执行一次指定知识库的问答，并显式交付完整性和错误类别。"""
    if not question or not question.strip():
        return _failed_answer("问题不能为空", "validation")
    valid, error = _validate_knowledge_base_id(knowledge_base_id)
    if not valid:
        return _failed_answer(error, "validation")

    try:
        result = await ima_client.ask_question_complete(
            question, timeout=300, knowledge_base_id=knowledge_base_id.strip()
        )
        response = ima_client._extract_text_content(result.messages) if result.messages else ""
        if result.status == "failed":
            return _answer_tool_result(result, [TextContent(type="text", text=result.error or "询问失败")])
        if result.status == "partial":
            response += f"\n\n[截断] 回答未完整接收（{result.error_category}），请重试。"
        content = [TextContent(type="text", text=response)]

        # 完整回答沿用原有文本及参考资料格式。
        knowledge_info = ima_client._extract_knowledge_info(result.messages)
        if knowledge_info:
            ref_text = "### 📚 参考资料\n\n"
            for i, item in enumerate(knowledge_info, 1):
                title = item.get('title', '未知标题')
                intro = item.get('introduction', '')
                if intro and len(intro) > 150:
                    intro = intro[:150] + "..."
                ref_text += f"{i}. **{title}**\n"
                if intro:
                    ref_text += f"   > {intro}\n"
                ref_text += "\n"
            content.append(TextContent(type="text", text=ref_text))
        return _answer_tool_result(result, content)
    except Exception as exc:
        logger.error("询问 IMA 时发生错误", error_category=type(exc).__name__)
        return _failed_answer("询问 IMA 失败", IMAAPIClient._error_category(exc))


# @mcp.on_shutdown()
# async def on_shutdown():
#     """服务器关闭时的清理工作"""
#     global ima_client
#     if ima_client:
#         logger.info("👋 正在关闭 IMA 客户端会话...")
#         await ima_client.close()
#         logger.info("✅ 客户端会话已关闭")


async def ensure_client_ready():
    """确保客户端已初始化；token 校验由问答重试链路负责。"""
    global ima_client

    if not ima_client:
        async with _client_init_lock:
            if not ima_client:
                logger.info("🚀 首次请求，初始化 IMA 客户端...")

                config = get_config()
                if not config:
                    logger.error("❌ 配置未加载")
                    return False

                try:
                    config.enable_raw_logging = app_config.enable_raw_logging
                    config.raw_log_dir = str(log_dir / "raw")
                    config.raw_log_on_success = False

                    ima_client = IMAAPIClient(config)
                    logger.debug("✅ IMA 客户端初始化成功")
                except Exception as e:
                    logger.exception("❌ IMA 客户端初始化失败")
                    return False
    
    # Token 校验留在问答链路内，网络错误由其 Tenacity 重试及结果契约处理。
    return True


@mcp.tool()
async def ask(question: str) -> ToolResult:
    """向腾讯 IMA 知识库询问任何问题

    Args:
        question: 要询问的问题

    Returns:
        IMA 知识库的回答
    """
    global ima_client
    
    # 生成请求ID用于日志追踪
    import uuid
    request_id = str(uuid.uuid4())[:8]
    
    # 绑定上下文
    with logger.contextualize(request_id=request_id):
        # 确保客户端已初始化
        if not await ensure_client_ready():
            return _failed_answer("IMA 客户端初始化失败，请检查配置", "configuration")

        if _is_multi_knowledge_base_mode():
            kb_ids = _get_knowledge_base_ids()
            return _failed_answer(
                "当前为多知识库模式，请使用 ask_with_kb 并传入 knowledge_base_id。"
                f"可用值: {', '.join(kb_ids)}",
                "validation",
            )

        logger.debug("🔍 ask 工具调用", question_length=len(question))

        default_kb_id = _get_knowledge_base_ids()[0]
        return await _ask_with_target_kb(question=question, knowledge_base_id=default_kb_id)


@mcp.tool()
async def ask_with_kb(question: str, knowledge_base_id: str) -> ToolResult:
    """向指定知识库询问问题（多知识库模式使用）

    Args:
        question: 要询问的问题
        knowledge_base_id: 目标知识库 ID（必须在配置的知识库列表中）

    Returns:
        IMA 知识库的回答
    """
    import uuid

    request_id = str(uuid.uuid4())[:8]
    with logger.contextualize(request_id=request_id):
        if not await ensure_client_ready():
            return _failed_answer("IMA 客户端初始化失败，请检查配置", "configuration")

        logger.debug(
            "🔍 ask_with_kb 工具调用",
            question_length=len(question),
            knowledge_base_id=knowledge_base_id,
        )
        return await _ask_with_target_kb(question=question, knowledge_base_id=knowledge_base_id)


@mcp.resource("ima://config")
def get_config_resource() -> str:
    """获取当前配置信息（不包含敏感数据）"""
    try:
        config = get_config()
        if not config:
            return "配置未加载"

        # 返回非敏感的配置信息
        config_info = "IMA 配置信息:\n"
        config_info += f"客户端ID: {config.client_id}\n"
        config_info += f"默认知识库ID: {config.knowledge_base_id}\n"
        config_info += f"可用知识库ID: {', '.join(config.knowledge_base_ids)}\n"
        config_info += f"知识库模式: {'多知识库' if len(config.knowledge_base_ids) > 1 else '单知识库'}\n"
        config_info += f"请求超时: {config.timeout}秒\n"
        config_info += f"重试次数: {config.retry_count}\n"
        config_info += f"代理设置: {config.proxy or '未设置'}\n"
        config_info += f"创建时间: {config.created_at}\n"
        if config.updated_at:
            config_info += f"更新时间: {config.updated_at}\n"

        return config_info

    except Exception as e:
        logger.error(f"获取配置资源时发生错误: {e}")
        return f"[ERROR] 获取配置失败: {str(e)}"


@mcp.resource("ima://help")
def get_help_resource() -> str:
    """获取使用帮助信息"""
    help_text = """
# IMA Copilot MCP 服务器帮助

## 概述
这是基于环境变量配置的 IMA Copilot MCP 服务器，提供腾讯 IMA 知识库的 MCP 协议接口。

## 配置方式
通过环境变量或 .env 文件配置 IMA 认证信息：

1. 复制 .env.example 为 .env
2. 填入从浏览器获取的认证信息：
   - IMA_COOKIES: 完整的 cookies 字符串
   - IMA_X_IMA_COOKIE: X-Ima-Cookie 请求头
   - IMA_X_IMA_BKN: X-Ima-Bkn 请求头

## 工具
- `ask`: 向 IMA 知识库询问问题
- `ask_with_kb`: 向指定知识库询问问题（多知识库模式推荐）

## 资源
- `ima://config`: 查看配置信息
- `ima://help`: 查看帮助信息

## 启动方式
```bash
# 使用 fastmcp 命令启动
fastmcp run ima_server_simple.py:mcp --transport http --host 127.0.0.1 --port 8081

# 或使用 Python 直接运行
python ima_server_simple.py
```

## 连接方式
使用 MCP Inspector 连接到: http://127.0.0.1:8081/mcp
"""
    return help_text


def main():
    """主函数 - 直接启动服务器时使用"""
    app_config = get_app_config()

    print("IMA Copilot MCP 服务器")
    print("=" * 50)
    print("版本: 简化版 (基于环境变量)")
    print(f"服务地址: http://{app_config.host}:{app_config.port}")
    print(f"MCP 端点: http://{app_config.host}:{app_config.port}/mcp")
    print(f"日志级别: {app_config.log_level}")
    print("=" * 50)

    # 验证配置
    is_valid, error_message = _validate_startup_config()
    if not is_valid:
        print(f"[ERROR] 启动失败: {error_message}")
        sys.exit(1)

    config = get_config()
    if not config:
        print("[ERROR] 配置加载失败，请检查环境变量")
        sys.exit(1)

    print("[OK] 配置加载成功，必需认证信息已设置")
    print(f"[INFO] 默认知识库: {config.knowledge_base_id}")
    print(f"[INFO] 可用知识库: {', '.join(config.knowledge_base_ids)}")

    print("=" * 50)
    mcp.run(transport="http", host=app_config.host, port=app_config.port)


if __name__ == "__main__":
    main()


__all__ = ["mcp"]
