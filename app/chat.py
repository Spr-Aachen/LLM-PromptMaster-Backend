"""
Chat 工具类: 注入系统 prompt、流式 JSON 封装与 token 计量
"""

import json
import logging
from PyEasyUtils import configManager
from typing import Optional, Union

from app.evaluation import promptTestStream
from app.store import readTemplate
from app.providers import ChatRequest, ProviderError, createChatClient
from app.providers.metering import recordStream

##############################################################################################################################

logger = logging.getLogger("PromptMaster.chat")


class ChatClient(object):
    """
    This class is used to interact with the chat completions API
    """
    def __init__(self, settings, promptDir, usageStore = None, catalog = None):
        self.settings = settings
        self.promptDir = promptDir
        self.usageStore = usageStore
        self.catalog = catalog
        self.client = createChatClient(settings)

        manager = configManager(settings.configPath) if settings.configPath else None
        self.Prompt = readTemplate(promptDir, manager, "promptFile")
        self.promptStabilityEvaluator = readTemplate(promptDir, manager, "promptFile_stabilityEvaluator")
        self.promptReconstructor = readTemplate(promptDir, manager, "promptFile_reconstructor")

    ######################################################################

    def buildRequest(self, model: str, messages: list, options: Optional[dict], stream: bool) -> ChatRequest:
        modelInfo = self.catalog.resolve(self.settings.source, model) if self.catalog else None
        return ChatRequest(
            provider = self.settings.source,
            model = model,
            messages = messages,
            options = options or {},
            stream = stream,
            modelInfo = modelInfo,
        )

    def chunks(self, request: ChatRequest):
        """
        透传 provider 增量并记账
        """
        pricing = self.catalog.pricingFor(request.provider, request.model) if self.catalog else None
        return recordStream(self.usageStore, self.client.stream(request), request, pricing)

    @staticmethod
    def frame(statusCode: int, data: str) -> str:
        """
        一行一帧 (NDJSON), 便于客户端按行切分
        """
        return json.dumps(
            {"code": statusCode, "message": "成功" if statusCode == 200 else "失败", "data": data},
            ensure_ascii = False,
        ) + "\n"

    def prepareMessages(self, messages: Union[str, list]) -> list:
        if isinstance(messages, list) and messages and isinstance(messages[0], dict):
            return messages
        return [
            {"role": "system", "content": self.Prompt},
            {"role": "user", "content": str(messages)},
        ]

    def resolveModel(self, model: Optional[str]) -> Optional[str]:
        """
        未显式指定模型时回退到目录默认模型
        """
        if model:
            return model
        return self.catalog.defaultModel(self.settings.source) if self.catalog else None

    ######################################################################

    async def run(self,
        model: Optional[str] = None,
        messages: Union[str, list] = ...,
        options: Optional[dict] = None
    ):
        messages = self.prepareMessages(messages)
        model = self.resolveModel(model)
        if model is None:
            yield self.frame(400, "[model_required] 未指定模型")
            return
        request = self.buildRequest(model, messages, options, stream = True)
        try:
            for chunk in self.chunks(request):
                if chunk.delta:
                    yield self.frame(200, chunk.delta)
        except ProviderError as error:
            logger.warning("来源 %s 模型 %s 调用失败: %s: %s", request.provider, request.model, error.code, error.message)
            yield self.frame(error.statusCode, f"[{error.code}] {error.message}")

    async def test(self,
        model: Optional[str] = None,
        messages: Union[str, list] = ...,
        options: Optional[dict] = None,
        testTimes: Optional[int] = None
    ):
        messages = self.prepareMessages(messages)
        defaultModel = self.resolveModel(model)
        if defaultModel is None:
            yield self.frame(400, "[model_required] 未指定模型")
            return

        def complete(targetMessages, model = None):
            request = self.buildRequest(model or defaultModel, targetMessages, options, stream = False)
            return "".join(chunk.delta for chunk in self.chunks(request))

        def streamText(targetMessages, model = None):
            request = self.buildRequest(model or defaultModel, targetMessages, options, stream = True)
            for chunk in self.chunks(request):
                if chunk.delta:
                    yield chunk.delta

        try:
            for result, statusCode in promptTestStream(
                messages,
                testTimes,
                complete = complete,
                stream = streamText,
                promptStabilityEvaluator = self.promptStabilityEvaluator,
                promptReconstructor = self.promptReconstructor,
            ):
                yield self.frame(statusCode, result)
        except ProviderError as error:
            yield self.frame(error.statusCode, f"[{error.code}] {error.message}")

##############################################################################################################################