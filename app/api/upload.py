from fastapi.routing import APIRouter
from fastapi.datastructures import UploadFile
from typing import List

from app import config as serverConfig
from app.utils import saveUpload

##############################################################################################################################

router_utils = APIRouter(
    prefix = "",
)


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