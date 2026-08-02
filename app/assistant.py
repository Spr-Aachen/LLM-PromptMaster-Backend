"""
Assistant 工具类
"""

import json
import logging
from PyEasyUtils import configManager
from typing import Optional, Union

from app.evaluation import promptTestStream
from app.store import readTemplate
from app.providers import ChatRequest, ProviderError, createAssistantClient, createChatClient
from app.providers.metering import recordStream

##############################################################################################################################

logger = logging.getLogger("PromptMaster.assistant")


class AssistantClient(object):
    """
    This class is used to interact with the assistant API
    """
    def __init__(self, settings, promptDir, usageStore = None, catalog = None):
        self.settings = settings
        self.usageStore = usageStore
        self.catalog = catalog
        self.assistantClient = createAssistantClient(settings)
        # 稳定性评分走对话模型通道
        self.chatClient = createChatClient(settings)

        manager = configManager(settings.configPath) if settings.configPath else None
        self.promptStabilityEvaluator = readTemplate(promptDir, manager, "promptFile_stabilityEvaluator")

    def buildRequest(self, assistantCode: Optional[str], messages: list, options: Optional[dict], stream: bool) -> ChatRequest:
        code = assistantCode or self.settings.assistant.get("assistantCode")
        return ChatRequest(
            provider = self.settings.source,
            model = f"assistant:{code or 'default'}",
            messages = messages,
            options = options or {},
            stream = stream,
            assistantCode = code,
        )

    def chunks(self, request: ChatRequest, client = None):
        client = client or self.assistantClient
        return recordStream(self.usageStore, client.stream(request), request, None)

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
        return [{"role": "user", "content": str(messages)}]

    async def run(self,
        assistantCode: Optional[str] = None,
        messages: Union[str, list] = ...,
        options: Optional[dict] = None
    ):
        messages = self.prepareMessages(messages)
        # 助手流式接口不稳定, 固定走非流式
        request = self.buildRequest(assistantCode, messages, options, stream = False)
        try:
            for chunk in self.chunks(request):
                if chunk.delta:
                    yield self.frame(200, chunk.delta)
        except ProviderError as error:
            logger.warning("助手 %s 调用失败: %s: %s", request.model, error.code, error.message)
            yield self.frame(error.statusCode, f"[{error.code}] {error.message}")

    async def test(self,
        assistantCode: Optional[str] = None,
        messages: Union[str, list] = ...,
        options: Optional[dict] = None,
        testTimes: Optional[int] = None
    ):
        messages = self.prepareMessages(messages)

        def complete(targetMessages, model = None):
            request = self.buildRequest(assistantCode, targetMessages, options, stream = False)
            return "".join(chunk.delta for chunk in self.chunks(request))

        def evaluate(targetMessages, model = None):
            request = ChatRequest(
                provider = self.settings.source,
                model = model or "gpt-4o",
                messages = targetMessages,
                options = options or {},
                stream = False,
                modelInfo = self.catalog.resolve(self.settings.source, model) if (self.catalog and model) else None,
            )
            return "".join(chunk.delta for chunk in self.chunks(request, client = self.chatClient))

        try:
            for result, statusCode in promptTestStream(
                messages,
                testTimes,
                complete = complete,
                evaluate = evaluate,
                promptStabilityEvaluator = self.promptStabilityEvaluator,
            ):
                yield self.frame(statusCode, result)
        except ProviderError as error:
            yield self.frame(error.statusCode, f"[{error.code}] {error.message}")

##############################################################################################################################