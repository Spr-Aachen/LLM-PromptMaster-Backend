# -*- coding: utf-8 -*-

import os
import psutil
import signal
import argparse
import PyEasyUtils as EasyUtils
import uvicorn
from fastapi import status
from fastapi.applications import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.requests import Request
from fastapi.responses import Response, JSONResponse, StreamingResponse
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from pathlib import Path

from routers import router_utils, router_chat

##############################################################################################################################

# Get current path
currentPath = EasyUtils.getCurrentPath()

# Get current directory
currentDir = Path(currentPath).parent.as_posix()

##############################################################################################################################

# 启动参数解析，启动环境，应用端口由命令行传入
parser = argparse.ArgumentParser()
#parser.add_argument("--env",  help = "环境启动项", type = str, default = "prod")
parser.add_argument("--host", help = "主机地址",   type = str, default = "localhost")
parser.add_argument("--port", help = "端口",       type = int, default = 8080)
parser.add_argument("--profileDir", help = "配置目录", type = str, default = Path(currentDir).joinpath('Profile').as_posix())
args = parser.parse_args()

##############################################################################################################################

class PromptTestTool:
    """
    """
    def __init__(self, title, version: str, description: str):
        # Initialize app
        self._app = FastAPI(
            title = title,
            version = version,
            description = description,
        )

        # Include routers
        self._app.include_router(router_utils)
        self._app.include_router(router_chat)

        # Set CORS
        self._app.add_middleware(
            middleware_class = CORSMiddleware,
            allow_origins = ["*"],
            allow_origin_regex = None,
            allow_credentials = True,
            allow_methods = ["*"],
            allow_headers = ["*"],
            expose_headers = ["*"],
            max_age = 600,
        )

        # Sever definition
        self.server = uvicorn.Server(uvicorn.Config(self._app))

        # Setup tools
        self.setExceptionHandler()
        self.setNormalActuator()

    def app(self):
        return self._app

    def setExceptionHandler(self):
        @self._app.exception_handler(StarletteHTTPException)
        async def http_exceptionHandler(request: Request, exc: StarletteHTTPException):
            return JSONResponse(
                status_code = exc.status_code,
                content = jsonable_encoder(
                    {
                        "code": exc.status_code,
                        "message": str(exc.detail),
                        "data": None
                    }
                )
            )

        @self._app.exception_handler(RequestValidationError)
        async def validation_exceptionHandler(request: Request, exc: RequestValidationError):
            return JSONResponse(
                status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
                content = jsonable_encoder(
                    {
                        "code": status.HTTP_422_UNPROCESSABLE_ENTITY,
                        "message": str(exc.errors()),
                        "data": str(exc.body)}
                )
            )

    def setNormalActuator(self):
        @self._app.get("/")
        async def default():
            return "Welcome To prompt Test Service!"

        @self._app.post("/shutdown")
        async def shutdown():
            self.server.should_exit = True
            EasyUtils.terminateProcess(os.getpid())
            #return {"message": "Shutting down, bye..."}

    def run(self):
        uvicorn.run(
            app = self._app,
            host = args.host,
            port = args.port
        )

##############################################################################################################################

if __name__ == "__main__":
    PromptTest = PromptTestTool(
        title = "PromptTestClient Demo",
        version = "1.0.0",
        description = "Just a demo"
    )
    PromptTest.run()

##############################################################################################################################