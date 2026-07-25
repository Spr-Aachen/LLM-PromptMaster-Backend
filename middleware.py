import json
from starlette.types import ASGIApp, Receive, Scope, Send
from typing import Optional

from utils.errors import errorBody

##############################################################################################################################

class RequestBodyLimitMiddleware:
    """
    限制请求体大小, 超过上限时直接返回统一错误体
    """
    def __init__(self, app: ASGIApp, maxBytes: int):
        self.app = app
        self.maxBytes = maxBytes

    @staticmethod
    def _getContentLength(scope: Scope) -> Optional[int]:
        for name, value in scope.get("headers", []):
            if name.lower() == b"content-length":
                try:
                    return int(value)
                except ValueError:
                    return None
        return None

    async def _reject(self, send: Send):
        body = json.dumps(
            errorBody(
                "payload_too_large",
                f"请求体不得超过 {self.maxBytes // (1024 * 1024)} MB",
                {"maxBytes": self.maxBytes},
            ),
            ensure_ascii = False,
        ).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        })
        await send({"type": "http.response.body", "body": body, "more_body": False})

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        contentLength = self._getContentLength(scope)
        if contentLength is not None and contentLength > self.maxBytes:
            await self._reject(send)
            return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > self.maxBytes:
                await self._reject(send)
                return
            if not message.get("more_body", False):
                break

        replayed = False
        async def replayReceive():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replayReceive, send)

##############################################################################################################################