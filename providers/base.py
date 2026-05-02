"""
Provider 层公共定义: 请求/结果模型、错误类型、重试策略、SSE 解析与 token 估算
"""

import json
import logging
import math
import random
import time
import json_repair
import requests
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterator, Optional

from utils.errors import ApiError

##############################################################################################################################

logger = logging.getLogger("PromptMaster.providers")

# 重试策略: 最多 3 次, 指数退避 + 抖动, Retry-After 优先但不超过上限
MAX_ATTEMPTS = 3
BASE_RETRY_DELAY = 0.5
MAX_RETRY_DELAY = 8.0
RETRY_JITTER = 0.5

# (连接超时, 读取超时) 秒
DEFAULT_TIMEOUT = (10.0, 180.0)

# 错误响应体读取上限
MAX_ERROR_BODY_BYTES = 64 * 1024


class ErrorCode:
    """
    稳定的错误码字符串, 客户端与日志按此匹配
    """
    invalidSource = "invalid_source"
    invalidEnv = "invalid_env"
    providerNotConfigured = "provider_not_configured"
    authFailed = "auth_failed"
    rateLimited = "rate_limited"
    serverError = "provider_server_error"
    badRequest = "provider_bad_request"
    timeout = "provider_timeout"
    transport = "provider_transport_error"
    badResponse = "provider_bad_response"
    streamInterrupted = "stream_interrupted"
    modelUnsupported = "model_unsupported"


# 可直接重试的错误码
RETRYABLE_CODES = frozenset({
    ErrorCode.rateLimited,
    ErrorCode.serverError,
    ErrorCode.timeout,
    ErrorCode.transport,
})


class ProviderError(ApiError):
    """
    Provider 调用错误, 继承 ApiError 以便复用统一错误体; 额外携带是否可重试与 Retry-After
    """
    def __init__(self,
        code: str,
        message: str,
        statusCode: int = 502,
        retryable: bool = False,
        retryAfter: Optional[float] = None,
        details: Any = None,
    ):
        super().__init__(code = code, message = message, statusCode = statusCode, details = details)
        self.retryable = retryable if retryable else code in RETRYABLE_CODES
        self.retryAfter = retryAfter


##############################################################################################################################

@dataclass
class Usage:
    """
    token 与成本计量; estimated 表示 token 由字符估算而非 provider 上报
    """
    tokensIn: int = 0
    tokensOut: int = 0
    costUsd: Optional[float] = None
    estimated: bool = False

    def toDict(self) -> dict:
        return {
            "tokensIn": self.tokensIn,
            "tokensOut": self.tokensOut,
            "costUsd": self.costUsd,
            "estimated": self.estimated,
        }


@dataclass
class ChatRequest:
    """
    provider 无关的聊天请求
    """
    provider: str
    model: str
    messages: list = field(default_factory = list)
    options: dict = field(default_factory = dict)
    stream: bool = True
    apiKey: Optional[str] = None
    modelInfo: Optional[dict] = None
    # transsion 助手接口使用
    assistantCode: Optional[str] = None


@dataclass
class ChatChunk:
    """
    流式增量; 最后一个 chunk 可能只带 usage
    """
    delta: str = ""
    usage: Optional[Usage] = None
    finishReason: Optional[str] = None


@dataclass
class ChatResult:
    """
    非流式结果
    """
    content: str
    provider: str
    model: str
    usage: Usage = field(default_factory = Usage)
    finishReason: Optional[str] = None


##############################################################################################################################

def estimateTokens(text: Any) -> int:
    """
    按字符估算 token: CJK 字符约 1 token, 其余字符约 4 字符 1 token
    """
    if text is None:
        return 0
    value = text if isinstance(text, str) else str(text)
    if not value:
        return 0
    cjk = 0
    other = 0
    for char in value:
        if "\u3040" <= char <= "\u30ff" or "\u3400" <= char <= "\u9fff" or "\uac00" <= char <= "\ud7af":
            cjk += 1
        else:
            other += 1
    return cjk + math.ceil(other / 4)


