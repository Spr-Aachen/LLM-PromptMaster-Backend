# -*- coding: utf-8 -*-

import logging
import os
import threading
import time
import uvicorn
from fastapi import status
from fastapi.applications import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.requests import Request
from starlette.exceptions import HTTPException as StarletteHTTPException
from typing import Optional

import config as serverConfig
from middleware import RequestBodyLimitMiddleware
from providers.settings import ensureProvidersFile
from routers import router_api, router_chat, router_utils
from utils.errors import ApiError, errorResponse

##############################################################################################################################

logger = logging.getLogger(f"{serverConfig.APP_NAME}.server")

# 关闭请求收到后先写回响应, 再通知 uvicorn 退出
SHUTDOWN_DELAY = 0.2

HTTP_ERROR_CODES = {
    status.HTTP_400_BAD_REQUEST: "bad_request",
    status.HTTP_401_UNAUTHORIZED: "unauthorized",
    status.HTTP_403_FORBIDDEN: "forbidden",
    status.HTTP_404_NOT_FOUND: "not_found",
    status.HTTP_405_METHOD_NOT_ALLOWED: "method_not_allowed",
    status.HTTP_409_CONFLICT: "conflict",
    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE: "payload_too_large",
    status.HTTP_422_UNPROCESSABLE_ENTITY: "validation_error",
    status.HTTP_500_INTERNAL_SERVER_ERROR: "internal_error",
    status.HTTP_501_NOT_IMPLEMENTED: "not_implemented",
    status.HTTP_503_SERVICE_UNAVAILABLE: "service_unavailable",
}

##############################################################################################################################

def setExceptionHandler(app: FastAPI):
    """
    所有错误统一返回 {error: {code, message, details}}
    """
    @app.exception_handler(ApiError)
    async def apiErrorHandler(request: Request, exc: ApiError):
        if exc.statusCode >= status.HTTP_500_INTERNAL_SERVER_ERROR:
            logger.error("%s %s -> %s: %s", request.method, request.url.path, exc.code, exc.message)
        else:
            logger.warning("%s %s -> %s: %s", request.method, request.url.path, exc.code, exc.message)
        return errorResponse(exc.statusCode, exc.code, exc.message, exc.details)

    @app.exception_handler(StarletteHTTPException)
    async def httpErrorHandler(request: Request, exc: StarletteHTTPException):
        code = HTTP_ERROR_CODES.get(exc.status_code, "http_error")
        if exc.status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR:
            logger.error("%s %s -> %s: %s", request.method, request.url.path, code, exc.detail)
        return errorResponse(exc.status_code, code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def validationErrorHandler(request: Request, exc: RequestValidationError):
        try:
            details = jsonable_encoder(exc.errors())
        except Exception:
            details = str(exc.errors())
        logger.warning("%s %s -> validation_error", request.method, request.url.path)
        return errorResponse(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "validation_error",
            "请求参数校验失败",
            details,
        )

    @app.exception_handler(Exception)
    async def unhandledErrorHandler(request: Request, exc: Exception):
        logger.exception("%s %s -> unhandled error", request.method, request.url.path)
        return errorResponse(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "internal_error",
            "服务端内部错误",
        )


def requestExit(server: uvicorn.Server):
    """
    等待响应写出后再通知 uvicorn 退出, 避免关闭请求被提前中断
    """
    time.sleep(SHUTDOWN_DELAY)
    server.should_exit = True


def setNormalActuator(app: FastAPI):
    @app.get("/", summary = "服务信息")
    async def default():
        return "Welcome To prompt Test Service!"

    @app.get("/health", summary = "健康检查")
    async def health():
        return {
            "status": "ok",
            "version": serverConfig.VERSION,
            "pid": os.getpid(),
            "workDir": serverConfig.workDir.as_posix(),
        }

    @app.post("/shutdown", summary = "关闭服务")
    async def shutdown(request: Request):
        server = getattr(request.app.state, "server", None)
        if server is None:
            return errorResponse(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "server_unavailable",
                "服务端未以可关闭模式运行",
            )
        logger.info("收到关闭请求，%.1fs 后退出", SHUTDOWN_DELAY)
        threading.Thread(target = requestExit, args = (server,), daemon = True).start()
        return {"status": "shutting_down"}

##############################################################################################################################

def createApp() -> FastAPI:
    serverConfig.ensureDirs()
    ensureProvidersFile()
    app = FastAPI(
        title = f"{serverConfig.APP_NAME} Server",
        version = serverConfig.VERSION,
        description = "LLM PromptMaster 本地服务端",
    )
    app.include_router(router_utils)
    app.include_router(router_chat)
    app.include_router(router_api)
    setExceptionHandler(app)
    setNormalActuator(app)
    app.add_middleware(RequestBodyLimitMiddleware, maxBytes = serverConfig.maxRequestBodyBytes)
    return app


def createServer(app: Optional[FastAPI] = None) -> uvicorn.Server:
    app = createApp() if app is None else app
    uvicornConfig = uvicorn.Config(
        app,
        host = serverConfig.host,
        port = serverConfig.port,
        log_config = None, # 复用 config.setupLogging 配置的日志
        timeout_graceful_shutdown = 5,
    )
    server = uvicorn.Server(uvicornConfig)
    app.state.server = server
    return server


def runServer():
    server = createServer()
    logger.info("工作目录: %s", serverConfig.workDir.as_posix())
    logger.info("日志文件: %s", serverConfig.logFile.as_posix())
    logger.info("监听地址: http://%s:%s", serverConfig.host, serverConfig.port)
    server.run()
    logger.info("服务已退出")

##############################################################################################################################

if __name__ == "__main__":
    serverConfig.setupLogging()
    runServer()

##############################################################################################################################