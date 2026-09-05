from __future__ import annotations

import re
from dataclasses import dataclass


_PUNCTUATION = r"\s\t,，。.!！?？~～、"
_SOCIAL_ONLY = re.compile(
    rf"^(?:你?好|您好|嗨|哈喽|hello|hi|hey|早上好|上午好|中午好|下午好|晚上好|"
    rf"谢谢|多谢|感谢|好的|好|行|可以|收到|明白了|知道了|没问题|再见|拜拜|晚安)[{_PUNCTUATION}]*$",
    re.IGNORECASE,
)
_META_ONLY = re.compile(
    r"^(?:你是谁|你叫什么|介绍(?:一下)?你自己|你能做什么|你会什么|有什么功能|帮助|help)[\s？?。.!！]*$",
    re.IGNORECASE,
)
_NON_KNOWLEDGE_TASK = re.compile(
    r"^(?:请|麻烦|帮我|请帮我)?\s*(?:写|创作|生成|润色|改写|翻译|起名|讲)(?:一|个|首|篇|下|一下|段|这)",
    re.IGNORECASE,
)
_REALTIME_QUERY = re.compile(r"(?:天气|几点|现在时间|今天日期|股价|汇率|比分|最新新闻|热搜)", re.IGNORECASE)
_CODE_OR_ERROR_TASK = re.compile(
    r"(?:Traceback\s*\(most recent call last\)|(?:Assertion|Attribute|Import|Index|Key|Name|Runtime|Syntax|Type|Value|ZeroDivision)Error\s*:|"
    r"Exception\s*:|stack\s*trace|segmentation\s+fault|File\s+[\"'][^\"']+[\"'],\s*line\s+\d+|"
    r"代码.{0,12}(?:报错|错误|异常|修复|调试)|(?:报错|异常).{0,12}(?:怎么|如何|原因|修复)|"
    r"(?:debug|fix|diagnose).{0,20}(?:code|error|exception))",
    re.IGNORECASE,
)
_EXPLICIT_KNOWLEDGE = re.compile(
    r"(?:知识库|本地资料|上传的?(?:文件|文档)|根据(?:资料|文档|知识)|文档中|资料中|检索|查找)",
    re.IGNORECASE,
)
_DOMAIN_OR_QUESTION = re.compile(
    r"(?:是什么|为什么|怎么|如何|哪些|多少|区别|原理|定义|优缺点|作用|是否|能否|"
    r"ofdm|mimo|通信|信号|频谱|调制|编码|信道|子载波|波束|网络|协议|算法|模型|公式)",
    re.IGNORECASE,
)
_FOLLOW_UP = re.compile(r"(?:它|这个|上述|上面|刚才|前面|其中|那|这些|继续|再说|展开|总结)", re.IGNORECASE)


@dataclass(frozen=True)
class RagIntentDecision:
    should_retrieve: bool
    query: str
    reason: str


def route_rag_query(message: str, history: list[dict] | None = None) -> RagIntentDecision:
    """Conservative local-RAG router.

    The RAG toggle expresses user preference, so ambiguous information
    questions still retrieve. We only skip clear conversational, meta,
    creative, and real-time intents that a local knowledge base cannot help.
    """
    query = re.sub(r"\s+", " ", message or "").strip()
    if not query:
        return RagIntentDecision(False, query, "empty message")
    if _EXPLICIT_KNOWLEDGE.search(query):
        return RagIntentDecision(True, query, "explicit knowledge request")
    if _SOCIAL_ONLY.fullmatch(query):
        return RagIntentDecision(False, query, "conversational greeting")
    if _META_ONLY.fullmatch(query):
        return RagIntentDecision(False, query, "assistant meta question")
    if _NON_KNOWLEDGE_TASK.search(query):
        return RagIntentDecision(False, query, "generation or editing request")
    if _REALTIME_QUERY.search(query):
        return RagIntentDecision(False, query, "real-time information request")
    if _CODE_OR_ERROR_TASK.search(query):
        return RagIntentDecision(False, query, "code or error analysis request")

    if _FOLLOW_UP.search(query):
        previous = _last_user_message(history or [])
        if previous:
            return RagIntentDecision(True, f"{previous}\n追问：{query}", "contextual follow-up")

    compact = re.sub(rf"[{_PUNCTUATION}]", "", query)
    if len(compact) <= 6 and not _DOMAIN_OR_QUESTION.search(query):
        return RagIntentDecision(False, query, "short conversational message")
    return RagIntentDecision(True, query, "knowledge question")


def _last_user_message(history: list[dict]) -> str:
    for item in reversed(history):
        if item.get("role") != "user":
            continue
        content = re.sub(r"\s+", " ", str(item.get("content") or "")).strip()
        if content:
            return content[:500]
    return ""
