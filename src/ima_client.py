"""
IMA API 客户端实现
"""
import asyncio
import base64
import codecs
import json
import random
import re
import secrets
import string
import time
import traceback
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional
from urllib.parse import unquote

import aiohttp
from loguru import logger
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception,
    retry_if_exception_type,
    before_sleep_log,
)

from .models import (
    IMAConfig,
    IMAMessage,
    MessageType,
    KnowledgeBaseMessage,
    TextMessage,
    MediaInfo,
    DeviceInfo,
    IMAStatus,
    TokenRefreshRequest,
    TokenRefreshResponse,
    InitSessionRequest,
    InitSessionResponse,
    EnvInfo,
    KnowledgeBaseInfoWithFolder,
    AskQuestionRequest, # New
    CommandInfo, # New
    KnowledgeQaInfo, # New
    ModelInfo, # New
    HistoryInfo, # New
)




class AuthenticationError(ValueError):
    """不可重试的认证异常"""


class IMAAPIClient:
    """IMA API 客户端"""

    def __init__(self, config: IMAConfig):
        self.config = config
        self.base_url = "https://ima.qq.com"
        self.api_endpoint = "/cgi-bin/assistant/qa"
        self.refresh_endpoint = "/cgi-bin/auth_login/refresh"
        self.init_session_endpoint = "/cgi-bin/session_logic/init_session"
        self.session: Optional[aiohttp.ClientSession] = None
        self.raw_log_dir: Optional[Path] = None
        self._token_lock = asyncio.Lock()  # 保护 token 刷新过程
        self._last_auth_error: Optional[str] = None
        self._ask_semaphore = asyncio.Semaphore(max(1, getattr(self.config, "ask_concurrency_limit", 1)))

        if getattr(self.config, "enable_raw_logging", False):
            raw_dir_value = getattr(self.config, "raw_log_dir", None)
            raw_dir = Path(raw_dir_value) if raw_dir_value else Path("logs") / "sse_raw"
            try:
                raw_dir.mkdir(parents=True, exist_ok=True)
                self.raw_log_dir = raw_dir
                logger.info(f"Raw SSE logs will be written to: {raw_dir}")
            except Exception as exc:
                logger.error(f"Failed to prepare raw SSE log directory: {exc}")

    def _should_persist_raw(self, stream_error: Optional[str]) -> bool:
        """判断当前是否需要保存原始SSE响应"""
        if not self.raw_log_dir or not getattr(self.config, "enable_raw_logging", False):
            return False

        if stream_error:
            return True  # always persist on errors

        return getattr(self.config, "raw_log_on_success", False)

    def _persist_raw_response(
        self,
        trace_id: str,
        attempt_index: int,
        question: Optional[str],
        full_response: str,
        message_count: int,
        parsed_message_count: int,
        failed_parse_count: int,
        elapsed_time: float,
        stream_error: Optional[str],
    ) -> Optional[Path]:
        """将原始SSE响应落盘，便于排查问题"""
        if not self._should_persist_raw(stream_error):
            return None

        assert self.raw_log_dir is not None  # for type checkers

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        suffix = f"attempt{attempt_index + 1}"
        filename = f"sse_{timestamp}_{trace_id}_{suffix}.log"
        target_path = self.raw_log_dir / filename

        max_bytes = getattr(self.config, "raw_log_max_bytes", 0) or 0
        encoded = full_response.encode("utf-8", errors="replace")
        response_bytes = len(encoded)
        truncated = False

        if max_bytes > 0 and response_bytes > max_bytes:
            encoded = encoded[:max_bytes]
            truncated = True

        preview_question = None
        if question:
            preview_question = question.strip()
            if len(preview_question) > 200:
                preview_question = preview_question[:200] + "..."

        metadata = {
            "timestamp": datetime.now().isoformat(),
            "trace_id": trace_id,
            "attempt": attempt_index + 1,
            "question": preview_question,
            "message_count": message_count,
            "parsed_message_count": parsed_message_count,
            "failed_parse_count": failed_parse_count,
            "elapsed_seconds": round(elapsed_time, 3),
            "response_bytes": response_bytes,
            "truncated": truncated,
            "stream_error": stream_error,
        }

        try:
            header = json.dumps(metadata, ensure_ascii=False, indent=2)
            body = encoded.decode("utf-8", errors="replace")

            with target_path.open("w", encoding="utf-8") as fp:
                fp.write(header)
                fp.write("\n\n")
                fp.write(body)

            logger.info(f"Raw SSE response saved to {target_path} (trace_id={trace_id})")
            return target_path
        except Exception as exc:
            logger.error(f"Failed to persist raw SSE response: {exc}")
            return None

    def _is_token_expired(self) -> bool:
        """检查token是否过期"""
        if not self.config.token_updated_at or not self.config.token_valid_time:
            return True
        
        expired_time = self.config.token_updated_at + timedelta(seconds=self.config.token_valid_time)
        # 提前 5 分钟刷新以防万一
        return datetime.now() > (expired_time - timedelta(minutes=5))

    def _parse_user_id_from_cookies(self) -> Optional[str]:
        """从IMA_X_IMA_COOKIE中解析IMA-UID"""
        try:
            uid_pattern = r"IMA-UID=([^;]+)"
            match = re.search(uid_pattern, self.config.x_ima_cookie)
            if match:
                return match.group(1)

            user_id_pattern = r"user_id=([a-f0-9]{16})"
            if self.config.cookies:
                match = re.search(user_id_pattern, self.config.cookies)
                if match:
                    return match.group(1)
        except Exception as e:
            logger.warning(f"解析user_id失败: {e}")
        return None

    def _parse_refresh_token_from_cookies(self) -> Optional[str]:
        """从IMA_X_IMA_COOKIE中解析IMA-REFRESH-TOKEN"""
        try:
            refresh_token_pattern = r"IMA-REFRESH-TOKEN=([^;]+)"
            match = re.search(refresh_token_pattern, self.config.x_ima_cookie)
            if match:
                token = unquote(match.group(1))
                logger.info(f"成功从 x_ima_cookie 解析 IMA-REFRESH-TOKEN (长度: {len(token)})")
                return token
            
            logger.warning("在 x_ima_cookie 中未找到 IMA-REFRESH-TOKEN")
            
            token_pattern = r"IMA-TOKEN=([^;]+)"
            match = re.search(token_pattern, self.config.x_ima_cookie)
            if match:
                token = unquote(match.group(1))
                logger.warning(f"使用 IMA-TOKEN 作为 refresh_token（长度: {len(token)}）")
                return token

            if self.config.cookies:
                refresh_token_pattern = r"refresh_token=([^;]+)"
                match = re.search(refresh_token_pattern, self.config.cookies)
                if match:
                    token = unquote(match.group(1))
                    logger.info(f"成功从 cookies 解析 refresh_token")
                    return token
            
            logger.warning("未能从任何来源解析到 refresh_token")
        except Exception as e:
            logger.error(f"解析 refresh_token 失败: {e}\n{traceback.format_exc()}")
        return None

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError)),
        before_sleep=before_sleep_log(logger, "WARNING"),
        reraise=True
    )
    async def refresh_token(self) -> bool:
        """刷新访问令牌"""
        async with self._token_lock:
            # 双重检查
            if not self._is_token_expired() and self.config.current_token:
                return True

            logger.info("🔄 开始刷新 Token")
            self._last_auth_error = None
            
            if not self.config.user_id or not self.config.refresh_token:
                logger.info("从 cookies 中解析 user_id 和 refresh_token")
                self.config.user_id = self._parse_user_id_from_cookies()
                self.config.refresh_token = self._parse_refresh_token_from_cookies()

                if not self.config.user_id or not self.config.refresh_token:
                    self._last_auth_error = "缺少 user_id 或 refresh_token，请重新抓取 IMA 登录态"
                    logger.warning("缺少token刷新所需的user_id或refresh_token")
                    return False

            try:
                session = await self._get_session()

                # 构建刷新请求
                refresh_request = TokenRefreshRequest(
                    user_id=self.config.user_id,
                    refresh_token=self.config.refresh_token
                )

                refresh_url = f"{self.base_url}{self.refresh_endpoint}"
                refresh_headers = self._build_headers(
                    for_refresh=True,
                    include_authorization=False,
                )
                
                request_body = refresh_request.model_dump()

                async with session.post(
                    refresh_url,
                    json=request_body,
                    headers=refresh_headers
                ) as response:
                    response_text = await response.text()
                    if response.status == 200:
                        try:
                            response_data = await response.json()
                            refresh_response = TokenRefreshResponse(**response_data)

                            if refresh_response.code == 0 and refresh_response.token:
                                # 更新token信息
                                self.config.current_token = refresh_response.token
                                self.config.token_valid_time = int(refresh_response.token_valid_time or "7200")
                                self.config.token_updated_at = datetime.now()
                                self._last_auth_error = None

                                logger.info(f"✅ Token刷新成功 (有效期: {self.config.token_valid_time}秒)")
                                return True
                            else:
                                self._last_auth_error = refresh_response.msg
                                logger.warning("=" * 60)
                                logger.warning(f"Token刷新失败: {refresh_response.msg} (Code: {refresh_response.code})")
                                logger.warning("=" * 60)
                                return False
                        except json.JSONDecodeError as je:
                            self._last_auth_error = f"刷新响应解析失败: {je}"
                            logger.error(f"无法解析响应为 JSON: {je}")
                            logger.error(f"原始响应: {response_text[:200]}")
                            return False
                    else:
                        self._last_auth_error = f"Token刷新请求失败: HTTP {response.status}"
                        logger.error(f"Token刷新请求失败: HTTP {response.status}")
                        return False

            except Exception as e:
                self._last_auth_error = f"Token刷新异常: {type(e).__name__}: {e}"
                logger.error(f"Token刷新异常: {type(e).__name__}: {e}")
                return False

    async def ensure_valid_token(self) -> bool:
        """确保token有效，如果过期则刷新"""
        if self._is_token_expired():
            return await self.refresh_token()
        return True

    def _build_auth_error_message(self) -> str:
        """生成用户可读的认证失败提示"""
        detail = self._last_auth_error or "登录态无效或已过期，请重新抓取 IMA_X_IMA_COOKIE 和 IMA_X_IMA_BKN"
        return f"Authentication failed - {detail}"

    
    def _parse_cookies(self, cookie_string: str) -> Dict[str, str]:
        """解析 Cookie 字符串为字典"""
        cookies = {}
        if not cookie_string:
            return cookies

        # 处理不同格式的 Cookie 字符串
        cookie_parts = cookie_string.split(';')
        for part in cookie_parts:
            if '=' in part:
                name, value = part.strip().split('=', 1)
                cookies[name.strip()] = value.strip()
        return cookies

    def _build_x_ima_cookie(self, include_current_token: bool = True) -> str:
        """构建 x-ima-cookie，必要时注入最新 token"""
        x_ima_cookie = self.config.x_ima_cookie
        if include_current_token and self.config.current_token:
            if 'IMA-TOKEN=' in x_ima_cookie:
                x_ima_cookie = re.sub(
                    r'IMA-TOKEN=[^;]+',
                    f'IMA-TOKEN={self.config.current_token}',
                    x_ima_cookie
                )
            else:
                x_ima_cookie = x_ima_cookie.rstrip('; ') + f'; IMA-TOKEN={self.config.current_token}'
        return x_ima_cookie

    def _extract_user_agent(self) -> str:
        """从 x-ima-cookie 中提取 IMA-IUA，缺省回退到固定 UA"""
        default_user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 Edg/144.0.0.0"
        )
        try:
            match = re.search(r"IMA-IUA=([^;]+)", self.config.x_ima_cookie)
            if match:
                return unquote(match.group(1))
        except Exception:
            pass
        return default_user_agent

    def _generate_traceparent(self) -> str:
        """生成 W3C traceparent"""
        trace_id = secrets.token_hex(16)
        span_id = secrets.token_hex(8)
        return f"00-{trace_id}-{span_id}-01"

    def _build_headers(
        self,
        *,
        for_init_session: bool = False,
        for_refresh: bool = False,
        include_authorization: bool = True,
    ) -> Dict[str, str]:
        """构建统一请求头（refresh/init_session/qa）"""
        accept = "application/json" if (for_init_session or for_refresh) else "*/*"
        content_type = "application/json" if (for_init_session or for_refresh) else "text/event-stream"

        headers: Dict[str, str] = {
            "accept": accept,
            "accept-language": "zh-CN,zh;q=0.9",
            "content-type": content_type,
            "extension_version": "999.999.999",
            "from_browser_ima": "1",
            "priority": "u=1, i",
            "sec-ch-ua": '"Not(A:Brand";v="8", "Chromium";v="144", "Microsoft Edge";v="144"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "traceparent": self._generate_traceparent(),
            "x-ima-bkn": self.config.x_ima_bkn,
            "x-ima-cookie": self._build_x_ima_cookie(include_current_token=not for_refresh),
            "referer": "https://ima.qq.com/wikis",
            "user-agent": self._extract_user_agent(),
        }

        if not for_init_session and not for_refresh:
            headers["cache-control"] = "no-cache"

        if include_authorization and not for_refresh and self.config.current_token:
            headers["authorization"] = f"Bearer {self.config.current_token}"

        return headers

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=5),
        retry=retry_if_exception_type((aiohttp.ClientError, OSError)),
        before_sleep=before_sleep_log(logger, "WARNING"),
        reraise=True
    )
    async def _get_session(self) -> aiohttp.ClientSession:
        """获取或创建 HTTP 会话"""
        if self.session is None or self.session.closed:
            connector = aiohttp.TCPConnector(
                limit=100,
                limit_per_host=30,
                ttl_dns_cache=300,
                use_dns_cache=True,
                keepalive_timeout=60,
            )

            sse_timeout = 600  # 增加总超时到 10 分钟以支持极长回复
            
            timeout = aiohttp.ClientTimeout(
                total=sse_timeout,
                sock_read=180,
                connect=30,
                sock_connect=30,
            )
            
            self.session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                cookies=self._parse_cookies(self.config.cookies or ""),
                # 注意：不在 session 层面固定 headers，因为 token 可能会变
                trust_env=True,
                read_bufsize=5 * 2**20,
                auto_decompress=True,
            )

        return self.session

    async def close(self):
        """关闭客户端会话"""
        if self.session and not self.session.closed:
            await self.session.close()

    def _generate_session_id(self) -> str:
        """生成会话 ID"""
        return ''.join(random.choices(string.ascii_lowercase + string.digits, k=24))

    def _generate_temp_uskey(self) -> str:
        """生成临时 uskey"""
        return base64.b64encode(secrets.token_bytes(32)).decode('utf-8')

    def _build_request(self, question: str, session_id: str) -> AskQuestionRequest:
        """构建 IMA API 请求"""
        uskey = self._generate_temp_uskey()

        try:
            ima_guid = self.config.x_ima_cookie.split('IMA-GUID=')[1].split(';')[0]
        except (IndexError, AttributeError):
            ima_guid = "default_guid"

        device_info = DeviceInfo(
            uskey=uskey,
            uskey_bus_infos_input=f"{ima_guid}_{int(datetime.now().timestamp())}"
        )

        return AskQuestionRequest(
            session_id=session_id,
            robot_type=self.config.robot_type,
            question=question,
            question_type=2,
            client_id=self.config.client_id,
            command_info=CommandInfo(
                type=14,
                knowledge_qa_info=KnowledgeQaInfo(
                    tags=[],
                    knowledge_ids=[]
                )
            ),
            model_info=ModelInfo(
                model_type=self.config.model_type,
                enable_enhancement=False
            ),
            history_info=HistoryInfo(),
            device_info=device_info
        )

    def _parse_sse_message(self, line: str) -> Optional[IMAMessage]:
        """解析 SSE 消息"""
        try:
            if line.startswith('data: '):
                data = line[6:]
            elif line.startswith('data:'):
                data = line[5:]
            elif line.startswith(('event: ', 'id: ')):
                return None
            else:
                data = line

            if not data or data == '[DONE]' or not data.strip():
                return None

            json_data = json.loads(data)

            if 'msgs' in json_data and isinstance(json_data['msgs'], list):
                for msg in json_data['msgs']:
                    if isinstance(msg, dict) and 'content' in msg:
                        content = msg.get('content', '')
                        if content:
                            return TextMessage(
                                type=MessageType.TEXT,
                                content=content,
                                text=content,
                                raw=data
                            )
                return None

            if 'content' in json_data:
                content = json_data['content']
                if isinstance(content, str) and content:
                    return TextMessage(
                        type=MessageType.TEXT,
                        content=content,
                        text=content,
                        raw=data
                    )

            if 'Text' in json_data and isinstance(json_data['Text'], str):
                return TextMessage(
                    type=MessageType.TEXT,
                    content=json_data['Text'],
                    text=json_data['Text'],
                    raw=data
                )

            if 'type' in json_data and json_data['type'] == 'knowledgeBase':
                if 'content' not in json_data:
                    json_data['content'] = json_data.get('processing', '知识库搜索中...')
                return KnowledgeBaseMessage(**json_data)

            if 'question' in json_data and 'answer' in json_data:
                answer = json_data.get('answer', '')
                if answer:
                    return TextMessage(
                        type=MessageType.TEXT,
                        content=answer,
                        text=answer,
                        raw=data
                    )

            return IMAMessage(
                type=MessageType.SYSTEM,
                content=str(json_data),
                raw=data
            )

        except (json.JSONDecodeError, KeyError, ValueError):
            raise

    async def _process_sse_stream(
        self,
        response: aiohttp.ClientResponse,
        *,
        trace_id: str,
        attempt_index: int,
        question: Optional[str],
    ) -> AsyncGenerator[IMAMessage, None]:
        """处理 SSE 流"""
        buffer = ""
        full_response = ""
        message_count = 0
        parsed_message_count = 0
        failed_parse_count = 0
        initial_timeout = 180
        chunk_timeout = 120
        last_data_time = asyncio.get_event_loop().time()
        start_time = asyncio.get_event_loop().time()
        has_received_data = False
        sample_chunks = []
        stream_error: Optional[str] = None
        
        # 使用增量解码器处理可能被截断的多字节字符
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

        try:
            logger.debug(f"🔄 [SSE流] 开始读取 (trace_id={trace_id})")
            logger.debug(f"  手动超时配置: initial={initial_timeout}s, chunk={chunk_timeout}s")
            
            async for chunk in response.content:
                current_time = asyncio.get_event_loop().time()

                timeout_threshold = chunk_timeout if has_received_data else initial_timeout
                elapsed_since_last_data = current_time - last_data_time
                
                # 手动超时检查（通常不会触发，因为aiohttp的timeout会先触发）
                if elapsed_since_last_data > timeout_threshold:
                    stream_error = f"Manual timeout after {elapsed_since_last_data:.1f}s with {message_count} chunks"
                    logger.warning(f"⏰ [SSE流] 手动超时触发: {stream_error}")
                    break

                if chunk:
                    has_received_data = True
                    last_data_time = current_time
                    message_count += 1

                    # 使用增量解码器解码
                    chunk_str = decoder.decode(chunk, final=False)

                    buffer += chunk_str
                    full_response += chunk_str

                    while '\n' in buffer:
                        line, buffer = buffer.split('\n', 1)
                        line = line.strip()
                        if line:
                            try:
                                message = self._parse_sse_message(line)
                                if message:
                                    parsed_message_count += 1
                                    yield message
                            except (json.JSONDecodeError, KeyError, ValueError):
                                failed_parse_count += 1


        except asyncio.TimeoutError:
            if has_received_data and parsed_message_count > 0:
                stream_error = None
            else:
                stream_error = "SSE timeout"
                logger.error(f"❌ [SSE流] 超时错误（未收到数据）, trace_id={trace_id}")
        except aiohttp.ClientPayloadError as exc:
            stream_error = f"SSE payload error: {exc}"
            logger.error(f"❌ [SSE流] ClientPayloadError: {exc}, trace_id={trace_id}")
        except Exception as exc:
            stream_error = f"SSE exception: {exc}"
            logger.error(f"❌ [SSE流] 未知异常: {type(exc).__name__}: {exc}, trace_id={trace_id}\n{traceback.format_exc()}")
        finally:
            # 确保响应被正确关闭
            if not response.closed:
                response.close()
            
            # 刷新解码器中剩余的字节
            remaining_str = decoder.decode(b"", final=True)
            if remaining_str:
                buffer += remaining_str
                full_response += remaining_str

            elapsed_time = asyncio.get_event_loop().time() - start_time
            self._persist_raw_response(
                trace_id=trace_id,
                attempt_index=attempt_index,
                question=question,
                full_response=full_response,
                message_count=message_count,
                parsed_message_count=parsed_message_count,
                failed_parse_count=failed_parse_count,
                elapsed_time=elapsed_time,
                stream_error=stream_error,
            )

        # 处理剩余的缓冲区内容
        if buffer.strip():
            remaining_lines = buffer.strip().split('\n')
            for i, line in enumerate(remaining_lines):
                line = line.strip()
                if line:
                    try:
                        message = self._parse_sse_message(line)
                        if message:
                            parsed_message_count += 1
                            yield message
                    except (json.JSONDecodeError, KeyError, ValueError):
                        failed_parse_count += 1

        if message_count < 100 or not has_received_data:
            try:
                if full_response.strip():
                    response_data = json.loads(full_response.strip())
                    messages = self._extract_messages_from_response(response_data)
                    for message in messages:
                        yield message

            except json.JSONDecodeError:
                if full_response:
                    lines = full_response.split('\n')
                    for i, line in enumerate(lines):
                        line = line.strip()
                        if line and line != '[DONE]':
                            message = self._parse_sse_message(line)
                            if message:
                                parsed_message_count += 1
                                yield message
                            else:
                                failed_parse_count += 1

        elapsed_time = asyncio.get_event_loop().time() - start_time
        
        logger.info("=" * 80)
        logger.info(f"✅ [SSE流] 处理完成 (trace_id={trace_id})")
        logger.info(f"  收到数据块: {message_count} 个, 成功解析: {parsed_message_count} 条, 失败: {failed_parse_count} 次")
        logger.info(f"  响应大小: {len(full_response)} 字节, 耗时: {elapsed_time:.1f} 秒")
        
        if stream_error:
            logger.warning(f"  ⚠️ 流错误: {stream_error}")
        
        # 诊断信息：如果耗时接近30秒，很可能是aiohttp的total timeout触发
        if 29.0 <= elapsed_time <= 31.0:
            logger.warning(f"  ⚠️ [诊断] 耗时正好约30秒，怀疑是aiohttp的ClientTimeout.total触发")
            logger.warning(f"  ⚠️ [诊断] 建议检查 IMAConfig.timeout 配置值（当前: {getattr(self.config, 'timeout', 'N/A')}s）")
            logger.warning(f"  ⚠️ [诊断] 对于长时间SSE流，建议将timeout设置为300秒以上")
        
        logger.info("=" * 80)

        if message_count > 100 and parsed_message_count < 5:
            logger.error(f"严重: 收到 {message_count} 个chunk但只解析出 {parsed_message_count} 条消息，"
                        f"解析率 {(parsed_message_count/message_count*100):.1f}%")
            logger.debug(f"前10个chunk样本: {sample_chunks}")

    def _extract_messages_from_response(self, response_data: Dict[str, Any]) -> List[IMAMessage]:
        """从完整响应中提取消息"""
        messages = []

        try:
            if 'msgs' in response_data and isinstance(response_data['msgs'], list):
                msgs_list = response_data['msgs']
                if msgs_list:
                    last_msg = msgs_list[-1]
                    if isinstance(last_msg, dict):
                        if last_msg.get('type') == 3:
                            qa_content = last_msg.get('content', {})
                            if isinstance(qa_content, dict):
                                answer = qa_content.get('answer', '')
                                if isinstance(answer, str) and answer:
                                    try:
                                        answer_data = json.loads(answer)
                                        if isinstance(answer_data, dict) and 'Text' in answer_data:
                                            text_content = answer_data['Text']
                                            messages.append(TextMessage(
                                                type=MessageType.TEXT,
                                                content=text_content,
                                                text=text_content,
                                                raw=str(last_msg)
                                            ))
                                        else:
                                            messages.append(TextMessage(
                                                type=MessageType.TEXT,
                                                content=answer,
                                                text=answer,
                                                raw=str(last_msg)
                                            ))
                                    except json.JSONDecodeError:
                                        messages.append(TextMessage(
                                            type=MessageType.TEXT,
                                            content=answer,
                                            text=answer,
                                            raw=str(last_msg)
                                        ))

                                context_refs = qa_content.get('context_refs', '')
                                if context_refs:
                                    try:
                                        context_data = json.loads(context_refs)
                                        if isinstance(context_data, dict):
                                            # 解析 medias 并创建 KnowledgeBaseMessage
                                            medias_list = []
                                            if 'medias' in context_data and isinstance(context_data['medias'], list):
                                                for media_data in context_data['medias']:
                                                    try:
                                                        # 尝试转换为 MediaInfo 对象
                                                        media_info = MediaInfo(**media_data)
                                                        medias_list.append(media_info)
                                                    except Exception as e:
                                                        logger.warning(f"Failed to parse media info: {e}")

                                            if medias_list:
                                                messages.append(KnowledgeBaseMessage(
                                                    type=MessageType.KNOWLEDGE_BASE,
                                                    content="参考资料",
                                                    medias=medias_list,
                                                    raw=context_refs
                                                ))
                                    except json.JSONDecodeError:
                                        logger.warning(f"Failed to decode context_refs: {context_refs[:100]}...")

            logger.info(f"从响应中提取了 {len(messages)} 条消息")
            return messages

        except Exception as e:
            logger.error(f"提取消息时出错: {e}")
            messages.append(IMAMessage(
                type=MessageType.SYSTEM,
                content=str(response_data),
                raw=str(response_data)
            ))
            return messages

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError)),
        before_sleep=before_sleep_log(logger, "WARNING"),
        reraise=True
    )
    async def init_session(self, knowledge_base_id: Optional[str] = None) -> str:
        """初始化会话并返回 session_id"""
        kb_id = knowledge_base_id or getattr(self.config, 'knowledge_base_id', '7305806844290061')

        logger.info(f"🔄 初始化会话 (知识库: {kb_id})")
        if not await self.ensure_valid_token():
            logger.error("❌ 无法获取有效的访问令牌")
            raise ValueError("Authentication failed - unable to obtain valid token")
        
        session = await self._get_session()

        init_request = InitSessionRequest(
            envInfo=EnvInfo(
                robotType=self.config.robot_type,
                interactType=0
            ),
            relatedUrl=kb_id,
            sceneType=self.config.scene_type,
            msgsLimit=10,
            forbidAutoAddToHistoryList=False,
            knowledgeBaseInfoWithFolder=KnowledgeBaseInfoWithFolder(
                knowledge_base_id=kb_id,
                folder_ids=[]
            )
        )
        
        url = f"{self.base_url}{self.init_session_endpoint}"
        request_json = init_request.model_dump(by_alias=True, exclude_none=True)
        headers = self._build_headers(for_init_session=True)

        try:
            async with session.post(
                url,
                json=request_json,
                headers=headers
            ) as response:
                if response.status != 200:
                    response_text = await response.text()
                    logger.error(f"初始化会话失败，HTTP状态码: {response.status}")
                    raise ValueError(f"init_session HTTP错误 {response.status}: {response_text[:500]}")
                
                response_data = await response.json()
                init_response = InitSessionResponse(**response_data)

                if init_response.code == 0 and init_response.session_id:
                    logger.info(f"✅ 会话初始化成功 (session_id: {init_response.session_id[:16]}...)")
                    return init_response.session_id
                else:
                    logger.error(f"❌ 会话初始化失败 (code: {init_response.code}): {init_response.msg}")
                    raise ValueError(f"Session initialization failed (code: {init_response.code}): {init_response.msg}")

        except Exception as e:
            logger.error(f"会话初始化异常: {e}")
            raise

    async def ask_question(self, question: str, session_id: Optional[str] = None) -> AsyncGenerator[IMAMessage, None]:
        """向 IMA 询问问题 (支持流式返回)"""
        if not question.strip():
            raise ValueError("Question cannot be empty")

        # 确保token有效
        if not await self.ensure_valid_token():
            raise ValueError("Authentication failed - unable to obtain valid token")

        # 如果没有提供 session_id，则动态初始化一个新会话（实现无状态/单次对话隔离）
        if not session_id:
            logger.debug("🔄 未提供 session_id，初始化临时会话...")
            session_id = await self.init_session()

        session = await self._get_session()
        request_data = self._build_request(question, session_id)
        url = f"{self.base_url}{self.api_endpoint}"
        headers = self._build_headers(for_init_session=False)

        # 生成trace_id用于跟踪
        trace_id = str(uuid.uuid4())[:8]
        trace_logger = logger.bind(trace_id=trace_id)

        trace_logger.debug("发送问题", question_preview=question[:50])

        response = None
        try:
            response = await session.post(
                url,
                json=request_data.model_dump(),
                headers=headers
            )

            # 检查响应状态
            if response.status != 200:
                response_text = await response.text()
                trace_logger.error("HTTP请求失败", status=response.status, response=response_text[:500])
                raise ValueError(f"HTTP {response.status}: {response_text[:200]}")

            content_type = response.headers.get('content-type', '')
            if 'text/event-stream' not in content_type:
                response_text = await response.text()
                try:
                    error_data = json.loads(response_text)
                    raise ValueError(f"API错误 (code: {error_data.get('code')}): {error_data.get('msg')}")
                except json.JSONDecodeError:
                    raise ValueError(f"意外响应类型: {content_type}, 内容: {response_text[:200]}")

            # 处理流式响应
            message_count = 0
            async for message in self._process_sse_stream(
                response,
                trace_id=trace_id,
                attempt_index=0,
                question=question
            ):
                message_count += 1
                yield message

            if message_count == 0:
                yield IMAMessage(type=MessageType.SYSTEM, content="未收到有效响应", raw="No SSE messages")

        finally:
            if response and not response.closed:
                response.close()

    def _is_login_expired_error(self, error_str: str) -> bool:
        """检测是否是登录过期相关错误"""
        login_expired_patterns = [
            "Session initialization failed",
            "登录过期", "登录失败", "authentication failed", "认证失败",
            "code: 600001", "code: 600002", "code: 600003",
            "code: 41", "110031", "token session expired",
            "token expired", "会话已过期", "请重新登录", "unauthorized", "401"
        ]
        error_lower = error_str.lower()
        return any(pattern.lower() in error_lower for pattern in login_expired_patterns)

    def _should_retry_ask_exception(self, exception: BaseException) -> bool:
        """问答链路的重试判定：认证问题不重试，瞬态错误才重试"""
        if isinstance(exception, AuthenticationError):
            return False

        if isinstance(exception, (aiohttp.ClientError, asyncio.TimeoutError)):
            return True

        if isinstance(exception, ValueError):
            return not self._is_login_expired_error(str(exception))

        return False

    def _collect_system_codes(self, messages: List[IMAMessage]) -> List[int]:
        """收集 system 消息中的 Code/code 返回码"""
        code_pattern = re.compile(r"['\"]?code['\"]?\s*[:=]\s*(\d+)", re.IGNORECASE)
        codes: List[int] = []
        for message in messages:
            if message.type != MessageType.SYSTEM:
                continue
            for raw_code in code_pattern.findall(message.content or ""):
                try:
                    parsed = int(raw_code)
                except ValueError:
                    continue
                if parsed not in codes:
                    codes.append(parsed)
        return codes

    def _is_code3_only_response(self, messages: List[IMAMessage]) -> bool:
        """判断是否属于仅包含 Code=3 的无文本响应"""
        if not messages:
            return False

        has_text = any(
            message.type == MessageType.TEXT and (message.content or "").strip()
            for message in messages
        )
        if has_text:
            return False

        codes = self._collect_system_codes(messages)
        return bool(codes) and codes == [3]

    async def ask_question_complete(
        self,
        question: str,
        timeout: Optional[float] = None,
        knowledge_base_id: Optional[str] = None,
    ) -> List[IMAMessage]:
        """获取完整的问题回答 - 支持自动重试"""
        start_time = time.time()

        async def _attempt_request():
            if timeout and (time.time() - start_time > timeout):
                raise asyncio.TimeoutError("Total timeout exceeded")

            messages = []
            # 每次尝试使用新的 session_id 以确保隔离
            session_id = await self.init_session(knowledge_base_id=knowledge_base_id)
            
            gen = self.ask_question(question, session_id=session_id)
            try:
                async for msg in gen:
                    messages.append(msg)
                    if timeout and (time.time() - start_time > timeout):
                        break
            finally:
                await gen.aclose()
            
            if not messages:
                raise ValueError("未收到有效消息")
            return messages

        async with self._ask_semaphore:
            try:
                async def _attempt_request_with_retry() -> List[IMAMessage]:
                    retryer = AsyncRetrying(
                        stop=stop_after_attempt(self.config.retry_count + 1),
                        wait=wait_exponential(multiplier=1, min=1, max=10),
                        retry=retry_if_exception(self._should_retry_ask_exception),
                        before_sleep=before_sleep_log(logger, "WARNING"),
                        reraise=True,
                    )

                    async for attempt in retryer:
                        with attempt:
                            try:
                                return await _attempt_request()
                            except AuthenticationError:
                                raise
                            except ValueError as e:
                                if self._is_login_expired_error(str(e)):
                                    logger.info("检测到登录失效，强制刷新 Token 并重试...")
                                    refreshed = await self.refresh_token()
                                    if not refreshed:
                                        raise AuthenticationError(self._build_auth_error_message()) from e
                                raise

                    raise ValueError("问答重试耗尽")

                base_backoff = 4.0
                max_code3_retry = 2

                for code3_retry_used in range(max_code3_retry + 1):
                    messages = await _attempt_request_with_retry()
                    if not self._is_code3_only_response(messages):
                        return messages

                    if code3_retry_used >= max_code3_retry:
                        return messages

                    delay = min(base_backoff * (2 ** code3_retry_used), 16.0) + random.uniform(0.2, 0.8)
                    logger.warning(
                        f"检测到 Code=3 且无文本响应，执行退避重试 (第{code3_retry_used + 1}/{max_code3_retry}次, sleep={delay:.1f}s)"
                    )
                    await asyncio.sleep(delay)

                return [IMAMessage(type=MessageType.SYSTEM, content="请求失败: Code=3 重试后仍无有效文本", raw="code3_retry_exhausted")]

            except Exception as e:
                logger.exception("问答失败", question_preview=question[:50])
                return [IMAMessage(type=MessageType.SYSTEM, content=f"请求失败: {e}", raw=str(e))]

    def _extract_text_content(self, messages: List[IMAMessage]) -> str:
        """从消息列表中提取文本内容 - 仅提取文本类型的消息"""
        if not messages:
            return "没有收到任何响应"

        content_parts = []

        for message in messages:
            # 仅提取 TextMessage 且类型为 TEXT 的内容
            if message.type == MessageType.TEXT:
                if isinstance(message, TextMessage) and message.text:
                    content_parts.append(message.text)
                elif hasattr(message, 'content') and message.content:
                    content_parts.append(message.content)

        # 拼接所有内容
        final_result = ''.join(content_parts).strip()

        # 清理和格式化结果
        final_result = self._clean_response_content(final_result)

        logger.debug(f"最终响应内容长度: {len(final_result)}")
        return final_result

    def _clean_response_content(self, content: str) -> str:
        """清理和格式化响应内容"""
        if not content:
            return content

        # 移除多余的空白行
        lines = content.split('\n')
        cleaned_lines = []
        prev_empty = False

        for line in lines:
            line = line.strip()
            if line:
                cleaned_lines.append(line)
                prev_empty = False
            elif not prev_empty:
                cleaned_lines.append('')
                prev_empty = True

        return '\n'.join(cleaned_lines)

    
    def _extract_knowledge_info(self, messages: List[IMAMessage]) -> List[Dict[str, Any]]:
        """从消息列表中提取知识库信息"""
        knowledge_items = []

        for message in messages:
            if isinstance(message, KnowledgeBaseMessage) and message.medias:
                for media in message.medias:
                    knowledge_items.append({
                        'id': media.id,
                        'title': media.title,
                        'subtitle': media.subtitle,
                        'introduction': media.introduction,
                        'timestamp': media.timestamp,
                        'knowledge_base': media.knowledge_base_info.name if media.knowledge_base_info else None
                    })

        return knowledge_items



  
