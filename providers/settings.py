"""
Provider 配置: 优先读取 data/server/providers.yaml, 兼容旧版 server/config/<source>/config.ini 一个版本周期
"""

import logging
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from PyEasyUtils import configManager
from typing import Any, Optional


import config as serverConfig
from providers.base import ErrorCode, ProviderError

##############################################################################################################################

logger = logging.getLogger("PromptMaster.providers.settings")

PROVIDERS_FILE_VERSION = 1

# 旧版配置中表示「未填写」的占位符
PLACEHOLDER_MARKERS = ("<", "your-", "your_", "changeme", "todo")

# 已提示过迁移的 source, 避免刷屏
_migrationNotified = set()

# providers.yaml 的 mtime 缓存
_fileCache = {"mtime": None, "data": {}}


@dataclass
class ProviderSettings:
    """
    单个 source(+env) 的解析结果
    """
    source: str
    kind: str
    env: Optional[str] = None
    baseUrl: Optional[str] = None
    apiKey: Optional[str] = None
    apiVersion: Optional[str] = None
    appID: Optional[str] = None
    appSecret: Optional[str] = None
    pfGateway: Optional[str] = None
    gptGateway: Optional[str] = None
    assistant: dict = field(default_factory = dict)
    configPath: Optional[str] = None
    origin: str = "legacy"

    @property
    def isConfigured(self) -> bool:
        if self.kind == "transsion":
            return bool(self.pfGateway and self.appID and self.appSecret)
        return bool(self.baseUrl and self.apiKey)


##############################################################################################################################

def isPlaceholder(value: Any) -> bool:
    """
    判断配置值是否为占位符或空值
    """
    if not isinstance(value, str):
        return not value
    text = value.strip()
    if not text:
        return True
    lowered = text.lower()
    return any(marker in lowered for marker in PLACEHOLDER_MARKERS)


def maskSecret(value: Optional[str]) -> str:
    """
    脱敏展示密钥: 仅保留末 4 位
    """
    if not value:
        return ""
    text = str(value)
    if len(text) <= 8:
        return "****"
    return f"****{text[-4:]}"


##############################################################################################################################

def readProvidersFile(force: bool = False) -> dict:
    """
    读取 providers.yaml, 按 mtime 缓存; 文件缺失或非法时返回空字典
    """
    path = serverConfig.providersFilePath
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _fileCache["mtime"] = None
        _fileCache["data"] = {}
        return {}
    if not force and _fileCache["mtime"] == mtime:
        return _fileCache["data"]
    try:
        with open(path, "r", encoding = "utf-8") as file:
            data = yaml.safe_load(file) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.error("读取 %s 失败: %s", path.as_posix(), exc)
        data = {}
    if not isinstance(data, dict):
        logger.error("%s 顶层必须是映射, 已忽略", path.as_posix())
        data = {}
    _fileCache["mtime"] = mtime
    _fileCache["data"] = data
    return data


def writeProvidersFile(data: dict) -> Path:
    """
    写入 providers.yaml (真实密钥文件, 位于忽略的 data/ 下)
    """
    path = serverConfig.providersFilePath
    path.parent.mkdir(parents = True, exist_ok = True)
    with open(path, "w", encoding = "utf-8") as file:
        yaml.safe_dump(data, file, allow_unicode = True, sort_keys = False, default_flow_style = False)
    _fileCache["mtime"] = None
    return path


##############################################################################################################################

def legacyConfigPath(source: str, env: Optional[str] = None) -> Path:
    """
    旧版 provider 配置文件路径
    """
    sourceDir = serverConfig.configDir.joinpath(source)
    if env:
        return sourceDir.joinpath(f"config-{env}.ini")
    return sourceDir.joinpath("config.ini")


def availableEnvs(source: str) -> list:
    """
    列出 source 可用的环境名: yaml 的 envs 优先, 其次旧版 config-*.ini
    """
    node = readProvidersFile().get("providers", {}).get(source)
    if isinstance(node, dict):
        envs = node.get("envs")
        return sorted(envs) if isinstance(envs, dict) else []
    sourceDir = serverConfig.configDir.joinpath(source)
    if not sourceDir.is_dir():
        return []
    return sorted(path.stem.split("-", 1)[-1] for path in sourceDir.glob("config-*.ini"))


