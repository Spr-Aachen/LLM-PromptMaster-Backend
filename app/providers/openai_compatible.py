"""
OpenAI 兼容客户端: 覆盖 openai / deepseek / azure (含 DALL·E 图像生成)
"""

from app.providers.base import (
    DEFAULT_TIMEOUT,
    ChatChunk,
    ChatRequest,
    ErrorCode,
    ProviderError,
    flattenContent,
    parseUsage,
)
from app.providers.client import BaseChatClient

##############################################################################################################################


class OpenAICompatibleClient(BaseChatClient):
    """
    OpenAI 兼容 chat/completions 客户端
    """
    kind = "openai"
    supportsStreamUsage = True

    def __init__(self, settings, timeout = None):
        super().__init__(settings, timeout or DEFAULT_TIMEOUT)
        # azure 与 openai 共用实现, 认证与 URL 规则由 kind 决定
        if settings.kind in ("azure", "openai"):
            self.kind = settings.kind

    def buildRequest(self, request: ChatRequest) -> tuple:
        """
        组装 URL、请求头与请求体
        """
        isImage = (request.modelInfo or {}).get("kind") == "image"
        url = self.buildURL(request, isImage)
        headers = self.buildHeaders()
        options = dict(request.options or {})

        if isImage:
            payload = {**options, "prompt": self.imagePrompt(request.messages)}
            return url, headers, payload

        payload = {
            **options,
            "messages": request.messages,
            "stream": bool(request.stream),
        }
        if self.kind != "azure":
            payload["model"] = request.model
        if request.stream and self.supportsStreamUsage:
            payload.setdefault("stream_options", {"include_usage": True})
        return url, headers, payload

    def buildURL(self, request: ChatRequest, isImage: bool) -> str:
        baseUrl = (self.settings.baseUrl or "").rstrip("/")
        segment = "images/generations" if isImage else "chat/completions"
        if self.kind == "azure":
            version = self.settings.apiVersion or "2024-10-21"
            return f"{baseUrl}/openai/deployments/{request.model}/{segment}?api-version={version}"
        return f"{baseUrl}/{segment}"

    def buildHeaders(self) -> dict:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.kind == "azure":
            headers["api-key"] = self.settings.apiKey or ""
        else:
            headers["Authorization"] = f"Bearer {self.settings.apiKey or ''}"
        return headers

    @staticmethod
    def imagePrompt(messages: list) -> str:
        parts = []
        for message in messages or []:
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                parts.append(message["content"])
        return "\n".join(parts)

    ######################################################################

    def extractChunk(self, payload: dict) -> tuple:
        """
        从流式帧提取增量、usage 与结束原因
        """
        usage = parseUsage(payload.get("usage"))
        choices = payload.get("choices") or []
        if not choices:
            return "", usage, None
        choice = choices[0] or {}
        delta = choice.get("delta")
        if isinstance(delta, dict):
            text = flattenContent(delta.get("content"))
        else:
            text = flattenContent((choice.get("message") or {}).get("content"))
        return text, usage, choice.get("finish_reason")

    def extractResult(self, payload: dict, request: ChatRequest) -> ChatChunk:
        """
        从一次性响应提取正文、usage 与结束原因
        """
        if (request.modelInfo or {}).get("kind") == "image":
            images = payload.get("data") or []
            url = images[0].get("url") if images and isinstance(images[0], dict) else None
            if not url:
                raise ProviderError(ErrorCode.badResponse, "图像生成响应缺少 url", statusCode = 502)
            return ChatChunk(delta = url, usage = parseUsage(payload.get("usage")))

        choices = payload.get("choices") or []
        if not choices:
            raise ProviderError(
                ErrorCode.badResponse,
                "Provider 响应缺少 choices",
                statusCode = 502,
                details = {"provider": request.provider, "model": request.model},
            )
        choice = choices[0] or {}
        text = flattenContent((choice.get("message") or {}).get("content"))
        return ChatChunk(
            delta = text,
            usage = parseUsage(payload.get("usage")),
            finishReason = choice.get("finish_reason"),
        )


class AzureOpenAIClient(OpenAICompatibleClient):
    """
    Azure OpenAI: api-key 头 + deployments URL
    """
    kind = "azure"

##############################################################################################################################