"""
Provider 客户端基类: 统一的请求重试、SSE 解析与结果归一化流程
"""

import json
import requests
from typing import Iterator

from app.providers.base import (
    DEFAULT_TIMEOUT,
    ChatChunk,
    ChatRequest,
    ChatResult,
    ErrorCode,
    ProviderError,
    estimateUsage,
    iterSSEData,
    loadsJson,
    responseError,
    streamError,
    withRetries,
)
from app.providers.settings import ProviderSettings

##############################################################################################################################


class BaseChatClient:
    """
    子类只需实现 buildRequest / extractChunk / extractResult
    """
    def __init__(self, settings: ProviderSettings, timeout: tuple = DEFAULT_TIMEOUT):
        self.settings = settings
        self.timeout = timeout
        self.session = requests.Session()

    ######################################################################

    def stream(self, request: ChatRequest) -> Iterator[ChatChunk]:
        """
        流式或一次性返回增量; 非流式时只产生一个 chunk
        """
        url, headers, payload = self.buildRequest(request)
        response = self.post(url, headers, payload, request)
        try:
            if request.stream:
                yield from self.parseStream(response, request)
            else:
                yield self.parseResponse(response, request)
        finally:
            response.close()

    def complete(self, request: ChatRequest) -> ChatResult:
        """
        收集流式结果, 供内部一次性调用使用
        """
        content = []
        usage = None
        finishReason = None
        for chunk in self.stream(request):
            if chunk.delta:
                content.append(chunk.delta)
            if chunk.usage is not None:
                usage = chunk.usage
            if chunk.finishReason:
                finishReason = chunk.finishReason
        text = "".join(content)
        return ChatResult(
            content = text,
            provider = request.provider,
            model = request.model,
            usage = usage or estimateUsage(request, text),
            finishReason = finishReason,
        )

    ######################################################################
    ######################################################################

    def post(self, url: str, headers: dict, payload: dict, request: ChatRequest):
        """
        发起请求; 网络错误与非 200 统一转成可重试/不可重试的 ProviderError
        """
        label = f"{request.provider}/{request.model}"

        def attempt():
            try:
                response = self.session.post(
                    url = url,
                    headers = headers,
                    data = json.dumps(payload, ensure_ascii = False),
                    stream = request.stream,
                    timeout = self.timeout,
                )
            except requests.Timeout as exc:
                raise ProviderError(
                    ErrorCode.timeout, f"请求超时: {exc}", statusCode = 504, retryable = True,
                ) from exc
            except requests.RequestException as exc:
                raise ProviderError(
                    ErrorCode.transport, f"网络错误: {exc}", statusCode = 502, retryable = True,
                ) from exc
            if response.status_code >= 400:
                error = responseError(response, stream = request.stream)
                response.close()
                raise error
            return response

        return withRetries(attempt, label = label)

    ######################################################################

    def parseStream(self, response, request: ChatRequest) -> Iterator[ChatChunk]:
        """
        解析 SSE: 支持 [DONE]、多行 data、错误帧; 完全无有效帧时报 provider_bad_response
        """
        sawPayload = False
        for data in iterSSEData(response):
            if data.strip() == "[DONE]":
                break
            payload = loadsJson(data)
            if not isinstance(payload, dict):
                continue
            error = streamError(payload)
            if error is not None:
                raise error
            sawPayload = True
            delta, usage, finishReason = self.extractChunk(payload)
            if delta or usage is not None or finishReason:
                yield ChatChunk(delta = delta, usage = usage, finishReason = finishReason)
        if not sawPayload:
            raise ProviderError(
                ErrorCode.badResponse,
                "Provider 返回了空的流式响应",
                statusCode = 502,
                details = {"provider": request.provider, "model": request.model},
            )

    def parseResponse(self, response, request: ChatRequest) -> ChatChunk:
        """
        解析非流式响应
        """
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError(
                ErrorCode.badResponse, f"Provider 返回非 JSON 响应: {exc}", statusCode = 502,
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderError(ErrorCode.badResponse, "Provider 返回了非对象响应", statusCode = 502)
        error = streamError(payload)
        if error is not None:
            raise error
        return self.extractResult(payload, request)

    ######################################################################

    def buildRequest(self, request: ChatRequest) -> tuple:
        """
        返回 (url, headers, payload)
        """
        raise NotImplementedError

    def extractChunk(self, payload: dict) -> tuple:
        """
        返回 (delta, usage, finishReason)
        """
        raise NotImplementedError

    def extractResult(self, payload: dict, request: ChatRequest) -> ChatChunk:
        raise NotImplementedError

##############################################################################################################################