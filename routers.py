from fastapi.routing import APIRouter
from fastapi.responses import StreamingResponse
from fastapi.datastructures import UploadFile
from fastapi import Query
from typing import List, Optional

import config as serverConfig
from utils import saveUpload
from utils.errors import ApiError
from providers import (
    ModelCatalog,
    UsageStore,
    assistantSources,
    describeProviders,
    getProviderSettings,
    supportedSources,
)
from gpt import GPTClient
from assistant import AssistantClient
from wrapper import ChatManager
from schemas import (
    AnswerReceiveRequest,
    ChatRequest,
    ConversationCreateRequest,
    ConversationRenameRequest,
    MessageAddRequest,
    PromptCreateRequest,
    PromptRenameRequest,
    PromptSaveRequest,
    QuestionSaveRequest,
)

##############################################################################################################################

router_utils = APIRouter(
    prefix = "",
)


def normalizeOptional(value):
    """
    归一化可选的短文本参数, 不执行任何输入
    """
    if value is None:
        return None
    value = str(value).strip()
    return None if value in ("", "None") else value


def validateName(name: str, field: str) -> str:
    """
    校验会参与文件名的 prompt / conversation 名称, 拒绝路径穿越
    """
    if name in ("", ".", "..") or "/" in name or "\\" in name or "\x00" in name:
        raise ApiError(
            code = "invalid_name",
            message = f"非法的{field}: {name!r}",
            details = {"field": field, "value": name},
        )
    return name


@router_utils.post("/upload")
async def upload_file(files: List[UploadFile]):
    serverConfig.uploadDir.mkdir(parents = True, exist_ok = True)
    uploadedFiles = []
    for file in files:
        try:
            uploadedFiles.append(await saveUpload(file, serverConfig.uploadDir, serverConfig.maxUploadBytes))
        finally:
            await file.close()
    return {"status": "Succeeded", "files": uploadedFiles}

##############################################################################################################################

router_chat = APIRouter(
    prefix = "/chat",
)


chatManager = ChatManager(
    promptDir = serverConfig.promptDir.as_posix(),
    conversationDir = serverConfig.conversationDir.as_posix(),
    questionDir = serverConfig.questionDir.as_posix()
)

modelCatalog = ModelCatalog()

usageStore = UsageStore(serverConfig.usageDbPath)


def loadMessages(historyID: str) -> list:
    """
    读取历史消息, 未知会话返回 404 而不是 KeyError
    """
    try:
        return chatManager.getHistory(historyID)[0]
    except KeyError as exc:
        raise ApiError(
            code = "history_not_found",
            message = f"会话不存在: {historyID}",
            statusCode = 404,
            details = {"historyID": historyID},
        ) from exc


def buildMessages(payload: ChatRequest) -> list:
    """
    历史消息 + 本次请求的消息
    """
    if payload.message is None:
        raise ApiError(
            code = "invalid_message",
            message = "缺少 message",
            details = {"historyID": payload.historyID},
        )
    return loadMessages(payload.historyID) + [payload.message]


def resolveModel(source: str, model: Optional[str]) -> str:
    """
    目录内模型直接用; 目录外 id 也放行 (由 provider 决定是否可用); 缺省取目录默认模型
    """
    modelName = normalizeOptional(model)
    if modelName is None:
        modelName = modelCatalog.defaultModel(source)
    if modelName is None:
        raise ApiError(
            code = "model_required",
            message = f"来源 {source} 没有可用模型, 请显式指定 model",
            details = {"source": source},
        )
    return modelName


@router_chat.get("/getCapabilities")
async def getCapabilities():
    return {
        "sources": list(supportedSources),
        "assistantSources": list(assistantSources),
    }


@router_chat.get("/getModelsInfo")
async def getModelsInfo():
    """
    兼容旧客户端的 {source: [modelId]} 结构; 新客户端使用 /api/meta/models
    """
    return {source: modelCatalog.modelIds(source) for source in supportedSources}


@router_chat.get("/loadPrompts")
async def loadPrompts():
    prompts = chatManager.loadPrompts()
    return prompts


@router_chat.get("/getPrompt")
async def getPrompt(promptID):
    prompt = chatManager.getPrompt(promptID)
    return prompt