def estimateMessagesTokens(messages: list) -> int:
    """
    估算消息列表的输入 token, 每条消息附带固定开销
    """
    total = 0
    for message in messages or []:
        if isinstance(message, dict):
            total += estimateTokens(message.get("content")) + 4
        else:
            total += estimateTokens(message) + 4
    return total


def estimateUsage(request: ChatRequest, content: str) -> Usage:
    """
    provider 未上报 usage 时按字符估算, 并标记 estimated
    """
    return Usage(
        tokensIn = estimateMessagesTokens(request.messages),
        tokensOut = estimateTokens(content),
        estimated = True,
    )


def parseUsage(raw: Any) -> Optional[Usage]:
    """
    兼容 OpenAI 与 transsion 的 usage 字段命名
    """
    if not isinstance(raw, dict):
        return None
    tokensIn = _firstInt(raw, ("prompt_tokens", "input_tokens", "promptTokens", "inputTokens"))
    tokensOut = _firstInt(raw, ("completion_tokens", "output_tokens", "completionTokens", "outputTokens"))
    if tokensIn is None and tokensOut is None:
        return None
    return Usage(tokensIn = tokensIn or 0, tokensOut = tokensOut or 0)


def _firstInt(source: dict, keys) -> Optional[int]:
    for key in keys:
        value = source.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return None


##############################################################################################################################

def loadsJson(text: str) -> Any:
    """
    先按标准 JSON 解析, 失败时用 json_repair 兜底, 仍失败返回 None
    """
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    try:
        return json_repair.loads(text)
    except Exception:
        return None


