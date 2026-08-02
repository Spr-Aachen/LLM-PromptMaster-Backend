"""
模型目录: 内置目录 + data/server/providers.yaml 覆盖 + provider /models 动态发现 (TTL 缓存)
"""

import copy
import hashlib
import json
import logging
import threading
import time
import requests
from typing import Optional

from app import config as serverConfig
from app.providers.base import loadsJson

##############################################################################################################################

logger = logging.getLogger("PromptMaster.providers.catalog")


def model(
    provider: str,
    id: str,
    label: str,
    contextWindow: Optional[int] = None,
    maxOutput: Optional[int] = None,
    stream: bool = True,
    vision: bool = False,
    embedding: bool = False,
    pricing: Optional[dict] = None,
    kind: str = "chat",
) -> dict:
    """
    构造一条目录项
    """
    return {
        "id": id,
        "label": label,
        "provider": provider,
        "contextWindow": contextWindow,
        "maxOutput": maxOutput,
        "stream": stream,
        "vision": vision,
        "embedding": embedding,
        "pricing": pricing,
        "kind": kind,
        "source": "builtin",
        "unlisted": False,
    }


def _pricing(inputPrice: float, outputPrice: float) -> dict:
    """
    每 1M token 的美元单价
    """
    return {"input": inputPrice, "output": outputPrice}


BUILTIN_MODELS = {
    "azure": [
        model("azure", "gpt-35-turbo", "GPT-3.5 Turbo", 16385, 4096, pricing = _pricing(0.5, 1.5)),
        model("azure", "gpt-4o", "GPT-4o", 128000, 16384, vision = True, pricing = _pricing(2.5, 10.0)),
        model("azure", "dall-e2", "DALL·E 2", stream = False, kind = "image"),
        model("azure", "dall-e3", "DALL·E 3", stream = False, kind = "image"),
    ],
    "deepseek": [
        model("deepseek", "deepseek-chat", "DeepSeek Chat", 128000, 8192, pricing = _pricing(0.27, 1.1)),
        model("deepseek", "deepseek-reasoner", "DeepSeek Reasoner", 128000, 8192, pricing = _pricing(0.55, 2.19)),
    ],
    "openai": [
        model("openai", "gpt-35-turbo", "GPT-3.5 Turbo", 16385, 4096, pricing = _pricing(0.5, 1.5)),
        model("openai", "gpt-4o", "GPT-4o", 128000, 16384, vision = True, pricing = _pricing(2.5, 10.0)),
        model("openai", "dall-e2", "DALL·E 2", stream = False, kind = "image"),
        model("openai", "dall-e3", "DALL·E 3", stream = False, kind = "image"),
    ],
    "transsion": [
        model("transsion", "gpt-35-turbo", "GPT-3.5 Turbo", 16385, 4096),
        model("transsion", "gpt-4o", "GPT-4o", 128000, 16384, vision = True),
        model("transsion", "gemini-1.5-pro-001", "Gemini 1.5 Pro", 2097152, 8192, vision = True),
        model("transsion", "moonshot-v1-128k", "Moonshot v1 128K", 131072, 4096),
        model("transsion", "claude-3-5-sonnet@20240620", "Claude 3.5 Sonnet", 200000, 8192, vision = True),
        model("transsion", "dall-e2", "DALL·E 2", stream = False, kind = "image"),
        model("transsion", "dall-e3", "DALL·E 3", stream = False, kind = "image"),
    ],
}

# 支持 GET /models 动态发现的 provider
DISCOVERY_SOURCES = ("openai", "deepseek")