@router_chat.post("/createPrompt")
async def createPrompt(payload: PromptCreateRequest):
    promptID, promptName = chatManager.createPrompt(validateName(payload.name, "Prompt 名称"))
    return promptID, promptName


@router_chat.post("/renamePrompt")
async def renamePrompt(payload: PromptRenameRequest):
    chatManager.renamePrompt(payload.promptID, validateName(payload.newName, "Prompt 名称"))


@router_chat.post("/deletePrompt")
async def deletePrompt(promptID):
    chatManager.deletePrompt(promptID)


@router_chat.post("/savePrompt")
async def savePrompt(payload: PromptSaveRequest):
    chatManager.savePrompt(payload.promptID, payload.prompt)


@router_chat.get("/loadHistories")
async def loadHistories():
    histories = chatManager.loadHistories()
    return histories


@router_chat.get("/getHistory")
async def getHistory(historyID):
    messages, question = chatManager.getHistory(historyID)
    return messages, question


@router_chat.post("/createConversation")
async def createConversation(payload: ConversationCreateRequest):
    historyID, conversationName = chatManager.createConversation(validateName(payload.name, "对话名称"))
    return historyID, conversationName


@router_chat.post("/renameConversation")
async def renameConversation(payload: ConversationRenameRequest):
    chatManager.renameConversation(payload.historyID, validateName(payload.newName, "对话名称"))


@router_chat.post("/deleteConversation")
async def deleteConversation(historyID):
    chatManager.deleteConversation(historyID)


@router_chat.post("/saveQuestion")
async def saveQuestion(payload: QuestionSaveRequest):
    chatManager.saveQuestion(payload.historyID, payload.question)


@router_chat.post("/applyPrompt")
async def applyPrompt(promptID):
    chatManager.applyPrompt(promptID)


@router_chat.post("/addUserMessage")
async def addUserMessage(payload: MessageAddRequest):
    chatManager.addUserMessage(payload.historyID, payload.message)


@router_chat.post("/recieveAnswer")
async def recieveAnswer(payload: AnswerReceiveRequest):
    messages = chatManager.recieveAnswer(payload.historyID, payload.recievedText)
    return messages


@router_chat.post("/gpt")
async def gpt(payload: ChatRequest):
    settings = getProviderSettings(payload.source, payload.env, payload.apiKey)
    model = resolveModel(payload.source, payload.model)
    messages = buildMessages(payload)
    gptClient = GPTClient(settings, serverConfig.promptTemplateDir.as_posix(), usageStore, modelCatalog)
    contentStream = gptClient.run(model, messages, payload.options) if payload.testTimes is None else gptClient.test(model, messages, payload.options, payload.testTimes)
    return StreamingResponse(
        content = contentStream,
        media_type = "application/json"
    )


@router_chat.post("/assistant")
async def assistant(payload: ChatRequest):
    settings = getProviderSettings(payload.source, payload.env, payload.apiKey)
    # 先校验来源是否支持 assistant, 未支持直接 501
    assistantClient = AssistantClient(settings, serverConfig.promptTemplateDir.as_posix(), usageStore, modelCatalog)
    messages = buildMessages(payload)
    contentStream = assistantClient.run(payload.code, messages, payload.options) if payload.testTimes is None else assistantClient.test(payload.code, messages, payload.options, payload.testTimes)
    return StreamingResponse(
        content = contentStream,
        media_type = "application/json"
    )

##############################################################################################################################

router_api = APIRouter(
    prefix = "/api",
)


@router_api.get("/meta/models")
async def getMetaModels(refresh: bool = False):
    """
    模型目录 + catalogVersion; 客户端仅在版本变化时刷新下拉
    """
    return modelCatalog.snapshot(refresh = refresh)


@router_api.get("/providers")
async def getProviders():
    """
    provider 列表与配置状态 (密钥脱敏)
    """
    return describeProviders()


@router_api.get("/usage")
async def getUsage(rangeName: str = Query("today", alias = "range")):
    """
    token / 成本计量汇总 (不限额)
    """
    if rangeName not in serverConfig.USAGE_RANGES:
        raise ApiError(
            code = "invalid_range",
            message = f"不支持的统计区间: {rangeName}",
            details = {"range": rangeName, "supported": list(serverConfig.USAGE_RANGES)},
        )
    return usageStore.summary(rangeName)

##############################################################################################################################