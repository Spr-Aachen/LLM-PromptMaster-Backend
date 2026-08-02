"""
Prompt 模板读取: 模板文件名来自 provider 配置的 [Chat-GPT] 段
"""

import logging
from pathlib import Path

##############################################################################################################################

logger = logging.getLogger("PromptMaster.prompts")

SECTION = "Chat-GPT"


def readTemplate(promptDir, manager, option: str) -> str:
    """
    按配置项读取模板正文; 配置或文件缺失时返回空串, 不阻断请求
    """
    if manager is None:
        return ""
    fileName = manager.getValue(SECTION, option, None)
    if not fileName:
        return ""
    path = Path(promptDir).joinpath(fileName)
    try:
        return path.read_text(encoding = "utf-8")
    except OSError as exc:
        logger.warning("读取 prompt 模板 %s 失败: %s", path.as_posix(), exc)
        return ""

##############################################################################################################################