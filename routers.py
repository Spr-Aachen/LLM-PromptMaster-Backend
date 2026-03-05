import PyEasyUtils as EasyUtils
from fastapi.routing import APIRouter
from fastapi.requests import Request
from fastapi.responses import StreamingResponse
from fastapi.datastructures import UploadFile
from fastapi.param_functions import Depends
from pathlib import Path
from typing import Union, Optional, List

import config as serverConfig
from utils import TokenParam, checkToken, write_file, modelsInfo, supportedSources
from utils.errors import ApiError
from gpt import GPTClient
from assistant import AssistantClient
from wrapper import ChatManager

##############################################################################################################################

router_utils = APIRouter(
    prefix = "",
)


def resolveProviderConfig(source: str, env: Optional[str]):
    """
    校验 source/env 并返回（provider 配置文件路径, prompt 模板目录）
    """
    if source not in supportedSources:
        raise ApiError(
            code = "invalid_source",
            message = f"不支持的来源：{source}",
            details = {"source": source, "supported": list(supportedSources)},
        )
    envName = EasyUtils.evalString(env) or None
    configPath = serverConfig.configDir.joinpath(source, f"config-{envName}.ini" if envName is not None else "config.ini")
    if not configPath.is_file():
        raise ApiError(
            code = "invalid_env",
            message = f"来源 {source} 不存在环境配置：{envName}",
            details = {"source": source, "env": envName},
        )
    return configPath.as_posix(), serverConfig.promptTemplateDir.as_posix()


@router_utils.get("/auth", summary = "验证token")
async def auth(token: TokenParam = Depends(checkToken)):
    return {"data": token}


@router_utils.post("/upload")
async def upload_file(files: List[UploadFile]):
    serverConfig.uploadDir.mkdir(parents = True, exist_ok = True)
    for file in files:
        filePath = serverConfig.uploadDir.joinpath(file.filename).as_posix()
        os.remove(filePath) if Path(filePath).exists() else None
        await write_file(filePath, file)
    return {"status": "Succeeded"}

##############################################################################################################################

router_chat = APIRouter(
    prefix = "/chat",
)


chatManager = ChatManager(
    promptDir = serverConfig.promptDir.as_posix(),
    conversationDir = serverConfig.conversationDir.as_posix(),
    questionDir = serverConfig.questionDir.as_posix()
)


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
async def createPrompt(name: str):
    promptID, promptName = chatManager.createPrompt(name)
    return promptID, promptName


@router_chat.post("/renamePrompt")
async def renamePrompt(promptID, newName):
    chatManager.renamePrompt(promptID, newName)



@router_chat.post("/deletePrompt")
async def deletePrompt(promptID):
    chatManager.deletePrompt(promptID)


@router_chat.post("/savePrompt")
async def savePrompt(promptID, prompt):
    chatManager.savePrompt(promptID, prompt)


@router_chat.get("/loadHistories")
async def loadHistories():
    histories = chatManager.loadHistories()
    return histories


@router_chat.get("/getHistory")
async def getHistory(historyID):
    messages, question = chatManager.getHistory(historyID)
    return messages, question


@router_chat.post("/createConversation")
async def createConversation(name):
    historyID, conversationName = chatManager.createConversation(name)
    return historyID, conversationName


@router_chat.post("/renameConversation")
async def renameConversation(historyID, newName):
    chatManager.renameConversation(historyID, newName)


@router_chat.post("/deleteConversation")
async def deleteConversation(historyID):
    chatManager.deleteConversation(historyID)


# @router_chat.post("/saveConversation")
# async def saveConversation(historyID, messages):
#     chatManager.saveConversation(historyID, messages)


@router_chat.post("/saveQuestion")
async def saveQuestion(historyID, question):
    chatManager.saveQuestion(historyID, question)


@router_chat.post("/applyPrompt")
async def applyPrompt(promptID):
    chatManager.applyPrompt(promptID)


@router_chat.post("/addUserMessage")
async def addUserMessage(historyID, userMessage):
    chatManager.addUserMessage(historyID, eval(userMessage))


@router_chat.post("/recieveAnswer")
async def recieveAnswer(historyID, recievedText):
    messages = chatManager.recieveAnswer(historyID, recievedText)
    return messages


@router_chat.post("/gpt")
async def gpt(request: Request, historyID: str, source: str, env: Optional[str] = None, model: Optional[str] = None, apiKey: Optional[str] = None, testTimes: Optional[Union[int, str]] = None):
    configPath, promptDir = resolveProviderConfig(source, env)
    reqJs: dict = await request.json()
    message = reqJs.get('message', None)
    options = reqJs.get('options', None)
    messages = chatManager.getHistory(historyID)[0] + [message]
    gptClient = GPTClient(source, EasyUtils.evalString(apiKey), configPath, promptDir)
    contentStream = gptClient.run(EasyUtils.evalString(model), messages, options) if EasyUtils.evalString(testTimes) is None else gptClient.test(EasyUtils.evalString(model), messages, options, int(testTimes))
    return StreamingResponse(
        content = contentStream,
        media_type = "application/json"
    )


@router_chat.post("/assistant")
async def assistant(request: Request, historyID: str, source: str, env: Optional[str] = None, code: Optional[str] = None, apiKey: Optional[str] = None, testTimes: Optional[Union[int, str]] = None):
    configPath, promptDir = resolveProviderConfig(source, env)
    reqJs: dict = await request.json()
    message = reqJs.get('message', None)
    options = reqJs.get('options', None)
    messages = chatManager.getHistory(historyID)[0] + [message]
    assistantClient = AssistantClient(source, EasyUtils.evalString(apiKey), configPath, promptDir)
    contentStream = assistantClient.run(EasyUtils.evalString(code), messages, options) if EasyUtils.evalString(testTimes) is None else assistantClient.test(EasyUtils.evalString(code), messages, options, int(testTimes))
    return StreamingResponse(
        content = contentStream,
        media_type = "application/json"
    )