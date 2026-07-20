# IMA Copilot MCP 服务器

基于 FastMCP v2 框架的腾讯 IMA Copilot MCP (Model Context Protocol) 服务器，**使用环境变量配置**，简化项目结构，专注于 MCP 协议实现。

## ✨ 主要特性

- 🚀 **极简配置**: 仅需两个必需参数即可启动，开箱即用
- 🤖 **MCP 协议支持**: 完整实现 Model Context Protocol 规范 (基于 FastMCP 2.14.1)
- 🔧 **环境变量配置**: 通过 `.env` 文件管理所有配置
- 📡 **HTTP 传输**: 支持 HTTP 传输协议，便于 MCP Inspector 连接
- 🛠️ **增强型 MCP 工具**: 提供腾讯 IMA 知识库问答功能，返回结果包含回答文本和结构化参考资料
- 🔄 **Token 自动刷新**: 智能管理认证 token，自动刷新保持会话有效
- 💪 **Tenacity-powered Retries**: 集成 tenacity 库，优化重试机制，支持指数退避和针对性错误重试
- 🧯 **Code=3 自愈**: 对高并发瞬时 `Code=3` 错误执行退避重试并自动恢复
- 🚦 **并发限流**: 默认串行问答（并发=1），降低请求突发导致的系统错误
- 📝 **Loguru-enhanced Logging**: 采用 Loguru 提升日志体验，提供更清晰、结构化的日志输出
- ⏱️ **超时保护**: 内置请求超时机制，防止长时间阻塞 (已提升至 300 秒)
- 🎯 **一键启动**: 简化的启动流程，自动环境检查和配置验证
- 🐳 **Docker 支持**: 提供官方 Docker 镜像，开箱即用

## 🚀 最新进展 (2025-12-21)

- ✅ **服务器成功运行**: 已验证基于 FastMCP 2.14.1 的 HTTP 传输模式正常工作。
- ✅ **修复启动报错**: 解决了 `AttributeError: 'FastMCP' object has no attribute 'on_shutdown'` 问题（通过在当前版本中禁用该钩子）。
- ✅ **全流程验证**: 验证了从 Token 刷新、会话初始化到 SSE 流式响应解析的完整链路，支持长回复（35秒+响应已验证）。

## 快速开始

### 方式一：使用 Docker（推荐）

#### 1. 使用 Docker Run

```bash
# 拉取镜像
docker pull highkay/tencent-ima-copilot-mcp:latest

# 运行容器（需要替换以下两个必需的环境变量）
docker run -d \
  --name ima-copilot-mcp \
  -p 8081:8081 \
  -e IMA_X_IMA_COOKIE="your_x_ima_cookie_here" \
  -e IMA_X_IMA_BKN="your_x_ima_bkn_here" \
  -v $(pwd)/logs:/app/logs \
  --restart unless-stopped \
  highkay/tencent-ima-copilot-mcp:latest

# 查看日志
docker logs -f ima-copilot-mcp
```

#### 2. 使用 Docker Compose（更便捷）

创建 `.env` 文件（或直接在 shell 中设置环境变量）：

```bash
# .env 文件
IMA_X_IMA_COOKIE="your_x_ima_cookie_here"
IMA_X_IMA_BKN="your_x_ima_bkn_here"
```

启动服务：

```bash
# 启动服务
docker-compose up -d

# 查看日志
docker-compose logs -f
```

### 方式二：本地安装

#### 1. 安装依赖

```bash
# 推荐使用项目元数据安装运行与开发依赖
python -m pip install -e '.[dev]'
```

#### 2. 配置环境变量

```bash
# 私密配置默认放在代码仓库外
mkdir -p ~/.claude/ima
cp .env.example ~/.claude/ima/.env

# 编辑 .env 文件，填入从浏览器获取的 IMA 认证信息
nano ~/.claude/ima/.env  # 或使用其他编辑器
chmod 600 ~/.claude/ima/.env
```

#### 必需配置项

以下环境变量必须正确配置才能使用服务：

- **`IMA_X_IMA_COOKIE`**: X-Ima-Cookie 请求头值（包含平台信息、token 等）
- **`IMA_X_IMA_BKN`**: X-Ima-Bkn 请求头值（业务密钥）

#### 3. 获取 IMA 认证信息

#### 步骤 1: 访问 IMA Copilot

