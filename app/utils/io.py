import os
import aiofiles
from pathlib import Path
from typing import Optional
from uuid import uuid4

from .errors import ApiError

##############################################################################################################################

# 定义写入大小
CHUNK_SIZE = 1024 * 1024


def sanitizeFileName(filename: Optional[str]) -> str:
    """
    提取并校验上传文件名, 拒绝空名、相对路径项与含路径分隔符的名字
    """
    rawName = filename or ""
    fileName = Path(rawName).name
    if (
        fileName in ("", ".", "..")
        or fileName != rawName
        or "/" in rawName
        or "\\" in rawName
        or "\x00" in rawName
    ):
        raise ApiError(
            code = "invalid_filename",
            message = f"非法的文件名：{rawName!r}",
            statusCode = 400,
            details = {"filename": rawName},
        )
    return fileName


async def saveUpload(file, directory, maxBytes: int) -> str:
    """
    以临时文件 + os.replace 原子写入上传文件, 超过上限时拒绝
    """
    fileName = sanitizeFileName(file.filename)
    targetPath = Path(directory).joinpath(fileName)
    tempPath = targetPath.with_name(f".{fileName}.{uuid4().hex}.tmp")
    totalBytes = 0
    try:
        async with aiofiles.open(tempPath, "wb") as outFile:
            while chunk := await file.read(CHUNK_SIZE):
                totalBytes += len(chunk)
                if totalBytes > maxBytes:
                    raise ApiError(
                        code = "payload_too_large",
                        message = f"单个文件不得超过 {maxBytes // (1024 * 1024)} MB",
                        statusCode = 413,
                        details = {"filename": fileName, "maxBytes": maxBytes},
                    )
                await outFile.write(chunk)
        os.replace(tempPath, targetPath)
    finally:
        if tempPath.exists():
            tempPath.unlink()
    return fileName

##############################################################################################################################