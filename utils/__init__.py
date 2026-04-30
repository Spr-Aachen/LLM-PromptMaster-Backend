from . import request_azure, request_deepseek, request_openai, request_transsion
from .errors import ApiError
from .io import saveUpload

##############################################################################################################################

class SourceName:
    azure = 'azure'
    deepseek = 'deepseek'
    openai = 'openai'
    transsion = 'transsion'


supportedSources = (
    SourceName.azure,
    SourceName.deepseek,
    SourceName.openai,
    SourceName.transsion,
)


GPT_HANDLERS = {
    SourceName.azure: request_azure.gptRequest,
    SourceName.deepseek: request_deepseek.gptRequest,
    SourceName.openai: request_openai.gptRequest,
    SourceName.transsion: request_transsion.gptRequest,
}


ASSISTANT_HANDLERS = {
    SourceName.transsion: request_transsion.assistantRequest,
}


modelsInfo = {
    SourceName.azure: list(request_azure.chatURLs.keys()),
    SourceName.deepseek: list(request_deepseek.chatURLs.keys()),
    SourceName.openai: list(request_openai.chatURLs.keys()),
    SourceName.transsion: list(request_transsion.chatURLs.keys())
}


def _resolveHandler(handlers: dict, sourceName: SourceName, errorCode: str, errorMessage: str, statusCode: int = 400):
    handler = handlers.get(sourceName)
    if handler is None:
        raise ApiError(
            code = errorCode,
            message = errorMessage,
            statusCode = statusCode,
            details = {"source": sourceName, "supported": list(handlers)},
        )
    return handler


def gptRequest(
    sourceName: SourceName = SourceName.azure,
    **kwargs
):
    """
    """
    handler = _resolveHandler(
        GPT_HANDLERS, sourceName,
        "invalid_source", f"不支持的来源: {sourceName}", 400,
    )
    return handler(**kwargs)


def assistantRequest(
    sourceName: SourceName = SourceName.azure,
    **kwargs
):
    """
    """
    handler = _resolveHandler(
        ASSISTANT_HANDLERS, sourceName,
        "assistant_unsupported", f"来源 {sourceName} 暂不支持 assistant", 501,
    )
    return handler(**kwargs)

##############################################################################################################################