1. 访问 [https://ima.qq.com](https://ima.qq.com) 并登录
2. 按 F12 打开开发者工具
3. 切换到 **Network** (网络) 标签页

#### 步骤 2: 获取认证头信息

1. 在 IMA 中发送一条消息
2. 找到向 `/cgi-bin/assistant/qa` 的 POST 请求
3. 查看 **Request Headers**，复制以下字段：
   - `x-ima-cookie` → `IMA_X_IMA_COOKIE`
   - `x-ima-bkn` → `IMA_X_IMA_BKN`

#### 4. 启动服务器

##### 方式一：使用启动脚本（推荐）

```bash
# macOS / Linux
./start-mcp.sh

# 安装后也可以直接运行
IMA_ENV_FILE="$HOME/.claude/ima/.env" ima-mcp
```

##### 方式二：使用 fastmcp 命令

```bash
fastmcp run ima_server_simple.py:mcp --transport http --host 127.0.0.1 --port 8081
```

#### 5. 使用 MCP Inspector

```bash
# 安装 MCP Inspector
npx @modelcontextprotocol/inspector

# 连接到服务器
# 在 Inspector 中输入: http://127.0.0.1:8081/mcp
```

### 服务端点

- **MCP 协议端点**: `http://127.0.0.1:8081/mcp`（用于 MCP Inspector 或其他 MCP 客户端）
- **日志文件**: `logs/debug/ima_server_YYYYMMDD_HHMMSS.log`（Loguru 自动生成和管理）
- **原始 SSE 日志**: `logs/debug/raw/sse_*.log`（发生错误时自动保存）

## 可用的 MCP 工具

### 1. `ask`

向腾讯 IMA 知识库询问任何问题

**参数:**
- `question` (必需): 要询问的问题

**示例:**
```
问题: "什么是机器学习？"
问题: "如何制作番茄炒蛋？"
```

**特性:**
- 自动管理会话，无需手动创建
- 智能 token 刷新，确保认证有效
- 内置并发限流（默认 `IMA_ASK_CONCURRENCY_LIMIT=1`）
- 检测到 `Code=3` 且无文本时自动指数退避重试（最多 2 次）
- **300 秒超时保护**，防止长时间等待
- 返回内容为 **`TextContent` 列表**，包含**回答文本**和格式化后的**参考资料**

> 注意：当配置了多个知识库 ID 时，`ask` 会直接报错并提示改用 `ask_with_kb`。

### 2. `ask_with_kb`

向指定知识库询问问题（多知识库模式）

**参数:**
- `question` (必需): 要询问的问题
- `knowledge_base_id` (必需): 目标知识库 ID（必须在配置列表中）

**示例:**
```
问题: "总结这个知识库的核心内容"
knowledge_base_id: "7305806844290061"
```

## 可用的 MCP 资源

### 1. `ima://config`

获取当前配置信息（不包含敏感数据）

### 2. `ima://help`

获取帮助信息

## 配置选项

### 必需的环境变量

| 变量名 | 说明 | 获取方式 |
|--------|------|---------|
| `IMA_X_IMA_COOKIE` | X-Ima-Cookie 请求头 | 从浏览器开发者工具 Network 标签中复制 |
| `IMA_X_IMA_BKN` | X-Ima-Bkn 请求头 | 从浏览器开发者工具 Network 标签中复制 |

### 可选的环境变量

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `IMA_KNOWLEDGE_BASE_ID` / `knowledgeBaseId` | 单知识库 ID（两者等价） | 无（必须显式配置） |
| `IMA_KNOWLEDGE_BASE_IDS` / `knowledgeBaseIds` | 多知识库 ID 列表（逗号分隔） | 无 |
| `IMA_MCP_HOST` | MCP 服务器地址 | `127.0.0.1` |
| `IMA_MCP_PORT` | MCP 服务器端口 | `8081` |
| `IMA_MCP_LOG_LEVEL` | 日志级别 (支持 `DEBUG`, `INFO`, `WARNING`, `ERROR`) | `INFO` |
| `IMA_REQUEST_TIMEOUT` | IMA API 请求超时时间（秒） | `30` |
| `IMA_RETRY_COUNT` | 网络/超时类异常重试次数 | `3` |
| `IMA_ASK_CONCURRENCY_LIMIT` | 问答并发上限（建议 1-2） | `1` |
| `IMA_ROBOT_TYPE` | 机器人类型 | `5` |
| `IMA_SCENE_TYPE` | 场景类型 | `1` |
| `IMA_MODEL_TYPE` | 模型类型 | `4` |

### 知识库配置模式

- 单知识库模式（兼容旧逻辑）：配置 `IMA_KNOWLEDGE_BASE_ID`（或 `knowledgeBaseId`），使用 `ask` 或 `ask_with_kb` 均可。
- 多知识库模式：配置 `IMA_KNOWLEDGE_BASE_IDS`（或 `knowledgeBaseIds`，逗号分隔），必须使用 `ask_with_kb`。
- 同时配置两者时：优先使用 `IMA_KNOWLEDGE_BASE_ID`（单知识库模式）。
- 启动强校验：若 `IMA_KNOWLEDGE_BASE_ID` 与 `IMA_KNOWLEDGE_BASE_IDS` 都未配置，服务将直接退出。

### 从旧版本迁移

- 只用一个知识库：保持原有 `IMA_KNOWLEDGE_BASE_ID=<id>` 即可，无需改调用方式。
- 需要多个知识库：新增 `IMA_KNOWLEDGE_BASE_IDS=id1,id2,...`，并把调用从 `ask(question)` 改为 `ask_with_kb(question, knowledge_base_id)`。

## 故障排除

### 常见问题

**Q: 认证失败（Token 验证失败）怎么办？**

A:
1. 检查 `${IMA_ENV_FILE:-~/.claude/ima/.env}` 中的 `IMA_X_IMA_COOKIE` 和 `IMA_X_IMA_BKN` 是否正确
2. 确认 `IMA_X_IMA_COOKIE` 中包含 `IMA-REFRESH-TOKEN` 字段
3. 重新从浏览器获取最新的认证信息

**Q: 如何连接特定的知识库？**

A:
在私密 env 文件中设置 `IMA_KNOWLEDGE_BASE_ID`（或 `knowledgeBaseId`）即可。获取方法：
1. 在 IMA 网页选择知识库
2. 找到 `init_session` 请求
3. 查看 Payload 中的 `knowledge_base_id`

**Q: 多知识库怎么配置和调用？**

A:
1. 在私密 env 文件中设置 `IMA_KNOWLEDGE_BASE_IDS=id1,id2,id3`
2. 调用工具时使用 `ask_with_kb(question, knowledge_base_id)`
3. 若调用 `ask`，会提示错误并给出可用 `knowledge_base_id` 列表

**Q: 偶发出现 `Code=3` 且无文本怎么办？**

A:
1. 先保持默认并发（`IMA_ASK_CONCURRENCY_LIMIT=1`）
2. 避免同一知识库短时间突发并发请求
3. 服务已内置 `Code=3` 退避重试；若仍频繁出现，可适当增加请求间隔

## 许可证

MIT License
