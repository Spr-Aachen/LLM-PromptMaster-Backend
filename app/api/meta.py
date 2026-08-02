from fastapi.routing import APIRouter
from fastapi import Query

from app import config as serverConfig
from app.deps import modelCatalog, usageStore
from app.utils.errors import ApiError
from app.providers import describeProviders

##############################################################################################################################

router_api = APIRouter(
    prefix = "/api",
)


@router_api.get("/meta/models")
async def getMetaModels(refresh: bool = False):
    """
    模型目录 + catalogVersion; 客户端仅在版本变化时刷新下拉
    """
    return modelCatalog.snapshot(refresh = refresh)


@router_api.get("/providers")
async def getProviders():
    """
    provider 列表与配置状态 (密钥脱敏)
    """
    return describeProviders()


@router_api.get("/usage")
async def getUsage(rangeName: str = Query("today", alias = "range")):
    """
    token / 成本计量汇总 (不限额)
    """
    if rangeName not in serverConfig.USAGE_RANGES:
        raise ApiError(
            code = "invalid_range",
            message = f"不支持的统计区间: {rangeName}",
            details = {"range": rangeName, "supported": list(serverConfig.USAGE_RANGES)},
        )
    return usageStore.summary(rangeName)

##############################################################################################################################