def _legacyValue(manager: configManager, section: str, option: str) -> Optional[str]:
    try:
        value = manager.getValue(section, option, None)
    except Exception:
        return None
    return None if isPlaceholder(value) else str(value).strip()


def _fromLegacy(source: str, env: Optional[str]) -> ProviderSettings:
    """
    从旧版 config.ini 读取 provider 设置
    """
    path = legacyConfigPath(source, env)
    if not path.is_file():
        raise ProviderError(
            code = ErrorCode.invalidEnv,
            message = f"来源 {source} 缺少配置文件: {path.name}",
            statusCode = 400,
            details = {"source": source, "env": env, "path": path.as_posix()},
        )
    manager = configManager(path.as_posix())
    settings = ProviderSettings(
        source = source,
        kind = source,
        env = env,
        configPath = path.as_posix(),
        origin = "legacy",
    )
    if source == "transsion":
        settings.gptGateway = _legacyValue(manager, "Auth", "gptGateway")
        settings.pfGateway = _legacyValue(manager, "Auth", "pfGateway")
        settings.appID = _legacyValue(manager, "Auth", "appID")
        settings.appSecret = _legacyValue(manager, "Auth", "appSecret")
        settings.assistant = {
            "xHeaderTenant": _legacyValue(manager, "Chat-Assistant", "xHeaderTenant"),
            "chatURL": _legacyValue(manager, "Chat-Assistant", "chatURL"),
            "assistantCode": _legacyValue(manager, "Chat-Assistant", "assistantCode"),
        }
    else:
        settings.baseUrl = _legacyValue(manager, "Auth", "gateway")
        settings.apiKey = _legacyValue(manager, "Auth", "apiKey")
    if source == "azure":
        settings.apiVersion = serverConfig.DEFAULT_AZURE_API_VERSION
    return settings


def _fromYaml(source: str, node: dict, env: Optional[str]) -> ProviderSettings:
    """
    从 providers.yaml 的 provider 节点读取设置, env 覆盖同名键
    """
    merged = {key: value for key, value in node.items() if key not in ("envs", "assistant")}
    assistant = dict(node.get("assistant") or {})
    envs = node.get("envs")
    if env:
        if not isinstance(envs, dict) or env not in envs:
            raise ProviderError(
                code = ErrorCode.invalidEnv,
                message = f"来源 {source} 不存在环境配置: {env}",
                statusCode = 400,
                details = {"source": source, "env": env, "supported": sorted(envs) if isinstance(envs, dict) else []},
            )
        override = envs.get(env) or {}
        merged.update({key: value for key, value in override.items() if key != "assistant"})
        assistant.update(override.get("assistant") or {})

    def pick(key: str) -> Optional[str]:
        value = merged.get(key)
        return None if isPlaceholder(value) else str(value).strip()

    settings = ProviderSettings(
        source = source,
        kind = source,
        env = env,
        baseUrl = pick("baseUrl"),
        apiKey = pick("apiKey"),
        apiVersion = pick("apiVersion") or (serverConfig.DEFAULT_AZURE_API_VERSION if source == "azure" else None),
        appID = pick("appID"),
        appSecret = pick("appSecret"),
        pfGateway = pick("pfGateway"),
        gptGateway = pick("gptGateway"),
        assistant = {key: value for key, value in assistant.items() if not isPlaceholder(value)},
        origin = "yaml",
    )
    legacyPath = legacyConfigPath(source, env)
    settings.configPath = legacyPath.as_posix() if legacyPath.is_file() else None
    return settings


