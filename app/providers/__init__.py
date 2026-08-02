"""
Provider 层入口: 客户端工厂与对外导出的请求/结果类型
"""

from app import config as serverConfig
from app.providers.base import ChatChunk, ChatRequest, ChatResult, ErrorCode, ProviderError, Usage
from app.providers.catalog import ModelCatalog
from app.providers.client import BaseChatClient
from app.providers.metering import UsageStore, recordStream
from app.providers.openai_compatible import AzureOpenAIClient, OpenAICompatibleClient
from app.providers.settings import ProviderSettings, describeProviders, getProviderSettings
from app.providers.transsion import TranssionAssistantClient, TranssionClient

##############################################################################################################################

supportedSources = tuple(serverConfig.SUPPORTED_SOURCES)

assistantSources = tuple(serverConfig.ASSISTANT_SOURCES)


def createChatClient(settings: ProviderSettings) -> BaseChatClient:
    """
    按 provider 类型创建聊天客户端
    """
    if settings.kind == "transsion":
        return TranssionClient(settings)
    if settings.kind == "azure":
        return AzureOpenAIClient(settings)
    return OpenAICompatibleClient(settings)


def createAssistantClient(settings: ProviderSettings) -> BaseChatClient:
    """
    按 provider 类型创建助手客户端; 未支持的来源返回 501
    """
    if settings.source not in assistantSources:
        raise ProviderError(
            code = "assistant_unsupported",
            message = f"来源 {settings.source} 暂不支持 assistant",
            statusCode = 501,
            details = {"source": settings.source, "supported": list(assistantSources)},
        )
    return TranssionAssistantClient(settings)

##############################################################################################################################