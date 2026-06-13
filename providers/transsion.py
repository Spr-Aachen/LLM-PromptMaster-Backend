"""
Transsion 网关客户端: OAuth 取令牌、URL 重写 (streamChatCompletion / streamChat)、data 包裹解包
"""

import threading
import time
import requests
from typing import Iterator

from providers.base import (
    DEFAULT_TIMEOUT,
    ChatChunk,
    ChatRequest,
    ErrorCode,
    ProviderError,
    flattenContent,
    loadsJson,
    parseUsage,
    withRetries,
)
from providers.client import BaseChatClient
from providers.settings import ProviderSettings

##############################################################################################################################

TOKEN_PATH = "service-pf-open-gateway/oauth/token"

CHAT_PATH = "api/azure/openai/chatCompletion"

IMAGE_PATHS = {
    "dall-e2": "api/azure/openai/generationImage",
    "dall-e3": "api/azure/openai/generateDell3Image?deploymentId=Dalle3",
}

# 令牌提前刷新的余量 (秒)
TOKEN_REFRESH_MARGIN = 60.0


def unwrapCandidates(payload: dict) -> Iterator[dict]:
    """
    依次产出 dataObject / data / data.data 包裹层级, 兼容网关的多层包装
    """
    yield payload
    for key in ("dataObject", "data"):
        node = payload.get(key)
        if not isinstance(node, dict):
            continue
        yield node
        inner = node.get("data")
        if isinstance(inner, dict):
            yield inner