def getProviderSettings(source: str, env: Optional[str] = None, apiKey: Optional[str] = None) -> ProviderSettings:
    """
    解析 source(+env) 的 provider 设置: yaml 优先, 否则旧版 config.ini
    """
    if source not in serverConfig.SUPPORTED_SOURCES:
        raise ProviderError(
            code = ErrorCode.invalidSource,
            message = f"不支持的来源: {source}",
            statusCode = 400,
            details = {"source": source, "supported": list(serverConfig.SUPPORTED_SOURCES)},
        )
    envName = (env or "").strip() or None
    if envName == "None":
        envName = None

    node = readProvidersFile().get("providers", {}).get(source)
    if isinstance(node, dict):
        settings = _fromYaml(source, node, envName)
    else:
        allowedEnvs = availableEnvs(source)
        if envName is not None and envName not in allowedEnvs:
            raise ProviderError(
                code = ErrorCode.invalidEnv,
                message = f"来源 {source} 不存在环境配置: {envName}",
                statusCode = 400,
                details = {"source": source, "env": envName, "supported": allowedEnvs},
            )
        settings = _fromLegacy(source, envName)
        if settings.isConfigured and source not in _migrationNotified:
            _migrationNotified.add(source)
            logger.warning(
                "来源 %s 仍在使用旧版 config/%s/config.ini 中的密钥, 请迁移到 %s",
                source, source, serverConfig.providersFilePath.as_posix(),
            )

    if apiKey and not isPlaceholder(apiKey):
        settings.apiKey = apiKey.strip()

    if not settings.isConfigured:
        raise ProviderError(
            code = ErrorCode.providerNotConfigured,
            message = f"来源 {source} 缺少网关或密钥配置, 请在 {serverConfig.providersFilePath.as_posix()} 中填写",
            statusCode = 400,
            details = {"source": source, "env": settings.env, "origin": settings.origin},
        )
    return settings


def discoveryTargets() -> list:
    """
    支持 GET /models 动态发现的 provider (当前为 openai / deepseek)
    """
    targets = []
    for source in ("openai", "deepseek"):
        try:
            settings = getProviderSettings(source)
        except ProviderError:
            continue
        if settings.baseUrl and settings.apiKey:
            targets.append({"source": source, "baseUrl": settings.baseUrl, "apiKey": settings.apiKey})
    return targets


def describeProviders() -> list:
    """
    供客户端展示的 provider 列表 (密钥脱敏)
    """
    result = []
    for source in serverConfig.SUPPORTED_SOURCES:
        entry = {
            "id": source,
            "label": serverConfig.SOURCE_LABELS.get(source, source),
            "configured": False,
            "origin": "yaml" if isinstance(readProvidersFile().get("providers", {}).get(source), dict) else "legacy",
            "envs": availableEnvs(source),
            "secret": "",
        }
        try:
            settings = getProviderSettings(source)
        except ProviderError:
            settings = None
            # 分环境配置需要显式 env, 逐个尝试以判断是否可用
            for env in entry["envs"]:
                try:
                    settings = getProviderSettings(source, env)
                    break
                except ProviderError:
                    continue
        if settings is None:
            result.append(entry)
            continue
        entry["configured"] = True
        entry["origin"] = settings.origin
        entry["secret"] = maskSecret(settings.apiKey or settings.appSecret)
        result.append(entry)
    return result


##############################################################################################################################

def ensureProvidersFile() -> Optional[Path]:
    """
    首次启动时把旧版 config.ini 中的真实密钥迁移到 data/server/providers.yaml
    """
    path = serverConfig.providersFilePath
    if path.exists():
        return None
    providers = {}
    for source in serverConfig.SUPPORTED_SOURCES:
        node = {}
        if source == "transsion":
            envs = {}
            for env in availableEnvs(source):
                legacy = _fromLegacy(source, env)
                if not legacy.isConfigured:
                    continue
                envs[env] = {
                    "gptGateway": legacy.gptGateway,
                    "pfGateway": legacy.pfGateway,
                    "appID": legacy.appID,
                    "appSecret": legacy.appSecret,
                    "assistant": legacy.assistant,
                }
            if envs:
                node["envs"] = envs
        else:
            legacyPath = legacyConfigPath(source)
            if legacyPath.is_file():
                legacy = _fromLegacy(source, None)
                if legacy.isConfigured:
                    node = {"baseUrl": legacy.baseUrl, "apiKey": legacy.apiKey}
                    if legacy.apiVersion:
                        node["apiVersion"] = legacy.apiVersion
        if node:
            providers[source] = node
    if not providers:
        return None
    writeProvidersFile({"version": PROVIDERS_FILE_VERSION, "providers": providers})
    logger.info("已从旧版 config/*.ini 生成 %s, 请核对后删除旧文件中的密钥", path.as_posix())
    return path

##############################################################################################################################