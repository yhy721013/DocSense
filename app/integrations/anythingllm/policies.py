"""AnythingLLM 集成层共享的重试、调用次数与 Workspace 行为策略。

本模块只保存多个 AnythingLLM 适配器共同遵守的执行策略，不收纳 URL、密钥或无关业务
常量。硬上限用于保护外部服务和任务成本，Workspace 策略用于保证新旧链路迁移期间行为
一致；每次都返回独立配置副本，避免跨任务共享可变对象。
"""

from __future__ import annotations


MAX_UPLOAD_RETRIES = 3
"""单次全局文档上传在首次请求之后允许的额外重试次数硬上限。"""

DEFAULT_UPLOAD_RETRIES = 3
"""单次全局文档上传默认额外重试次数；总请求次数还包含首次请求。"""

DEFAULT_UPLOAD_RETRY_BASE_DELAY_SECONDS = 3.0
"""Document Processor 暂时不可用时，指数退避的默认基础秒数。"""

MAX_EMBEDDING_ATTEMPTS = 3
"""单次文档绑定操作允许的总调用次数硬上限，包含首次调用。"""

DEFAULT_EMBEDDING_ATTEMPTS = 2
"""单次文档绑定操作默认总调用次数，包含首次调用。"""

DOCUMENT_RAG_WORKSPACE_POLICY_VERSION = 1
"""文档 RAG Workspace 策略版本；配置语义变化时必须递增。"""


def document_rag_workspace_settings() -> dict[str, object]:
    """返回文档抽取与永久知识库共同遵守的 Workspace 策略。

    返回新字典而不是公开可变模块常量，确保不同任务和 AnythingLLM Client 不能通过原地
    修改影响其他请求。新旧链路在迁移期都调用该函数，避免阶段 9 切换后悄悄回退到
    AnythingLLM 默认阈值、默认 Prompt 或默认检索数量。
    """
    return {
        "similarityThreshold": 0.25,
        "openAiTemp": 0.1,
        "openAiHistory": 1,
        "openAiPrompt": (
            "你是一个文档信息抽取与判断系统。\n"
            "【重要规则】\n"
            "1. 你只能基于已提供的文档内容回答，不得使用常识或猜测。\n"
            "2. 如果文档中不存在相关信息，必须返回 null。\n"
            "3. 你必须只输出合法的 JSON，不得包含任何解释、注释、Markdown 或多余文本。\n"
            "4. JSON 的字段名、层级和类型必须严格保持一致。\n"
            "5. 不允许补充文档中未明确出现的信息。\n"
        ),
        "queryRefusalResponse": (
            '{"outline":[],"security_level":"公开","category_confidence":0.1,'
            '"category":null,"sub_category":null,"category_candidates":[],'
            '"extract":{},"summary":"未能从文档中检索到足够信息"}'
        ),
        "chatMode": "query",
        "topN": 6,
    }


def chat_workspace_settings() -> dict[str, object]:
    """Return the workspace settings used by the file-chat conversation flow.

    This function is the single integration-layer owner of the legacy
    `/llm/chat` workspace policy. It returns a fresh dict on every call so
    request-scoped gateways cannot mutate shared configuration.
    """
    return {
        "similarityThreshold": 0.0,
        "openAiTemp": 0.7,
        "openAiHistory": 20,
        "openAiPrompt": (
            "你是一个基于文档内容的智能问答助手。\n"
            "请根据已提供的文档内容回答用户的问题。\n"
            "如果文档中没有相关信息，请如实告知用户。\n"
            "回答应当准确、清晰、有条理。\n"
        ),
        "chatMode": "chat",
        "topN": 20,
    }


def validate_upload_max_retries(value: int) -> int:
    """校验并返回全局文档上传的额外重试次数。

    显式拒绝 ``bool`` 和浮点数。Python 中 ``bool`` 是 ``int`` 的子类，如果只执行区间
    比较，``True`` 会被错误解释为一次重试，并把配置错误带入生产任务。
    """
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= MAX_UPLOAD_RETRIES
    ):
        raise ValueError(
            f"upload_max_retries 必须是 0 到 {MAX_UPLOAD_RETRIES} 之间的整数"
        )
    return value


def validate_upload_retry_base_delay(value: float) -> float:
    """校验并返回上传指数退避的非负基础秒数。"""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or value < 0
    ):
        raise ValueError("upload_retry_base_delay 必须是非负数")
    return float(value)


def validate_embedding_max_attempts(value: int) -> int:
    """校验并返回文档绑定操作包含首次调用在内的最大调用次数。"""
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_EMBEDDING_ATTEMPTS
    ):
        raise ValueError(
            "embedding_max_attempts 必须是 1 到 "
            f"{MAX_EMBEDDING_ATTEMPTS} 之间的整数"
        )
    return value