def flattenContent(content: Any) -> str:
    """
    把字符串或多段内容 (视觉输入回包) 拼成文本
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text")
                if text is None:
                    text = part.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    if isinstance(content, dict):
        return flattenContent(content.get("text"))
    return str(content)


def iterSSEData(response) -> Iterator[str]:
    """
    解析 SSE 响应, 按空行聚合多行 data:, 跳过注释与 event/id/retry 字段
    """
    dataLines = []
    try:
        for rawLine in response.iter_lines(chunk_size = 1024):
            if rawLine is None:
                continue
            line = rawLine.decode("utf-8", errors = "replace").rstrip("\r")
            if not line:
                if dataLines:
                    yield "\n".join(dataLines)
                    dataLines = []
                continue
            if line.startswith(":"):
                continue
            if line.startswith("data:"):
                dataLines.append(line[5:].lstrip())
            elif line.startswith(("event:", "id:", "retry:")):
                continue
            else:
                dataLines.append(line)
    except requests.Timeout as exc:
        raise ProviderError(
            ErrorCode.timeout, f"响应流读取超时: {exc}", statusCode = 504, retryable = False,
        ) from exc
    except requests.RequestException as exc:
        raise ProviderError(
            ErrorCode.streamInterrupted, f"响应流中断: {exc}", statusCode = 502, retryable = False,
        ) from exc
    if dataLines:
        yield "\n".join(dataLines)


def parseRetryAfter(value: Optional[str]) -> Optional[float]:
    """
    解析 Retry-After: 支持秒数与 HTTP 日期
    """
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        pass
    try:
        target = parsedate_to_datetime(value)
        if target.tzinfo is None:
            target = target.replace(tzinfo = timezone.utc)
        return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        return None


def retryDelay(attempt: int, retryAfter: Optional[float] = None) -> float:
    """
    计算退避时长: 优先 Retry-After, 否则指数退避并叠加 ±50% 抖动, 上限统一裁剪
    """
    if retryAfter is not None:
        return min(max(0.0, retryAfter), MAX_RETRY_DELAY)
    delay = min(BASE_RETRY_DELAY * (2 ** attempt), MAX_RETRY_DELAY)
    jittered = delay * (1.0 - RETRY_JITTER + random.random() * RETRY_JITTER * 2.0)
    return min(jittered, MAX_RETRY_DELAY)


def withRetries(func, attempts: int = MAX_ATTEMPTS, label: str = "provider"):
    """
    按错误类型重试: 仅重试 retryable 错误, 其余立即抛出
    """
    lastError = None
    for attempt in range(attempts):
        try:
            return func()
        except ProviderError as error:
            lastError = error
            if not error.retryable or attempt >= attempts - 1:
                raise
            delay = retryDelay(attempt, error.retryAfter)
            logger.warning(
                "%s 请求失败(%s), %.2fs 后重试 (%d/%d)",
                label, error.code, delay, attempt + 1, attempts,
            )
            time.sleep(delay)
    raise lastError


##############################################################################################################################

def classifyStatus(status: int, message: str) -> tuple[str, bool]:
    """
    按 HTTP 状态与错误文本归类错误码
    """
    lowered = (message or "").lower()
    if status == 429:
        return ErrorCode.rateLimited, True
    if status in (401, 403):
        return ErrorCode.authFailed, False
    if status == 408:
        return ErrorCode.timeout, True
    if status >= 500:
        return ErrorCode.serverError, True
    if status == 404 or ("model" in lowered and any(
        token in lowered for token in ("not found", "does not exist", "unsupported", "invalid", "unknown")
    )):
        return ErrorCode.modelUnsupported, False
    return ErrorCode.badRequest, False


def responseError(response, stream: bool = False) -> ProviderError:
    """
    把非 200 响应转成结构化 ProviderError, 保留上游状态码与错误体摘要
    """
    try:
        if stream:
            body = response.raw.read(MAX_ERROR_BODY_BYTES, decode_content = True).decode("utf-8", errors = "replace")
        else:
            body = response.text[:MAX_ERROR_BODY_BYTES]
    except Exception:
        body = ""
    message = _extractErrorMessage(body) or str(response.reason or "Provider 请求失败")
    code, retryable = classifyStatus(response.status_code, message)
    return ProviderError(
        code = code,
        message = message,
        statusCode = response.status_code if response.status_code < 500 else 502,
        retryable = retryable,
        retryAfter = parseRetryAfter(response.headers.get("Retry-After")),
        details = {"providerStatus": response.status_code, "body": body[:2000]},
    )


def _extractErrorMessage(body: str) -> Optional[str]:
    payload = loadsJson(body)
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            for key in ("message", "msg", "detail"):
                value = error.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        if isinstance(error, str) and error.strip():
            return error.strip()
        for key in ("message", "msg", "detail", "errorMessage"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return body.strip()[:500] or None


def streamError(payload: dict) -> Optional[ProviderError]:
    """
    识别 SSE 内的错误帧
    """
    error = payload.get("error") if isinstance(payload, dict) else None
    if not error:
        return None
    if isinstance(error, dict):
        message = str(error.get("message") or error.get("msg") or error)
        code = str(error.get("code") or error.get("type") or "")
    else:
        message = str(error)
        code = ""
    errorCode = classifyFrame(f"{code} {message}")
    return ProviderError(
        code = errorCode,
        message = message,
        statusCode = 502,
        retryable = errorCode in RETRYABLE_CODES,
        details = {"frame": payload},
    )


def classifyFrame(hint: str) -> str:
    """
    按错误帧文本归类; 识别 rate limit / auth / model 等关键词
    """
    lowered = (hint or "").lower()
    if "rate" in lowered and "limit" in lowered:
        return ErrorCode.rateLimited
    if "quota" in lowered:
        return ErrorCode.rateLimited
    if any(token in lowered for token in ("auth", "api key", "api_key", "unauthorized", "forbidden")):
        return ErrorCode.authFailed
    if "model" in lowered and any(token in lowered for token in ("not found", "does not exist", "unsupported", "invalid", "unknown")):
        return ErrorCode.modelUnsupported
    if any(token in lowered for token in ("timeout", "timed out")):
        return ErrorCode.timeout
    return classifyStatus(400, lowered)[0]

##############################################################################################################################