class ModelCatalog:
    """
    目录快照按 TTL 重建; 发现失败时保留上一份发现结果, 首次失败回退内置目录
    """
    def __init__(self, discovery: bool = True, ttl: int = serverConfig.CATALOG_CACHE_SECONDS):
        self.discoveryEnabled = discovery
        self.ttl = ttl
        self._lock = threading.RLock()
        self._models = {}
        self._version = ""
        self._loadedAt = 0.0
        self._discovered = {}

    ######################################################################

    def ensureFresh(self) -> None:
        with self._lock:
            if self._models and (time.monotonic() - self._loadedAt) < self.ttl:
                return
            self.refresh()

    def refresh(self) -> None:
        """
        重建目录: 内置 → yaml 覆盖 → 动态发现
        """
        with self._lock:
            models = copy.deepcopy(BUILTIN_MODELS)
            self.applyOverrides(models)
            self.applyDiscovered(models)
            if self.discoveryEnabled:
                self.discover(models)
            self._models = {
                provider: {entry["id"]: entry for entry in entries}
                for provider, entries in models.items()
            }
            self._version = self.computeVersion(self._models)
            self._loadedAt = time.monotonic()

    def applyOverrides(self, models: dict) -> None:
        from app.providers.settings import readProvidersFile

        overrides = readProvidersFile().get("models")
        if not isinstance(overrides, list):
            return
        for raw in overrides:
            if not isinstance(raw, dict) or not raw.get("id") or not raw.get("provider"):
                continue
            provider = str(raw["provider"])
            if provider not in models:
                models[provider] = []
            entry = model(
                provider = provider,
                id = str(raw["id"]),
                label = str(raw.get("label") or raw["id"]),
                contextWindow = raw.get("contextWindow"),
                maxOutput = raw.get("maxOutput"),
                stream = bool(raw.get("stream", True)),
                vision = bool(raw.get("vision", False)),
                embedding = bool(raw.get("embedding", False)),
                pricing = raw.get("pricing") if isinstance(raw.get("pricing"), dict) else None,
                kind = str(raw.get("kind") or "chat"),
            )
            entry["source"] = "override"
            models[provider] = [item for item in models[provider] if item["id"] != entry["id"]]
            models[provider].append(entry)

    def applyDiscovered(self, models: dict) -> None:
        """
        回填上一次成功发现的结果, 保证发现失败时目录不退化
        """
        for provider, entries in self._discovered.items():
            models.setdefault(provider, [])
            known = {item["id"] for item in models[provider]}
            for entry in entries:
                if entry["id"] not in known:
                    models[provider].append(copy.deepcopy(entry))

    def discover(self, models: dict) -> None:
        """
        拉取 provider /models, 失败时保留旧结果并记录日志
        """
        from app.providers.settings import discoveryTargets

        for target in discoveryTargets():
            provider = target["source"]
            entries = self.fetchModels(target)
            if entries is None:
                logger.info("来源 %s 的 /models 不可达, 继续使用现有目录", provider)
                continue
            self._discovered[provider] = entries
            models.setdefault(provider, [])
            known = {item["id"] for item in models[provider]}
            for entry in entries:
                if entry["id"] not in known:
                    models[provider].append(copy.deepcopy(entry))

    def fetchModels(self, target: dict) -> Optional[list]:
        url = f"{target['baseUrl'].rstrip('/')}/models"
        headers = {"Authorization": f"Bearer {target['apiKey']}", "Accept": "application/json"}
        try:
            response = requests.get(
                url = url,
                headers = headers,
                timeout = (serverConfig.DISCOVERY_TIMEOUT_SECONDS, serverConfig.DISCOVERY_TIMEOUT_SECONDS),
            )
        except requests.RequestException as exc:
            logger.info("发现 %s 模型失败: %s", target["source"], exc)
            return None
        if response.status_code != 200:
            logger.info("发现 %s 模型失败, 状态码 %s", target["source"], response.status_code)
            return None
        payload = loadsJson(response.text)
        items = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            logger.info("来源 %s 的 /models 响应格式不符合预期", target["source"])
            return None
        entries = []
        for item in items:
            modelId = item.get("id") if isinstance(item, dict) else None
            if not modelId:
                continue
            entry = model(target["source"], str(modelId), str(modelId))
            entry["source"] = "discovered"
            entries.append(entry)
        return entries

    ######################################################################

    @staticmethod
    def computeVersion(models: dict) -> str:
        payload = json.dumps(models, ensure_ascii = False, sort_keys = True, default = str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]

    def version(self) -> str:
        self.ensureFresh()
        return self._version

    def find(self, provider: str, modelId: str) -> Optional[dict]:
        self.ensureFresh()
        entry = self._models.get(provider, {}).get(modelId)
        return copy.deepcopy(entry) if entry else None

    def resolve(self, provider: str, modelId: str) -> dict:
        """
        目录项不存在时返回 unlisted 条目, 保证目录外 id 仍可调用
        """
        entry = self.find(provider, modelId)
        if entry is not None:
            return entry
        return {
            "id": modelId,
            "label": modelId,
            "provider": provider,
            "contextWindow": None,
            "maxOutput": None,
            "stream": True,
            "vision": False,
            "embedding": False,
            "pricing": None,
            "kind": "chat",
            "source": "unlisted",
            "unlisted": True,
        }

    def models(self, provider: Optional[str] = None) -> list:
        self.ensureFresh()
        if provider is not None:
            return [copy.deepcopy(entry) for entry in self._models.get(provider, {}).values()]
        return [
            copy.deepcopy(entry)
            for providerModels in self._models.values()
            for entry in providerModels.values()
        ]

    def modelIds(self, provider: str) -> list:
        """
        供旧版 chat/getModelsInfo 兼容使用的 id 列表 (保持内置顺序)
        """
        self.ensureFresh()
        return list(self._models.get(provider, {}).keys())

    def defaultModel(self, provider: str) -> Optional[str]:
        """
        默认模型: 目录中第一个可流式对话的内置模型
        """
        for entry in self.models(provider):
            if entry.get("kind") == "chat" and entry.get("stream") and not entry.get("embedding"):
                return entry["id"]
        return None

    def pricingFor(self, provider: str, modelId: str) -> Optional[dict]:
        entry = self.find(provider, modelId)
        if not entry:
            return None
        pricing = entry.get("pricing")
        return pricing if isinstance(pricing, dict) else None

    def snapshot(self, refresh: bool = False) -> dict:
        """
        /api/meta/models 的响应体
        """
        from app.providers.settings import describeProviders

        if refresh:
            self.refresh()
        else:
            self.ensureFresh()
        return {
            "catalogVersion": self._version,
            "providers": describeProviders(),
            "models": self.models(),
        }

##############################################################################################################################