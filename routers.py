from fastapi.routing import APIRouter
from fastapi.responses import StreamingResponse
from fastapi.datastructures import UploadFile
from typing import List, Optional

import config as serverConfig
from utils import ASSISTANT_HANDLERS, modelsInfo, saveUpload, supportedSources
from utils.errors import ApiError
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


def resolveProviderConfig(source: str, env: Optional[str]):
    """
    校验 source/env 并返回 (provider 配置文件路径, prompt 模板目录)
    """
    if source not in supportedSources:
        raise ApiError(
            code = "invalid_source",
            message = f"不支持的来源: {source}",
            details = {"source": source, "supported": list(supportedSources)},
        )
    envName = normalizeOptional(env)
    sourceDir = serverConfig.configDir.joinpath(source)
    if envName is None:
        configPath = sourceDir.joinpath("config.ini")
    else:
        allowedEnvs = sorted(path.stem.split("-", 1)[-1] for path in sourceDir.glob("config-*.ini"))
        if envName not in allowedEnvs:
            raise ApiError(
                code = "invalid_env",
                message = f"来源 {source} 不存在环境配置: {envName}",
                details = {"source": source, "env": envName, "supported": allowedEnvs},
            )
        configPath = sourceDir.joinpath(f"config-{envName}.ini")
    if not configPath.is_file():
        raise ApiError(
            code = "invalid_env",
            message = f"来源 {source} 缺少配置文件: {configPath.name}",
            details = {"source": source, "env": envName},
        )
    return configPath.as_posix(), serverConfig.promptTemplateDir.as_posix()


def validateModel(source: str, model: Optional[str]) -> Optional[str]:
    """
    校验 model 白名单
    """
    modelName = normalizeOptional(model)
    if modelName is None:
        return None
    supportedModels = modelsInfo.get(source, [])
    if modelName not in supportedModels:
        raise ApiError(
            code = "invalid_model",
            message = f"来源 {source} 不支持模型: {modelName}",
            details = {"source": source, "model": modelName, "supported": supportedModels},
        )
    return modelName


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


@router_chat.get("/getCapabilities")
async def getCapabilities():
    return {
        "sources": list(supportedSources),
        "assistantSources": list(ASSISTANT_HANDLERS),
    }


@router_chat.get("/getModelsInfo")
async def getModelsInfo():
    return modelsInfo


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
    configPath, promptDir = resolveProviderConfig(payload.source, payload.env)
    model = validateModel(payload.source, payload.model)
    messages = chatManager.getHistory(payload.historyID)[0] + [payload.message]
    gptClient = GPTClient(payload.source, normalizeOptional(payload.apiKey), configPath, promptDir)
    contentStream = gptClient.run(model, messages, payload.options) if payload.testTimes is None else gptClient.test(model, messages, payload.options, payload.testTimes)
    return StreamingResponse(
        content = contentStream,
        media_type = "application/json"
    )


@router_chat.post("/assistant")
async def assistant(payload: ChatRequest):
    configPath, promptDir = resolveProviderConfig(payload.source, payload.env)
    if payload.source not in ASSISTANT_HANDLERS:
        raise ApiError(
            code = "assistant_unsupported",
            message = f"来源 {payload.source} 暂不支持 assistant",
            statusCode = 501,
            details = {"source": payload.source, "supported": list(ASSISTANT_HANDLERS)},
        )
    messages = chatManager.getHistory(payload.historyID)[0] + [payload.message]
    assistantClient = AssistantClient(payload.source, normalizeOptional(payload.apiKey), configPath, promptDir)
    assistantCode = normalizeOptional(payload.code)
    contentStream = assistantClient.run(assistantCode, messages, payload.options) if payload.testTimes is None else assistantClient.test(assistantCode, messages, payload.options, payload.testTimes)
    return StreamingResponse(
        content = contentStream,
        media_type = "application/json"
    )