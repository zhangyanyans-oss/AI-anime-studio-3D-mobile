"""Open Image Prompts 核心能力移植包（检索引擎 + 美术风格卡）。

用法（在 FastAPI 后端）：
    import oip
    oip.corpus_status()                 # 语料库就绪状态
    oip.search_corpus("黄昏公园逆光")    # 完整检索结果
    oip.reference_brief("雨夜巷道追逐")  # 供提示词生成用的紧凑参考
    oip.pick_card(style_bible, synopsis) # 选一张美术方向风格卡
"""
from .engine import RETRIEVAL_VERSION
from .library import (
    corpus_status,
    reference_brief,
    search_corpus,
    slim_results,
)
from .cards import (
    DEFAULT_CARD,
    available_cards,
    card_excerpt,
    card_full,
    card_list,
    pick_card,
)

__all__ = [
    "RETRIEVAL_VERSION",
    "corpus_status",
    "search_corpus",
    "slim_results",
    "reference_brief",
    "DEFAULT_CARD",
    "available_cards",
    "card_excerpt",
    "card_full",
    "card_list",
    "pick_card",
]
