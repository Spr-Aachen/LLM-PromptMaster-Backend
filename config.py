import argparse
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

##############################################################################################################################

APP_NAME = "PromptMaster"

VERSION = "1.0.0"

DATA_DIR_NAME = "data"

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

LOG_DATEFORMAT = "%Y-%m-%d %H:%M:%S"

LOG_MAX_BYTES = 5 * 1024 * 1024

LOG_BACKUP_COUNT = 3

LOG_LEVELS = ("debug", "info", "warning", "error", "critical")

##############################################################################################################################

# 源码目录 (server/) 与项目根目录
appDir = Path(__file__).resolve().parent
sourceDir = appDir.parent

# 是否由 PyInstaller 打包运行
isFrozen = bool(getattr(sys, "frozen", False))

# 可执行文件所在目录
exeDir = Path(sys.executable).resolve().parent if isFrozen else sourceDir

# 静态资源目录 (兼容 PyInstaller 的解包目录)
resourceDir = Path(getattr(sys, "_MEIPASS", exeDir)) if isFrozen else sourceDir

##############################################################################################################################

def parseArguments(argv: Optional[list] = None) -> argparse.Namespace:
    """
    解析启动参数, 仅在模块加载时执行一次
    """
    parser = argparse.ArgumentParser(description = "LLM PromptMaster 服务端")
    parser.add_argument("--host", help = "监听地址", type = str, default = "127.0.0.1")
    parser.add_argument("--port", help = "监听端口", type = int, default = 8080)
    parser.add_argument("--work-dir", dest = "workDir", help = "数据目录 (缺省为数据根目录下的 server/)", type = str, default = None)
    parser.add_argument("--log-level", dest = "logLevel", help = "日志级别", type = str, default = "info", choices = LOG_LEVELS)
    return parser.parse_args(argv)


args = parseArguments()


def resolveDataRoot() -> Path:
    """
    解析数据根目录：打包时优先便携 <exe>/data, 否则用户数据目录; 源码运行时固定 <repo>/data
    """
    if not isFrozen:
        return sourceDir.joinpath(DATA_DIR_NAME)
    portableRoot = exeDir.joinpath(DATA_DIR_NAME)
    if portableRoot.is_dir():
        return portableRoot
    appDataDir = os.environ.get("APPDATA")
    userDir = Path(appDataDir) if appDataDir else Path.home().joinpath(".local", "share")
    return userDir.joinpath(APP_NAME)


def resolveWorkDir(explicit: Optional[str] = None) -> Path:
    """
    解析工作目录：显式参数即服务端数据目录, 否则为数据根目录下的 server/
    """
    return Path(explicit).expanduser().resolve() if explicit else resolveDataRoot().joinpath("server")


host = args.host
port = args.port
logLevel = args.logLevel
workDir = resolveWorkDir(args.workDir)

# 数据目录
promptDir = workDir.joinpath("prompts")
historyDir = workDir.joinpath("history")
conversationDir = historyDir.joinpath("conversations")
questionDir = historyDir.joinpath("questions")
uploadDir = workDir.joinpath("uploads")

# 日志目录
logDir = workDir.joinpath("logs")
logFile = logDir.joinpath("server.log")

# 随源码分发的资源目录
configDir = appDir.joinpath("config")
promptTemplateDir = appDir.joinpath("prompt")

##############################################################################################################################

def ensureDirs():
    """
    创建工作目录及其派生目录
    """
    for dir in (workDir, promptDir, conversationDir, questionDir, uploadDir, logDir):
        dir.mkdir(parents = True, exist_ok = True)


def setupLogging(level: Optional[str] = None) -> logging.Logger:
    """
    配置日志：控制台 + 轮转文件 (logs/server.log)
    """
    ensureDirs()
    logger = logging.getLogger()
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.setLevel(getattr(logging, (level or logLevel).upper(), logging.INFO))
    formatter = logging.Formatter(LOG_FORMAT, LOG_DATEFORMAT)
    consoleHandler = logging.StreamHandler()
    consoleHandler.setFormatter(formatter)
    fileHandler = RotatingFileHandler(
        logFile,
        maxBytes = LOG_MAX_BYTES,
        backupCount = LOG_BACKUP_COUNT,
        encoding = "utf-8",
    )
    fileHandler.setFormatter(formatter)
    logger.addHandler(consoleHandler)
    logger.addHandler(fileHandler)
    return logger

##############################################################################################################################