"""Read-only retrieval for the Open Image Prompts archive (ported for AI-anime-studio).

检索逻辑与上游 open-image-prompts 仓库保持一致：确定性、绝不调用模型、
绝不写数据库。本移植版把 CLI 入口换成了供 FastAPI 调用的程序化接口
（search_corpus / corpus_status / slim_results / reference_brief）。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import defaultdict
from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, Iterable, Sequence

from .archive_db import (
    active_taxonomy_version,
    connect_read_only,
    resolve_db_path,
    table_exists,
)
from .engine import (
    HAN_RE,
    IntentTag,
    RETRIEVAL_VERSION,
    SearchIntent,
    lexical_group_hits,
    lexical_term_present,
    parse_intent,
    weighted_tag_similarity,
)

SCHEMA_VERSION = "oip-retrieval-v1"

RELATED_RELAXABLE_DIMENSIONS = frozenset({
    "composition",
    "lighting",
    "color_palette",
    "mood",
})
RELATED_RELAXABLE_TAGS = frozenset({
    "camera_lens:long-exposure",
    "visual_style:minimal",
})
# Relaxing a facet means accepting a reference that does not *declare* it. It must
# not mean accepting one that declares the opposite: "black-and-white low-key noir
# alley portrait" was returning a bright natural-daylight lifestyle portrait, which
# shares only the monochrome treatment and demonstrates none of the requested
# grammar. Entries are deliberately limited to formal opposites within one
# dimension; add a pair only after checking it against the retrieval benchmark,
# since dropping a candidate also drops whatever coverage it provided.
RELATED_CONTRADICTORY_TAGS: dict[str, frozenset[str]] = {
    "lighting:neon-light": frozenset({"lighting:golden-hour", "lighting:natural-daylight", "lighting:high-key"}),
    "lighting:low-key": frozenset({"lighting:high-key", "lighting:natural-daylight"}),
    "lighting:high-key": frozenset({"lighting:low-key"}),
}
RELATED_IMAGE_EVIDENCE_DIMENSIONS = frozenset({
    "subject_type",
    "human_attributes",
    "visual_style",
    "composition",
    "camera_lens",
    "lighting",
    "color_palette",
    "mood",
    "scene",
})
RELATED_BLOCKED_TAGS = frozenset({
    "quality_flags:logo-watermark",
    "quality_flags:multi-panel-layout",
    "quality_flags:needs-review",
    "quality_flags:screenshot-ui",
    "quality_flags:visible-artifacts",
})
RELATED_TEXT_TAGS = frozenset({
    "quality_flags:embedded-text",
    "quality_flags:typography-heavy",
})


def json_array(value: object) -> list[str]:
    try:
        parsed = json.loads(str(value or "[]"))
        return [str(item) for item in parsed] if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        return []


def column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return table_exists(conn, table) and any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def current_version(conn: sqlite3.Connection) -> str:
    return active_taxonomy_version(conn)


def require_v2_active(conn: sqlite3.Connection) -> None:
    actual = current_version(conn)
    if actual != RETRIEVAL_VERSION:
        raise RuntimeError(
            f"retrieval is not ready: active taxonomy is {actual!r}, expected {RETRIEVAL_VERSION!r}"
        )


def search_terms(query: str) -> list[str]:
    """Compatibility helper backed by the v2 intent parser.

    In particular, this never emits individual Han characters.  A one-character
    match recalls most of a Chinese archive and was the main source of false
    positives in the previous search implementation.
    """
    return parse_intent(query).lexical_terms


def trim(text: object, maximum: int) -> str:
    value = str(text or "").strip()
    return value if len(value) <= maximum else f"{value[:maximum - 1].rstrip()}…"


def bounded_int(minimum: int, maximum: int):
    def parse(value: str) -> int:
        try:
            parsed = int(value)
        except ValueError as error:
            raise argparse.ArgumentTypeError(f"must be an integer from {minimum} to {maximum}") from error
        if not minimum <= parsed <= maximum:
            raise argparse.ArgumentTypeError(f"must be from {minimum} to {maximum}")
        return parsed
    return parse


def label_rows(conn: sqlite3.Connection, *, media: bool, taxonomy_version: str, tweet_ids: set[str] | None = None) -> Iterable[sqlite3.Row]:
    table = "media_labels" if media else "prompt_labels"
    if not (table_exists(conn, table) and table_exists(conn, "labels") and table_exists(conn, "label_dimensions")):
        return []
    alias = "ml" if media else "pl"
    owner = "ml.tweet_id" if media else "pl.tweet_id"
    versioned = column_exists(conn, table, "taxonomy_version")
    catalog = table_exists(conn, "taxonomy_labels") and versioned
    catalog_join = (
        f"LEFT JOIN taxonomy_labels tl ON tl.taxonomy_version={alias}.taxonomy_version AND tl.label_id=l.id"
        if catalog else ""
    )
    where: list[str] = []
    params: list[str] = []
    if media:
        where.append("ml.media_type='image'")
    if versioned:
        where.append(f"{alias}.taxonomy_version=?")
        params.append(taxonomy_version)
    if tweet_ids is not None:
        if not tweet_ids:
            return []
        where.append(f"{owner} IN ({','.join('?' for _ in tweet_ids)})")
        params.extend(sorted(tweet_ids))
    return conn.execute(
        f"""
        SELECT {owner} AS tweet_id, ld.key AS dimension_key, l.key AS label_key,
               l.name AS fallback_name, {alias}.confidence, '' AS evidence,
               {"tl.display_en, tl.display_zh, tl.aliases_en_json, tl.aliases_zh_json" if catalog else "NULL AS display_en, NULL AS display_zh, NULL AS aliases_en_json, NULL AS aliases_zh_json"}
        FROM {table} {alias}
        JOIN labels l ON l.id={alias}.label_id
        JOIN label_dimensions ld ON ld.id=l.dimension_id
        {catalog_join}
        WHERE {' AND '.join(where)}
        ORDER BY {owner}, ld.key, {alias}.confidence DESC, l.key
        """,
        params,
    )


def tags_by_tweet(conn: sqlite3.Connection, taxonomy_version: str, tweet_ids: set[str] | None = None) -> dict[str, list[dict[str, Any]]]:
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for media in (False, True):
        for row in label_rows(conn, media=media, taxonomy_version=taxonomy_version, tweet_ids=tweet_ids):
            tweet_id = str(row["tweet_id"])
            canonical = f"{row['dimension_key']}:{row['label_key']}"
            item = {
                "id": canonical,
                "dimension": row["dimension_key"],
                "key": row["label_key"],
                "display": {
                    "en": row["display_en"] or row["fallback_name"] or row["label_key"],
                    "zh-Hans": row["display_zh"] or row["display_en"] or row["fallback_name"] or row["label_key"],
                },
                "aliases": {
                    "en": json_array(row["aliases_en_json"]),
                    "zh-Hans": json_array(row["aliases_zh_json"]),
                },
                "confidence": float(row["confidence"] or 0),
                "evidence": row["evidence"] or "",
                "scope": "image" if media else "prompt",
                "scopes": ["image" if media else "prompt"],
            }
            key = (tweet_id, canonical)
            if key not in best:
                best[key] = item
            else:
                existing = best[key]
                existing["scopes"] = sorted(set([*existing.get("scopes", []), *item["scopes"]]))
                if item["confidence"] > existing["confidence"]:
                    item["scopes"] = existing["scopes"]
                    best[key] = item
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (tweet_id, _), item in best.items():
        grouped[tweet_id].append(item)
    for tags in grouped.values():
        tags.sort(key=lambda tag: (tag["dimension"], tag["key"]))
    return grouped


def translations_by_tweet(conn: sqlite3.Connection, taxonomy_version: str, tweet_ids: set[str] | None = None) -> dict[str, dict[str, str]]:
    if not table_exists(conn, "prompt_translations"):
        return {}
    if tweet_ids is not None and not tweet_ids:
        return {}
    where = "translation_version=?"
    params: list[str] = [taxonomy_version]
    if tweet_ids is not None:
        where += f" AND tweet_id IN ({','.join('?' for _ in tweet_ids)})"
        params.extend(sorted(tweet_ids))
    rows = conn.execute(
        f"SELECT tweet_id, locale, translated_text FROM prompt_translations WHERE {where}",
        params,
    )
    translated: dict[str, dict[str, str]] = defaultdict(dict)
    for row in rows:
        translated[str(row["tweet_id"])][str(row["locale"])] = str(row["translated_text"])
    return translated


def image_summary_by_tweet(conn: sqlite3.Connection, tweet_ids: set[str] | None = None) -> dict[str, dict[str, Any]]:
    if not table_exists(conn, "images"):
        return {}
    id_column = "id" if column_exists(conn, "images", "id") else "NULL"
    if tweet_ids is not None and not tweet_ids:
        return {}
    where = ""
    params: list[str] = []
    if tweet_ids is not None:
        where = f"WHERE tweet_id IN ({','.join('?' for _ in tweet_ids)})"
        params.extend(sorted(tweet_ids))
    rows = conn.execute(
        f"""
        SELECT {id_column} AS image_id, tweet_id, image_index, url, local_path
        FROM images {where} ORDER BY tweet_id, image_index, {id_column if id_column != 'NULL' else 'url'}
        """,
        params,
    )
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["tweet_id"])].append({
            "id": str(row["image_id"]) if row["image_id"] is not None else None,
            "index": int(row["image_index"]),
            "url": row["url"],
            "local_path": row["local_path"],
        })
    return {
        tweet_id: {
            "count": len(items),
            "representative": items[0],
            "items": items,
            "paths": [item["local_path"] for item in items if item["local_path"]],
        }
        for tweet_id, items in grouped.items()
    }


def recipe_by_tweet(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    if not table_exists(conn, "prompt_recipes"):
        return {}
    rows = conn.execute(
        """
        SELECT recipe.tweet_id, recipe.recipe_text_en, recipe.recipe_text_zh
        FROM prompt_recipes recipe
        JOIN (
          SELECT tweet_id, max(updated_at) AS updated_at FROM prompt_recipes
          WHERE status='generated' GROUP BY tweet_id
        ) latest ON latest.tweet_id=recipe.tweet_id AND latest.updated_at=recipe.updated_at
        """
    )
    return {
        str(row["tweet_id"]): {"en": row["recipe_text_en"], "zh-Hans": row["recipe_text_zh"]}
        for row in rows
    }


def searchable_text(prompt: sqlite3.Row, tags: list[dict[str, Any]], translations: dict[str, str]) -> str:
    tag_text = " ".join(
        " ".join([
            tag["id"], tag["display"]["en"], tag["display"]["zh-Hans"],
            *tag["aliases"]["en"], *tag["aliases"]["zh-Hans"], tag["evidence"],
        ])
        for tag in tags
    )
    return " ".join([
        str(prompt["prompt_text"] or ""), str(prompt["author"] or ""), str(prompt["tool"] or ""),
        *translations.values(), tag_text,
    ]).casefold()


def requires_female_subject(intent: SearchIntent) -> bool:
    female_terms = {"female", "woman", "women", "girl", "lady", "女性", "女人", "女孩", "女生", "女士"}
    return any(female_terms & set(group) for group in intent.required_lexical_groups)


def declares_male_subject(text: str) -> bool:
    return any(
        re.search(pattern, text)
        for pattern in (
            r'"gender"\s*:\s*"male"',
            r'"gender"\s*:\s*"男性"',
            r'"subject_type"\s*:\s*"man"',
            r'"subject_type"\s*:\s*"male"',
            r'"description"\s*:\s*"[^"]*\bman\b',
            r'"description"\s*:\s*"[^"]*\bmale\b',
            r'"description"\s*:\s*"[^"]*男性',
        )
    )


def score_match(query: str, terms: list[str], text: str, tag_ids: set[str]) -> float:
    phrase = query.casefold().strip()
    score = 14.0 if phrase and phrase in text else 0.0
    for term in terms:
        folded = term.casefold()
        if folded in tag_ids:
            score += 9
        elif folded in text:
            score += 2 if len(folded) == 1 else 4
    return score


def record_for(prompt: sqlite3.Row, tags: list[dict[str, Any]], translations: dict[str, str], images: dict[str, Any], recipes: dict[str, dict[str, Any]], *, score: float, maximum: int, max_tags: int, match_reasons: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    tweet_id = str(prompt["tweet_id"])
    matched_ids = {
        str(reason["value"])
        for reason in (match_reasons or [])
        if reason.get("type") in {"locked_tag", "must_tag", "should_tag"} and reason.get("value")
    }
    ranked_tags = sorted(tags, key=lambda tag: (tag["id"] not in matched_ids, -tag["confidence"], tag["id"]))
    image_data = images.get(tweet_id, {"count": 0, "representative": None, "items": [], "paths": []})
    return {
        "tweet_id": tweet_id,
        "score": round(score, 2),
        "author": prompt["author"],
        "tool": prompt["tool"] or "None",
        "created_at": prompt["created_at"] or prompt["collected_at"],
        "tweet_url": prompt["tweet_url"],
        "prompt_text": trim(prompt["prompt_text"], maximum),
        "source_prompt": str(prompt["prompt_text"] or ""),
        "source_url": prompt["tweet_url"],
        "translations": translations,
        "tag_count": len(tags),
        "tags": ranked_tags[:max_tags],
        "images": image_data,
        "image_paths": image_data.get("paths", []),
        "match_reasons": match_reasons or [],
        "recipe": recipes.get(tweet_id),
    }


def archive(conn: sqlite3.Connection, taxonomy_version: str | None = None, tweet_ids: set[str] | None = None) -> tuple[dict[str, sqlite3.Row], dict[str, list[dict[str, Any]]], dict[str, dict[str, str]], dict[str, dict[str, Any]], dict[str, dict[str, Any]], str]:
    # Retrieval is a v2-only product surface.  Legacy and partially inferred
    # free-form labels are intentionally never a fallback.
    require_v2_active(conn)
    version = RETRIEVAL_VERSION
    if tweet_ids is not None and not tweet_ids:
        return {}, {}, {}, {}, {}, version
    prompt_where = ""
    prompt_params: list[str] = []
    if tweet_ids is not None:
        prompt_where = f" WHERE tweet_id IN ({','.join('?' for _ in tweet_ids)})"
        prompt_params.extend(sorted(tweet_ids))
    prompts = {
        str(row["tweet_id"]): row
        for row in conn.execute(
            "SELECT tweet_id, author, tool, prompt_text, created_at, tweet_url, collected_at FROM prompts" + prompt_where,
            prompt_params,
        )
    }
    tags = tags_by_tweet(conn, version, tweet_ids)
    translations = translations_by_tweet(conn, version, tweet_ids)
    # A named staging version must never fall through to unlabeled legacy
    # prompts merely because their raw source text happens to match a query.
    prompts = {tweet_id: prompt for tweet_id, prompt in prompts.items() if tweet_id in tags}
    return prompts, tags, translations, image_summary_by_tweet(conn, tweet_ids), recipe_by_tweet(conn), version


def fts_rank_percentiles(conn: sqlite3.Connection, intent: SearchIntent, limit: int = 500) -> dict[str, float]:
    """Return a bounded lexical rank boost when native SQLite FTS5 is present."""
    if not intent.lexical_terms or not table_exists(conn, "prompt_fts"):
        return {}
    phrases = []
    for term in intent.lexical_terms:
        escaped = term.replace('"', '""').strip()
        if escaped:
            phrases.append(f'"{escaped}"')
    if not phrases:
        return {}
    expression = " OR ".join(dict.fromkeys(phrases))
    try:
        rows = list(conn.execute(
            """
            SELECT tweet_id, bm25(prompt_fts, 0.0, 1.0, 0.05, 0.1, 1.8) AS rank
            FROM prompt_fts WHERE prompt_fts MATCH ?
            ORDER BY rank LIMIT ?
            """,
            (expression, limit),
        ))
    except sqlite3.OperationalError:
        return {}
    count = max(1, len(rows))
    return {str(row["tweet_id"]): 1.0 - (index / count) for index, row in enumerate(rows)}


def candidate_tweet_ids(
    conn: sqlite3.Connection,
    intent: SearchIntent,
    *,
    fts_scores: dict[str, float],
    author: str | None,
    tool: str | None,
    require_images: bool,
    limit: int = 1500,
) -> set[str]:
    """Use indexed v2 label assignments to bound hydration to a small pool."""
    required = sorted({item.tag for item in [*intent.locked_tags, *intent.must_tags]})
    candidates = set(fts_scores)
    if required:
        label_rows = list(conn.execute(
            f"""
            SELECT label_id FROM taxonomy_labels
            WHERE taxonomy_version=? AND (dimension_key || ':' || key) IN ({','.join('?' for _ in required)})
            """,
            [RETRIEVAL_VERSION, *required],
        ))
        label_ids = sorted({int(row[0]) for row in label_rows})
        if len(label_ids) != len(required):
            return set()
        placeholders = ",".join("?" for _ in label_ids)
        where = []
        params: list[Any] = [RETRIEVAL_VERSION, *label_ids, RETRIEVAL_VERSION, *label_ids]
        if require_images:
            where.append("EXISTS (SELECT 1 FROM images image WHERE image.tweet_id=p.tweet_id)")
        if author:
            where.append("lower(p.author) LIKE ?")
            params.append(f"%{author.casefold()}%")
        if tool:
            where.append("lower(COALESCE(p.tool,'')) LIKE ?")
            params.append(f"%{tool.casefold()}%")
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        params.extend([len(label_ids), limit])
        rows = conn.execute(
            f"""
            WITH fact AS (
              SELECT tweet_id,label_id,max(confidence) confidence
              FROM prompt_labels
              WHERE taxonomy_version=? AND label_id IN ({placeholders})
              GROUP BY tweet_id,label_id
              UNION ALL
              SELECT tweet_id,label_id,max(confidence) confidence
              FROM media_labels
              WHERE taxonomy_version=? AND media_type='image' AND label_id IN ({placeholders})
              GROUP BY tweet_id,label_id
            ), aggregated AS (
              SELECT tweet_id,label_id,max(confidence) confidence
              FROM fact GROUP BY tweet_id,label_id
            )
            SELECT p.tweet_id,sum(aggregated.confidence) confidence
            FROM prompts p JOIN aggregated ON aggregated.tweet_id=p.tweet_id
            {where_sql}
            GROUP BY p.tweet_id
            HAVING count(DISTINCT aggregated.label_id)=?
            ORDER BY confidence DESC,p.tweet_id
            LIMIT ?
            """,
            params,
        )
        candidates.update(str(row[0]) for row in rows)
    elif not candidates:
        where = []
        params: list[Any] = []
        if require_images:
            where.append("EXISTS (SELECT 1 FROM images image WHERE image.tweet_id=p.tweet_id)")
        if author:
            where.append("lower(p.author) LIKE ?")
            params.append(f"%{author.casefold()}%")
        if tool:
            where.append("lower(COALESCE(p.tool,'')) LIKE ?")
            params.append(f"%{tool.casefold()}%")
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        params.append(limit)
        candidates.update(str(row[0]) for row in conn.execute(
            f"""
            SELECT p.tweet_id FROM prompts p {where_sql}
            ORDER BY CAST(p.tweet_id AS INTEGER) DESC LIMIT ?
            """,
            params,
        ))
    return candidates


def tag_evidence(tag: dict[str, Any]) -> tuple[float, str]:
    scopes = set(tag.get("scopes") or [tag.get("scope", "prompt")])
    if scopes == {"image", "prompt"}:
        return 1.0, "prompt+image"
    if "image" in scopes:
        return 0.9, "image"
    return 0.55, "prompt"


def score_candidate(intent: SearchIntent, prompt: sqlite3.Row, prompt_tags: list[dict[str, Any]], translations: dict[str, str], image_data: dict[str, Any], fts_percentile: float) -> tuple[float, list[dict[str, Any]]] | None:
    by_id = {tag["id"].casefold(): tag for tag in prompt_tags}
    reasons: list[dict[str, Any]] = []
    score = 0.0

    forbidden_hits = [constraint.tag for constraint in intent.forbidden_tags if constraint.tag.casefold() in by_id]
    if forbidden_hits:
        return None

    if intent.verified_only and str(prompt["tweet_id"]) not in intent.verified_tweet_ids:
        return None

    for required_image_tag in intent.required_image_tags:
        tag = by_id.get(required_image_tag.casefold())
        if (
            (not tag or "image" not in set(tag.get("scopes") or [tag.get("scope", "prompt")]))
            and str(prompt["tweet_id"]) not in intent.verified_tweet_ids
        ):
            return None
    if str(prompt["tweet_id"]) in intent.verified_tweet_ids:
        score += 30.0
        reasons.append({"type": "human_verified", "value": str(prompt["tweet_id"])})

    for constraint in [*intent.locked_tags, *intent.must_tags]:
        tag = by_id.get(constraint.tag.casefold())
        if not tag:
            return None
        evidence, scope = tag_evidence(tag)
        weight = 22.0 if constraint.mode == "locked" else 15.0
        score += weight * evidence
        reasons.append({
            "type": f"{constraint.mode}_tag",
            "value": constraint.tag,
            "scope": scope,
            "matched_by": list(constraint.matched_by),
        })

    for constraint in intent.should_tags:
        tag = by_id.get(constraint.tag.casefold())
        if not tag:
            continue
        evidence, scope = tag_evidence(tag)
        score += 6.0 * evidence
        reasons.append({
            "type": "should_tag",
            "value": constraint.tag,
            "scope": scope,
            "matched_by": list(constraint.matched_by),
        })

    text = searchable_text(prompt, prompt_tags, translations)
    requested = {tag.tag for tag in [*intent.locked_tags, *intent.must_tags, *intent.should_tags]}
    if (
        "sneaker" in intent.concepts
        and {"subject_type:product", "usage:ecommerce", "color_palette:white-minimal"}.issubset(requested)
    ):
        clean_product_conflicts = {
            "subject_type:fashion-person",
            "subject_type:portrait",
            "quality_flags:embedded-text",
            "quality_flags:typography-heavy",
            "quality_flags:multi-panel-layout",
            "quality_flags:screenshot-ui",
        }
        if clean_product_conflicts & set(by_id):
            return None
    if (
        "perfume" in intent.concepts
        and {"visual_style:minimal", "color_palette:dark-moody", "usage:ad-campaign"}.issubset(requested)
    ):
        restrained_product_conflicts = {
            "subject_type:fashion-person",
            "subject_type:portrait",
            "quality_flags:embedded-text",
            "quality_flags:typography-heavy",
            "quality_flags:multi-panel-layout",
            "mood:energetic",
            "composition:dynamic-diagonal",
        }
        if restrained_product_conflicts & set(by_id):
            return None
        if lexical_group_hits(text, ["splash", "splashing", "burst", "explosion", "飞溅", "喷溅", "爆炸"]):
            return None
    if "jewelry-product" in intent.concepts:
        jewelry_product_conflicts = {
            "subject_type:portrait",
            "subject_type:fashion-person",
            "subject_type:beauty-closeup",
            "subject_type:diagram-document",
            "quality_flags:embedded-text",
            "quality_flags:typography-heavy",
            "quality_flags:multi-panel-layout",
            "quality_flags:screenshot-ui",
            "quality_flags:logo-watermark",
        }
        if jewelry_product_conflicts & set(by_id):
            return None
        if lexical_group_hits(text, ["keychain", "key chain", "key ring", "钥匙扣", "钥匙链"]):
            return None
    if "tech-product" in intent.concepts:
        tech_product_conflicts = {
            "subject_type:graphic-design",
            "subject_type:fashion-person",
            "subject_type:portrait",
            "subject_type:food-drink",
            "subject_type:character",
            "quality_flags:typography-heavy",
            "quality_flags:multi-panel-layout",
            "quality_flags:screenshot-ui",
            "quality_flags:logo-watermark",
            "usage:poster",
        }
        if tech_product_conflicts & set(by_id):
            return None
    if "rain-race-car" in intent.concepts:
        race_car_conflicts = {
            "subject_type:character",
            "subject_type:diagram-document",
            "subject_type:ui-screen",
            "quality_flags:embedded-text",
            "quality_flags:typography-heavy",
            "quality_flags:multi-panel-layout",
            "quality_flags:screenshot-ui",
            "quality_flags:logo-watermark",
        }
        if race_car_conflicts & set(by_id):
            return None
    hard_constraint_count = len(intent.locked_tags) + len(intent.must_tags)
    lexical_weight = 1.0 if hard_constraint_count < 2 else 0.75
    lexical_cap = 18.0 if hard_constraint_count < 2 else 4.0
    phrase_weight = 10.0 if hard_constraint_count < 2 else 2.0
    bm25_weight = 12.0 if hard_constraint_count < 2 else 2.0
    for required_group in intent.required_lexical_groups:
        group_hits = lexical_group_hits(text, required_group)
        if not group_hits:
            return None
        score += 10.0
        reasons.append({"type": "required_lexical", "values": group_hits})
    if requires_female_subject(intent) and declares_male_subject(text):
        return None
    phrase = intent.query.casefold().strip()
    if phrase and lexical_term_present(text, phrase):
        score += phrase_weight
        reasons.append({"type": "exact_phrase", "value": intent.query})
    lexical_hits = [term for term in intent.lexical_terms if lexical_term_present(text, term)]
    if lexical_hits:
        score += min(lexical_cap, lexical_weight * len(lexical_hits))
        reasons.append({"type": "lexical", "values": lexical_hits})
    if fts_percentile:
        score += bm25_weight * fts_percentile
        reasons.append({"type": "bm25", "percentile": round(fts_percentile, 3)})

    if image_data.get("count", 0):
        score += 3.0
        reasons.append({"type": "image_available", "count": image_data["count"]})

    quality_ids = set(by_id)
    if "quality_flags:needs-review" in quality_ids:
        score -= 12.0
    if "quality_flags:visible-artifacts" in quality_ids:
        score -= 8.0
    if "quality_flags:screenshot-ui" in quality_ids and "subject_type:ui-screen" not in {tag.tag for tag in [*intent.locked_tags, *intent.must_tags, *intent.should_tags]}:
        score -= 5.0
    requested = {tag.tag for tag in [*intent.locked_tags, *intent.must_tags]}
    if "lighting:neon-light" in requested:
        if "color_palette:neon" in quality_ids:
            score += 8.0
        if "color_palette:dark-moody" in quality_ids:
            score += 4.0
        if "visual_style:black-and-white" in quality_ids or "color_palette:black-and-white" in quality_ids:
            score -= 20.0
        if "color_palette:warm-tones" in quality_ids:
            score -= 8.0

    if intent.query.strip() and not any(reason["type"] not in {"image_available"} for reason in reasons):
        return None
    return score, reasons


def relaxation_plan(intent: SearchIntent) -> list[str]:
    """Explicit constraints are never relaxed; inferred preferences are `should`."""
    return []


def related_relaxation_plan(intent: SearchIntent) -> list[str]:
    """Return single-facet relaxations that are safe to expose as related references.

    The exact result channel remains fail-closed. Related references may omit one
    aesthetic facet, but never a locked tag, subject, usage, scene, workflow, or
    negative constraint.
    """
    return [
        item.tag
        for item in intent.must_tags
        if (
            item.tag in RELATED_RELAXABLE_TAGS
            or item.tag.partition(":")[0] in RELATED_RELAXABLE_DIMENSIONS
        )
    ]


def intent_with_relaxations(intent: SearchIntent, relaxed: list[str]) -> SearchIntent:
    relaxed_set = set(relaxed)
    moved = [item for item in intent.must_tags if item.tag in relaxed_set]
    kept = [item for item in intent.must_tags if item.tag not in relaxed_set]
    should_by_tag = {item.tag: item for item in intent.should_tags}
    for item in moved:
        should_by_tag[item.tag] = IntentTag(item.tag, "should", item.matched_by)
    return SearchIntent(
        query=intent.query,
        language=intent.language,
        locked_tags=list(intent.locked_tags),
        must_tags=kept,
        should_tags=sorted(should_by_tag.values(), key=lambda item: item.tag),
        forbidden_tags=list(intent.forbidden_tags),
        lexical_terms=list(intent.lexical_terms),
        required_lexical_groups=[list(group) for group in intent.required_lexical_groups],
        required_image_tags=list(intent.required_image_tags),
        verified_tweet_ids=list(intent.verified_tweet_ids),
        verified_only=intent.verified_only,
        concepts=list(intent.concepts),
    )


def search_hits(
    conn: sqlite3.Connection,
    intent: SearchIntent,
    *,
    fts_scores: dict[str, float],
    require_images: bool,
    author_filter: str | None,
    tool_filter: str | None,
    max_prompt_chars: int,
    max_tags: int,
    relaxed: list[str],
) -> tuple[list[dict[str, Any]], str]:
    candidate_ids = candidate_tweet_ids(
        conn, intent, fts_scores=fts_scores, author=author_filter, tool=tool_filter,
        require_images=require_images,
    )
    prompts, tags, translations, images, recipes, version = archive(
        conn, RETRIEVAL_VERSION, candidate_ids,
    )
    hits = []
    for tweet_id, prompt in prompts.items():
        if author_filter and author_filter.casefold() not in str(prompt["author"] or "").casefold():
            continue
        if tool_filter and tool_filter.casefold() not in str(prompt["tool"] or "").casefold():
            continue
        prompt_tags = tags.get(tweet_id, [])
        image_data = images.get(tweet_id, {"count": 0, "representative": None, "items": [], "paths": []})
        if require_images and not image_data["count"]:
            continue
        scored = score_candidate(
            intent, prompt, prompt_tags, translations.get(tweet_id, {}), image_data,
            fts_scores.get(tweet_id, 0.0),
        )
        if scored is None:
            continue
        score, reasons = scored
        if relaxed:
            reasons.append({"type": "relaxation", "constraints": list(relaxed)})
        item = record_for(
            prompt, prompt_tags, translations.get(tweet_id, {}), images, recipes,
            score=score, maximum=max_prompt_chars, max_tags=max_tags,
            match_reasons=reasons,
        )
        item["relaxation_level"] = len(relaxed)
        item["relaxed_constraints"] = list(relaxed)
        item["_tag_ids"] = {tag["id"] for tag in prompt_tags}
        item["_tag_scopes"] = {
            tag["id"]: set(tag.get("scopes") or [tag.get("scope", "prompt")])
            for tag in prompt_tags
        }
        item["_prompt_key"] = re.sub(r"\s+", " ", str(prompt["prompt_text"] or "").casefold()).strip()[:1000]
        hits.append(item)
    hits.sort(key=lambda item: (-item["score"], item["tweet_id"]), reverse=False)
    return hits, version


def diversify_tiers(
    tiers: list[list[dict[str, Any]]], limit: int, *, finalize: bool = True,
) -> list[dict[str, Any]]:
    chosen: list[dict[str, Any]] = []
    author_counts: dict[str, int] = defaultdict(int)
    prompt_keys: set[str] = set()
    tweet_ids: set[str] = set()
    for hits in tiers:
        pool = [item for item in hits[:max(100, limit * 15)] if item["tweet_id"] not in tweet_ids]
        maximum = max((float(item["score"]) for item in pool), default=1.0) or 1.0
        while pool and len(chosen) < limit:
            best_index = None
            best_value = float("-inf")
            for index, item in enumerate(pool):
                author = str(item.get("author") or "")
                if item["tweet_id"] in tweet_ids or author_counts[author] >= 2 or item["_prompt_key"] in prompt_keys:
                    continue
                similarity = max(
                    (weighted_tag_similarity(item["_tag_ids"], previous["_tag_ids"]) for previous in chosen),
                    default=0.0,
                )
                value = 0.82 * (float(item["score"]) / maximum) - 0.18 * similarity
                if author_counts[author]:
                    value -= 0.08
                if value > best_value:
                    best_value = value
                    best_index = index
            if best_index is None:
                break
            item = pool.pop(best_index)
            chosen.append(item)
            tweet_ids.add(item["tweet_id"])
            author_counts[str(item.get("author") or "")] += 1
            prompt_keys.add(item["_prompt_key"])
        if len(chosen) >= limit:
            break
    if finalize:
        for rank, item in enumerate(chosen, 1):
            item["rank"] = rank
            item.pop("_tag_ids", None)
            item.pop("_tag_scopes", None)
            item.pop("_prompt_key", None)
    return chosen


def related_candidate_allowed(
    intent: SearchIntent,
    item: dict[str, Any],
    relaxed: Sequence[str] = (),
) -> bool:
    """Require visual evidence and reject obvious gallery-hostile near matches."""
    tag_ids = set(item.get("_tag_ids") or ())
    tag_scopes = item.get("_tag_scopes") or {}
    for constraint in relaxed:
        if RELATED_CONTRADICTORY_TAGS.get(constraint, frozenset()) & tag_ids:
            return False
    requested = {
        constraint.tag
        for constraint in [*intent.locked_tags, *intent.must_tags, *intent.should_tags]
    }
    if RELATED_BLOCKED_TAGS & tag_ids:
        return False
    if "subject_type:diagram-document" in tag_ids and "subject_type:diagram-document" not in requested:
        return False
    if "subject_type:ui-screen" in tag_ids and "subject_type:ui-screen" not in requested:
        return False
    if RELATED_TEXT_TAGS & tag_ids and not (RELATED_TEXT_TAGS & requested):
        return False
    for constraint in [*intent.locked_tags, *intent.must_tags]:
        if (
            constraint.tag.partition(":")[0] in RELATED_IMAGE_EVIDENCE_DIMENSIONS
            and "image" not in set(tag_scopes.get(constraint.tag) or ())
        ):
            return False
    return True


def merged_related_hits(tiers: list[list[dict[str, Any]]], exact_ids: set[str]) -> list[dict[str, Any]]:
    """Merge independent one-facet tiers without favoring config order."""
    best_by_id: dict[str, dict[str, Any]] = {}
    for hits in tiers:
        for item in hits:
            tweet_id = str(item["tweet_id"])
            if tweet_id in exact_ids:
                continue
            existing = best_by_id.get(tweet_id)
            if existing is None or float(item["score"]) > float(existing["score"]):
                best_by_id[tweet_id] = item
    return sorted(
        best_by_id.values(),
        key=lambda item: (-float(item["score"]), str(item["tweet_id"])),
    )


def run_search(conn: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    require_v2_active(conn)
    selected_tag = str(getattr(args, "tag", "") or "").casefold().strip()
    intent = parse_intent(args.query, [selected_tag] if selected_tag else [])
    fts_scores = fts_rank_percentiles(conn, intent)
    require_images = not bool(getattr(args, "allow_no_image", False))
    author_filter = getattr(args, "author", None)
    tool_filter = getattr(args, "tool", None)
    tiers: list[list[dict[str, Any]]] = []
    tier_summaries = []
    relaxed: list[str] = []
    plan = relaxation_plan(intent)
    version = RETRIEVAL_VERSION
    while True:
        tier_intent = intent_with_relaxations(intent, relaxed)
        hits, version = search_hits(
            conn, tier_intent, fts_scores=fts_scores, require_images=require_images,
            author_filter=author_filter, tool_filter=tool_filter,
            max_prompt_chars=args.max_prompt_chars, max_tags=args.max_tags,
            relaxed=relaxed,
        )
        tiers.append(hits)
        tier_summaries.append({
            "level": len(relaxed), "relaxed_constraints": list(relaxed),
            "match_count": len(hits),
        })
        selectable_count = len(diversify_tiers(tiers, args.limit, finalize=False))
        if selectable_count >= args.limit or len(relaxed) >= len(plan):
            break
        relaxed.append(plan[len(relaxed)])
    exact_match_count = len(tiers[0]) if tiers else 0
    total_matches = len({item["tweet_id"] for tier in tiers for item in tier})
    selected = diversify_tiers(tiers, args.limit)
    for item in selected:
        item["match_kind"] = "exact"
        item["missing_constraints"] = []
    used_relaxations = list(dict.fromkeys(
        constraint for item in selected for constraint in item.get("relaxed_constraints", [])
    ))
    related_limit = max(0, args.limit - len(selected))
    related_tiers: list[list[dict[str, Any]]] = []
    related_tier_summaries: list[dict[str, Any]] = []
    if related_limit:
        exact_ids = {str(item["tweet_id"]) for item in selected}
        for constraint in related_relaxation_plan(intent):
            tier_intent = intent_with_relaxations(intent, [constraint])
            related_hits, version = search_hits(
                conn, tier_intent, fts_scores=fts_scores, require_images=require_images,
                author_filter=author_filter, tool_filter=tool_filter,
                max_prompt_chars=args.max_prompt_chars, max_tags=args.max_tags,
                relaxed=[constraint],
            )
            related_hits = [
                item for item in related_hits
                if related_candidate_allowed(tier_intent, item, [constraint])
            ]
            related_tiers.append(related_hits)
            related_tier_summaries.append({
                "relaxed_constraint": constraint,
                "match_count": len(related_hits),
            })
        related_pool = merged_related_hits(related_tiers, exact_ids)
        related_selected = diversify_tiers([related_pool], related_limit)
    else:
        related_selected = []
    for item in related_selected:
        item["match_kind"] = "related"
        item["missing_constraints"] = list(item.get("relaxed_constraints") or [])
    return {
        "schema_version": SCHEMA_VERSION,
        "query": args.query,
        "active_taxonomy_version": current_version(conn),
        "taxonomy_version": version,
        "parsed_intent": intent.as_dict(),
        "relaxed_constraints": used_relaxations,
        "search_tiers": tier_summaries,
        "result_count": len(selected),
        "exact_match_count": exact_match_count,
        "total_matches": total_matches,
        "results": selected,
        "related_count": len(related_selected),
        "related_total_matches": len({
            item["tweet_id"] for tier in related_tiers for item in tier
        }),
        "related_search_tiers": related_tier_summaries,
        "related_results": related_selected,
    }


def run_get(conn: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    require_v2_active(conn)
    prompts, tags, translations, images, recipes, version = archive(conn, RETRIEVAL_VERSION, {str(args.tweet_id)})
    prompt = prompts.get(args.tweet_id)
    if not prompt:
        raise SystemExit(f"No prompt found for tweet_id={args.tweet_id}")
    item = record_for(
        prompt, tags.get(args.tweet_id, []), translations.get(args.tweet_id, {}), images, recipes,
        score=0, maximum=args.max_prompt_chars, max_tags=args.max_tags,
    )
    item["rank"] = 1
    return {
        "schema_version": SCHEMA_VERSION,
        "active_taxonomy_version": current_version(conn),
        "taxonomy_version": version,
        "result": item,
    }


def run_status(conn: sqlite3.Connection) -> dict[str, Any]:
    version = RETRIEVAL_VERSION
    actual = current_version(conn)
    return {
        "schema_version": SCHEMA_VERSION,
        "active_taxonomy_version": actual,
        "required_taxonomy_version": version,
        "ready": actual == version,
        "prompts": conn.execute("SELECT count(*) FROM prompts").fetchone()[0],
        "images": conn.execute("SELECT count(*) FROM images").fetchone()[0] if table_exists(conn, "images") else 0,
        "translations": conn.execute("SELECT count(*) FROM prompt_translations WHERE translation_version=?", (version,)).fetchone()[0] if table_exists(conn, "prompt_translations") else 0,
        "prompt_labels": conn.execute("SELECT count(*) FROM prompt_labels" + (" WHERE taxonomy_version=?" if column_exists(conn, "prompt_labels", "taxonomy_version") else ""), (version,) if column_exists(conn, "prompt_labels", "taxonomy_version") else ()).fetchone()[0] if table_exists(conn, "prompt_labels") else 0,
    }


# ---------- 程序化接口（供 FastAPI 调用） ----------

def _ensure_ready(conn: sqlite3.Connection) -> None:
    require_v2_active(conn)


def corpus_status() -> dict[str, Any]:
    """返回语料库可用性与规模统计；库不可用时 ready=False。"""
    db_path = resolve_db_path()
    if not db_path:
        return {"ready": False, "reason": "未找到 prompts.db：请在 .env 配置 OIP_DB_PATH，或将库放入 oip/data/"}
    try:
        with closing(connect_read_only(db_path)) as conn:
            return run_status(conn)
    except Exception as exc:
        return {"ready": False, "reason": f"语料库打开失败：{exc}"}


def search_corpus(
    query: str,
    *,
    limit: int = 6,
    max_prompt_chars: int = 900,
    max_tags: int = 10,
    allow_no_image: bool = False,
    tag: str = "",
) -> dict[str, Any]:
    """执行一次完整的 v2 检索（与上游 CLI search 完全同逻辑）。"""
    query = str(query or "").strip()
    if not query:
        raise ValueError("query 不能为空")
    db_path = resolve_db_path()
    if not db_path:
        raise RuntimeError("OIP 数据库不可用：请在 .env 配置 OIP_DB_PATH 指向 prompts.db，或将库放入 oip/data/")
    args = SimpleNamespace(
        query=query[:200],
        limit=max(1, min(int(limit), 20)),
        max_prompt_chars=max(200, min(int(max_prompt_chars), 20000)),
        max_tags=max(1, min(int(max_tags), 50)),
        allow_no_image=bool(allow_no_image),
        tag=str(tag or "").strip(),
        author=None,
        tool=None,
    )
    with closing(connect_read_only(db_path)) as conn:
        payload = run_search(conn, args)
        # 宽松兜底：语料库面向「美学意图」查询设计，must 标签是硬约束；
        # 动画平台的用户常输入叙事句（"一只加班的橘猫幻想自己是大侠"）或
        # 收窄过头的词组（"校园 日系"），严格匹配可能只剩 0-1 条。此时
        # 把全部 must 降级为 should、清空词组硬约束，按语义相关度重排一轮，
        # 结果标记为 related（前端显示"相关推荐"），绝不冒充严格命中。
        strict_n = int(payload.get("result_count") or 0) + int(payload.get("related_count") or 0)
        if strict_n < 3 and not args.tag:
            fallback = _narrative_fallback(conn, args)
            if fallback:
                existing = {
                    str(item.get("tweet_id"))
                    for item in [*(payload.get("results") or []), *(payload.get("related_results") or [])]
                }
                merged = [item for item in fallback if str(item.get("tweet_id")) not in existing]
                payload["related_results"] = [*(payload.get("related_results") or []), *merged]
                payload["related_count"] = len(payload["related_results"])
                payload["fallback"] = {"used": True, "added": len(merged)}
        return payload


def _relax_all_intent(intent: SearchIntent) -> SearchIntent:
    """全部 must → should，清空词组/图片硬约束；保留 locked 与 forbidden。"""
    return SearchIntent(
        query=intent.query,
        language=intent.language,
        locked_tags=list(intent.locked_tags),
        must_tags=[],
        should_tags=sorted([*intent.must_tags, *intent.should_tags], key=lambda item: item.tag),
        forbidden_tags=list(intent.forbidden_tags),
        lexical_terms=list(intent.lexical_terms),
        required_lexical_groups=[],
        required_image_tags=[],
        verified_tweet_ids=[],
        verified_only=False,
        concepts=list(intent.concepts),
    )


def _narrative_search_terms(terms: Iterable[str], max_terms: int = 24) -> list[str]:
    """把检索词扩展成 n-gram：长中文串（叙事句）在 unicode61 分词下只有整段
    连续出现才能命中，切出 4/3/2 字滑窗后，句中的关键画面词（橘猫/大侠/雨夜）
    才有机会被 OR 检索召回。拉丁词原样保留。"""
    out: list[str] = []
    for term in terms:
        text = str(term or "").strip()
        if not text:
            continue
        out.append(text)
        if HAN_RE.search(text) and len(text) >= 3:
            han_run = "".join(HAN_RE.findall(text))
            for size in (4, 3, 2):
                if len(han_run) < size:
                    continue
                out.extend(han_run[i: i + size] for i in range(len(han_run) - size + 1))
    return list(dict.fromkeys(out))[:max_terms]


def _like_fts_scores(conn: sqlite3.Connection, terms: list[str], limit: int = 800) -> dict[str, float]:
    """FTS 召回为空时的 LIKE 兜底：在 prompt 正文与中文翻译里做子串匹配，
    命中的 n-gram 词数作为候选分（归一到 0~1）。小库（几千条）下毫秒级。
    两字词全部落空时，退到高频内容单字召回（极端叙事句的最后兜底）。"""
    useful = [t for t in terms if len(t) >= 2][:16]
    if not useful:
        return {}

    def scan(from_clause: str, col: str, weight: float) -> dict[str, float]:
        likes = " OR ".join([f"{col} LIKE ?"] * len(useful))
        params = [f"%{t}%" for t in useful]
        out: dict[str, float] = {}
        for row in conn.execute(
            f"SELECT tweet_id, {col} FROM {from_clause} WHERE {likes}", params
        ):
            tid = str(row[0])
            out[tid] = out.get(tid, 0.0) + weight * sum(1 for t in useful if t in str(row[1] or ""))
        return out

    merged = scan("prompts p", "p.prompt_text", 1.0)
    for tid, score in scan("prompt_translations t", "t.translated_text", 0.8).items():
        merged[tid] = merged.get(tid, 0.0) + score
    if not merged:
        # 单字兜底：取查询里的高频内容汉字（过滤常见虚词），命中即计入
        stop = set("的一了是自己是有和就在不上个这那也都很为到说被把")
        han_chars = [c for t in useful for c in t if HAN_RE.match(c) and c not in stop]
        freq: dict[str, int] = {}
        for c in han_chars:
            freq[c] = freq.get(c, 0) + 1
        singles = [c for c, _ in sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))[:6]]
        if singles:
            likes = " OR ".join(["p.prompt_text LIKE ?"] * len(singles))
            params = [f"%{c}%" for c in singles]
            for row in conn.execute(
                f"SELECT p.tweet_id, p.prompt_text FROM prompts p WHERE {likes}", params
            ):
                tid = str(row[0])
                merged[tid] = merged.get(tid, 0.0) + 0.5 * sum(1 for c in singles if c in str(row[1] or ""))
    if not merged:
        return {}
    top = dict(sorted(merged.items(), key=lambda kv: -kv[1])[:limit])
    peak = max(top.values()) or 1.0
    return {tid: score / peak for tid, score in top.items()}


def _narrative_fallback(conn: sqlite3.Connection, args: SimpleNamespace) -> list[dict[str, Any]]:
    """宽松二次检索：全部 must 降级 + n-gram 词召回 + 不强制配图，
    纯相关度排序，供严格结果过薄时兜底；失败返回空列表。"""
    try:
        intent = parse_intent(args.query)
        expanded = _narrative_search_terms(intent.lexical_terms)
        expanded_intent = replace(intent, lexical_terms=expanded)
        fts_scores = fts_rank_percentiles(conn, expanded_intent)
        if not fts_scores:
            # 精简语料库里 n-gram 词可能一个都召不回：LIKE 子串扫描兜底
            fts_scores = _like_fts_scores(conn, expanded)
        relaxed_intent = _relax_all_intent(expanded_intent)
        hits, _version = search_hits(
            conn, relaxed_intent, fts_scores=fts_scores,
            require_images=False,  # 兜底放宽配图要求：无图的真实提示词仍有参考价值
            author_filter=None, tool_filter=None,
            max_prompt_chars=args.max_prompt_chars, max_tags=args.max_tags,
            relaxed=["宽松模式：只按画面语义相关度排序"],
        )
        if not hits:
            print(f"[oip] 宽松兜底无结果：query={args.query!r} 召回词={len(expanded)} fts候选={len(fts_scores)}")
            return []
        selected = diversify_tiers([hits], args.limit)
        for item in selected:
            item["match_kind"] = "related"
            item["missing_constraints"] = []
        return selected
    except Exception as exc:
        print(f"[oip] 宽松兜底检索失败：{exc}")
        return []


def slim_results(payload: dict[str, Any], *, zh_chars: int = 600, prompt_chars: int = 400) -> dict[str, Any]:
    """把完整检索结果裁成给前端/LLM 的轻量版：保留可追溯性与中文翻译。"""
    def _trim(value: object, maximum: int) -> str:
        text = str(value or "").strip()
        return text if len(text) <= maximum else f"{text[: maximum - 1].rstrip()}…"

    def _slim(item: dict[str, Any]) -> dict[str, Any]:
        translations = item.get("translations") or {}
        return {
            "tweet_id": item.get("tweet_id"),
            "rank": item.get("rank"),
            "score": item.get("score"),
            "match_kind": item.get("match_kind") or "exact",
            "tool": item.get("tool"),
            "author": item.get("author"),
            "source_url": item.get("source_url") or item.get("tweet_url"),
            "prompt_text": _trim(item.get("prompt_text"), prompt_chars),
            "prompt_zh": _trim(translations.get("zh-Hans"), zh_chars),
            "missing_constraints": item.get("missing_constraints") or [],
            "tags": [
                {"id": tag.get("id"), "zh": (tag.get("display") or {}).get("zh-Hans") or tag.get("id")}
                for tag in (item.get("tags") or [])[:10]
            ],
        }

    return {
        "query": payload.get("query"),
        "active_taxonomy_version": payload.get("active_taxonomy_version"),
        "result_count": payload.get("result_count"),
        "exact_match_count": payload.get("exact_match_count"),
        "total_matches": payload.get("total_matches"),
        "related_count": payload.get("related_count"),
        "fallback": payload.get("fallback"),
        "results": [_slim(item) for item in (payload.get("results") or [])],
        "related_results": [_slim(item) for item in (payload.get("related_results") or [])],
    }


def reference_brief(
    query: str,
    *,
    limit: int = 3,
    zh_chars: int = 420,
) -> dict[str, Any] | None:
    """给提示词生成链路用的紧凑参考：检索 + 裁剪一步完成；任何失败返回 None（不拖垮主流程）。"""
    try:
        payload = search_corpus(query, limit=limit, max_prompt_chars=900, max_tags=8)
    except Exception as exc:
        print(f"[oip] 检索参考失败（降级跳过）：{exc}")
        return None
    slim = slim_results(payload, zh_chars=zh_chars, prompt_chars=320)
    refs = (slim.get("results") or []) or (slim.get("related_results") or [])
    if not refs:
        return None
    return {"query": query, "references": refs}

