"""美术方向风格卡（img-gen-taste 移植版）：按画风/剧情文本挑选最贴合的风格卡。

39 张卡的蒸馏内容存放在 oip/style-cards/*.md（与上游 open-image-prompts 仓库一致）。
路由是纯关键词打分，确定性、零依赖；匹配不上时回退到叙事插画卡（与分镜动画最贴近）。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

CARDS_DIR = Path(__file__).resolve().parent / "style-cards"
DEFAULT_CARD = "illustration-storytelling"
MAX_EXCERPT_CHARS = 2400

# 每张卡的中英匹配词（命中越多越贴合）。动画平台常用词优先覆盖。
CARD_KEYWORDS: dict[str, tuple[str, ...]] = {
    "product-advertising": (
        "产品", "包装", "珠宝", "首饰", "手表", "化妆品", "电商", "广告", "商业", "静物",
        "product", "packaging", "jewelry", "commercial", "ecommerce",
    ),
    "portrait-photography": (
        "人像", "肖像", "美妆", "写真", "颜值", "时尚", "portrait", "beauty", "fashion photography",
    ),
    "print-paint-craft": (
        "版画", "水墨", "纸艺", "剪纸", "手绘", "手工", "颜料", "版画感", "国风",
        "printmaking", "paper craft", "ink painting", "watercolor",
    ),
    "food-drink": (
        "美食", "食物", "饮品", "咖啡", "甜品", "餐厅", "菜单", "food", "drink", "dessert",
    ),
    "illustration-storytelling": (
        "插画", "绘本", "童话", "叙事", "漫画", "动画", "分镜", "儿童", "故事",
        "illustration", "storybook", "narrative", "comic", "anime",
    ),
    "poster-cover": (
        "海报", "封面", "招贴", "字体", "标题", "poster", "cover", "typography",
    ),
    "architecture-interior": (
        "建筑", "室内", "房间", "空间", " house", "房屋", "家居", "architecture", "interior",
    ),
    "landscape-city": (
        "风景", "城市", "山水", "田野", "航拍", "天空", "大海", "街道", "landscape", "city", "aerial",
    ),
    "sci-fi-fantasy": (
        "科幻", "奇幻", "魔法", "机甲", "太空", "未来", "游戏", "概念设计", "龙", "异世界",
        "sci-fi", "science fiction", "fantasy", "cyberpunk", "mecha", "game concept",
    ),
    "graphic-abstract": (
        "抽象", "图形", "几何", "3d", "网页", "ui", "logo", "abstract", "graphic",
    ),
}

_card_cache: dict[str, str] = {}


def available_cards() -> list[str]:
    if not CARDS_DIR.is_dir():
        return []
    return sorted(path.stem for path in CARDS_DIR.glob("*.md"))


def _load_card(name: str) -> str:
    if name not in _card_cache:
        path = CARDS_DIR / f"{name}.md"
        if not path.is_file():
            return ""
        try:
            _card_cache[name] = path.read_text(encoding="utf-8")
        except OSError:
            _card_cache[name] = ""
    return _card_cache[name]


def pick_card(*texts: str | None) -> str:
    """按关键词命中数选卡；全部为零命中时回退叙事插画卡。"""
    haystack = "\n".join(str(text or "") for text in texts).casefold()
    if not haystack.strip():
        return DEFAULT_CARD
    best_name, best_score = DEFAULT_CARD, 0
    for name, keywords in CARD_KEYWORDS.items():
        score = sum(1 for keyword in keywords if keyword in haystack)
        if score > best_score:
            best_name, best_score = name, score
    return best_name if _load_card(best_name) else DEFAULT_CARD


def card_excerpt(name: str, max_chars: int = MAX_EXCERPT_CHARS) -> str:
    """读取一张风格卡，截断到行边界，供注入 LLM 提示词。"""
    content = _load_card(name)
    if not content or len(content) <= max_chars:
        return content
    cut = content.rfind("\n", 0, max_chars)
    return content[: cut if cut > max_chars // 2 else max_chars].rstrip()


def card_meta(name: str) -> dict[str, str]:
    """解析一张卡的标题与首段摘要（供前端卡片墙展示）。"""
    content = _load_card(name)
    title, summary = name, ""
    if content:
        lines = content.splitlines()
        for line in lines:
            if line.startswith("# "):
                title = line[2:].strip() or title
                break
        for line in lines:
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            summary = re.sub(r"[*_`>\\[\\]]", "", text)
            break
    if len(summary) > 120:
        summary = summary[:119].rstrip() + "…"
    return {"id": name, "title": title, "summary": summary}


def card_list() -> list[dict[str, str]]:
    """全部风格卡的 {id, title, summary}，按固定顺序返回。"""
    order = list(CARD_KEYWORDS.keys())
    names = [n for n in order if _load_card(n)] + [
        n for n in available_cards() if n not in order
    ]
    return [card_meta(name) for name in names]


def card_full(name: str, max_chars: int = 6000) -> dict[str, Any] | None:
    """单张卡完整要点（前端详情展示用）；未知卡名返回 None。"""
    if name not in CARD_KEYWORDS and f"{name}.md" not in {p.name for p in CARDS_DIR.glob("*.md")}:
        return None
    content = card_excerpt(name, max_chars)
    if not content:
        return None
    return {**card_meta(name), "content": content}
