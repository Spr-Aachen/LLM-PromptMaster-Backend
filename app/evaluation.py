"""
prompt 稳定性测试流程: 重复采样 → 相似度 → 稳定性评分 → 必要时重写
"""

import logging
import threading
from typing import Callable, Iterator, Optional

from app.utils import calc

##############################################################################################################################

logger = logging.getLogger("PromptMaster.evaluation")

DEFAULT_THRESHOLD = 0.9

# 稳定性评分固定使用 gpt-4o
STABILITY_MODEL = "gpt-4o"


def promptTestStream(
    messages: list,
    totalTestTimes: Optional[int],
    *,
    complete: Callable[..., str],
    promptStabilityEvaluator: str,
    stream: Optional[Callable[..., Iterator[str]]] = None,
    evaluate: Optional[Callable[..., str]] = None,
    promptReconstructor: Optional[str] = None,
    threshold: float = DEFAULT_THRESHOLD,
) -> Iterator[tuple]:
    """
    complete(messages, model=None) -> str: 按目标通道采样
    evaluate(messages, model=None) -> str: 稳定性评分通道, 缺省同 complete
    stream(messages, model=None) -> Iterator[str]: 重写通道, 缺省不重写
    """
    if not totalTestTimes or totalTestTimes <= 0:
        yield "测试次数必须大于 0\n\n", 400
        return

    answers = []
    for currentTestTime in range(1, totalTestTimes + 1):
        logger.info("Current test time: %s", currentTestTime)
        answers.append(complete(messages))

    # 相似度在后台线程计算, 与稳定性评分并行
    recordingThread = None
    if len(answers) > 1:
        recordingThread = threading.Thread(
            target = calc.computeSimilarity,
            args = (answers, _lastUserContent(messages), totalTestTimes, None),
            daemon = True,
        )
        recordingThread.start()

    yield "本次测试结果正在分析与计算中，请稍后...\n\n", 200

    stability = None
    evaluator = evaluate or complete
    try:
        evaluation = evaluator(
            [
                {"role": "system", "content": promptStabilityEvaluator},
                {"role": "user", "content": "\n,\n".join(answers)},
            ],
            model = STABILITY_MODEL,
        )
        yield f"分析完毕：\n\n{evaluation}\n\n", 200
        stability = float(str(evaluation).strip().strip('`'))
    except Exception:
        yield "本次测试返回值的稳定值提取失败，将使用本次测试返回值的相似度计算结果作为替代\n\n", 200
        if recordingThread is not None:
            recordingThread.join()
        stability = calc.average_similarity if calc.average_similarity is not None else 0.0

    if stability >= threshold:
        yield f"本次测试返回值的稳定性维持在：{stability}\n\nprompt无需调优\n\n", 200
        return
    if stability >= 0:
        yield f"本次测试返回值的稳定性维持在：{stability}\n\n建议对prompt进行调优\n\n", 200

    if not promptReconstructor or stream is None:
        return

    yield "开始prompt调优, please stand by...\n\n", 200
    prompt = next(
        (message.get("content", "") for message in messages if isinstance(message, dict) and message.get("role") == "system"),
        "",
    )
    try:
        for delta in stream(
            [
                {"role": "system", "content": promptReconstructor},
                {"role": "user", "content": "```\n%s\n```" % prompt},
            ]
        ):
            if delta:
                yield delta, 200
    except Exception as exc:
        logger.warning("prompt 重写失败: %s", exc)
        yield "prompt调优失败", 200


def _lastUserContent(messages: list) -> str:
    for message in reversed(messages or []):
        if isinstance(message, dict) and message.get("role") == "user":
            return str(message.get("content", ""))
    return ""

##############################################################################################################################