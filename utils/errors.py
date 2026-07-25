from typing import Any, Optional

from fastapi.responses import JSONResponse

##############################################################################################################################

class ApiError(Exception):
    """
    业务错误, 由 main.py 的异常处理器统一转成错误体
    """
    def __init__(self, code: str, message: str, statusCode: int = 400, details: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.statusCode = statusCode
        self.details = details


def errorBody(code: str, message: str, details: Any = None) -> dict:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details,
        }
    }


def errorResponse(statusCode: int, code: str, message: str, details: Any = None) -> JSONResponse:
    return JSONResponse(
        status_code = statusCode,
        content = errorBody(code, message, details),
    )

##############################################################################################################################