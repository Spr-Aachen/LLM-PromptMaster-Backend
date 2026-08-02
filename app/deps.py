"""
路由共享的进程级实例: 会话存储、模型目录与用量计量
"""

from app import config as serverConfig
from app.providers import ModelCatalog, UsageStore
from app.store import ChatManager

##############################################################################################################################

chatManager = ChatManager(
    promptDir = serverConfig.promptDir.as_posix(),
    conversationDir = serverConfig.conversationDir.as_posix(),
    questionDir = serverConfig.questionDir.as_posix()
)

modelCatalog = ModelCatalog()

usageStore = UsageStore(serverConfig.usageDbPath)

##############################################################################################################################