class TranssionClient(BaseChatClient):
    """
    Transsion 对话模型网关客户端
    """
    kind = "transsion"
    # 网关不保证支持 stream_options.include_usage, 缺失时由计量层估算并标记
    supportsStreamUsage = False

    def __init__(self, settings: ProviderSettings, timeout: tuple = DEFAULT_TIMEOUT):
        super().__init__(settings, timeout)
        self._token = None
        self._tokenExpiry = 0.0
        self._tokenLock = threading.Lock()

    ######################################################################

    def accessToken(self) -> str:
        """
        获取 OAuth 访问令牌, 带进程内缓存
        """
        with self._tokenLock:
            if self._token and time.monotonic() < self._tokenExpiry:
                return self._token
            self._token = self.requestToken()
            return self._token

    def requestToken(self) -> str:
        url = (
            f"{(self.settings.pfGateway or '').rstrip('/')}/{TOKEN_PATH}"
            f"?grant_type=client_credentials"
            f"&client_id={self.settings.appID or ''}"
            f"&client_secret={self.settings.appSecret or ''}"
        )

        def attempt():
            try:
                response = self.session.get(url = url, timeout = self.timeout)
            except requests.Timeout as exc:
                raise ProviderError(
                    ErrorCode.timeout, f"获取访问令牌超时: {exc}", statusCode = 504, retryable = True,
                ) from exc
            except requests.RequestException as exc:
                raise ProviderError(
                    ErrorCode.transport, f"获取访问令牌失败: {exc}", statusCode = 502, retryable = True,
                ) from exc
            if response.status_code != 200:
                raise ProviderError(
                    ErrorCode.authFailed,
                    f"获取访问令牌失败, 上游状态码 {response.status_code}",
                    statusCode = 502,
                    retryable = response.status_code >= 500,
                    details = {"providerStatus": response.status_code},
                )
            return response

        response = withRetries(attempt, label = f"{self.settings.source}/token")
        payload = loadsJson(response.text) or {}
        data = payload.get("data") if isinstance(payload, dict) else None
        token = data.get("access_token") if isinstance(data, dict) else None
        if not token:
            raise ProviderError(
                ErrorCode.authFailed, "访问令牌响应缺少 access_token", statusCode = 502,
            )
        try:
            expiresIn = float(data.get("expires_in") or 3600)
        except (TypeError, ValueError):
            expiresIn = 3600.0
        self._tokenExpiry = time.monotonic() + max(TOKEN_REFRESH_MARGIN, expiresIn - TOKEN_REFRESH_MARGIN)
        return token

    ######################################################################

    def buildRequest(self, request: ChatRequest) -> tuple:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.accessToken()}",
        }
        options = dict(request.options or {})
        isImage = (request.modelInfo or {}).get("kind") == "image"

        if isImage:
            path = IMAGE_PATHS.get(request.model)
            if path is None:
                raise ProviderError(
                    ErrorCode.modelUnsupported,
                    f"来源 {request.provider} 不支持图像模型: {request.model}",
                    statusCode = 400,
                    details = {"provider": request.provider, "model": request.model},
                )
            payload = {**options, "prompt": self.imagePrompt(request.messages)}
        else:
            path = f"{CHAT_PATH}?deploymentId={request.model}"
            if request.stream:
                path = path.replace("/chatCompletion", "/streamChatCompletion")
            payload = {"messages": request.messages, **options}

        url = f"{(self.settings.gptGateway or '').rstrip('/')}/{path}"
        return url, headers, payload

    @staticmethod
    def imagePrompt(messages: list) -> str:
        parts = []
        for message in messages or []:
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                parts.append(message["content"])
        return "\n".join(parts)

    ######################################################################

    def extractChunk(self, payload: dict) -> tuple:
        return self._extract(payload, stream = True)

    def extractResult(self, payload: dict, request: ChatRequest) -> ChatChunk:
        if (request.modelInfo or {}).get("kind") == "image":
            return self._extractImage(payload)
        text, usage, finishReason = self._extract(payload, stream = False)
        return ChatChunk(delta = text, usage = usage, finishReason = finishReason)

    def _extract(self, payload: dict, stream: bool) -> tuple:
        usage = None
        for node in unwrapCandidates(payload):
            parsed = parseUsage(node.get("usage"))
            if parsed is not None:
                usage = parsed
                break
        for node in unwrapCandidates(payload):
            dataContent = node.get("dataContent")
            if isinstance(dataContent, str) and dataContent:
                return dataContent, usage, None
            choices = node.get("choices")
            if isinstance(choices, list) and choices:
                choice = choices[0] or {}
                if stream and isinstance(choice.get("delta"), dict):
                    text = flattenContent(choice["delta"].get("content"))
                else:
                    text = flattenContent((choice.get("message") or {}).get("content"))
                return text, usage, choice.get("finish_reason")
        return "", usage, None

    def _extractImage(self, payload: dict) -> ChatChunk:
        usage = None
        for node in unwrapCandidates(payload):
            parsed = parseUsage(node.get("usage"))
            if parsed is not None:
                usage = parsed
                break
        for node in unwrapCandidates(payload):
            images = node.get("data")
            if isinstance(images, list) and images and isinstance(images[0], dict):
                url = images[0].get("url")
                if url:
                    return ChatChunk(delta = url, usage = usage)
        raise ProviderError(ErrorCode.badResponse, "图像生成响应缺少 url", statusCode = 502)


class TranssionAssistantClient(TranssionClient):
    """
    Transsion 助手 (智库) 客户端
    """
    def buildRequest(self, request: ChatRequest) -> tuple:
        assistantCode = request.assistantCode or self.settings.assistant.get("assistantCode")
        chatURL = (self.settings.assistant.get("chatURL") or "").strip("/")
        if not assistantCode or not chatURL:
            raise ProviderError(
                ErrorCode.providerNotConfigured,
                "助手接口缺少 chatURL 或 assistantCode 配置",
                statusCode = 400,
                details = {"provider": request.provider},
            )
        url = f"{(self.settings.pfGateway or '').rstrip('/')}/{chatURL}/{assistantCode}"
        if request.stream:
            url = url.replace("/chat", "/streamChat")
        headers = {
            "Content-Type": "application/json",
            "x-header-tenant": self.settings.assistant.get("xHeaderTenant") or "",
            "Authorization": f"Bearer {self.accessToken()}",
        }
        options = dict(request.options or {})
        payload = {"messages": request.messages, **({"options": options} if options else {})}
        return url, headers, payload

    def extractChunk(self, payload: dict) -> tuple:
        return self._extract(payload, stream = True)

    def extractResult(self, payload: dict, request: ChatRequest) -> ChatChunk:
        text, usage, finishReason = self._extract(payload, stream = False)
        return ChatChunk(delta = text, usage = usage, finishReason = finishReason)

##############################################################################################################################