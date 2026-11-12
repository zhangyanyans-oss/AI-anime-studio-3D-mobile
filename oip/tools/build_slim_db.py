#!/usr/bin/env python3
"""从完整 prompts.db 抽取「主要风格」精简库，供随项目发布。

策略：
- 以 visual_style 维度的 25 个标签做分层抽样，每个标签保留 quota 条
- 候选条件：有配图、有中文翻译、prompt 长度适中（200~6000 字符）
- 优先保留标签丰富的条目（检索打分价值高），同分随机打散（固定种子，可复现）
- 表结构/FTS 全部按原库重建，library.py 零改动可用
- 只读源库，绝不修改；输出到 oip/data/prompts.db

用法：python oip/tools/build_slim_db.py [--source PATH] [--quota 80]
"""
from __future__ import annotations

import argparse
import random
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = Path(r"C:/Users/ZYY/Downloads/open-image-prompts/open-image-prompts/.oip/runtime/prompts.db")
DEFAULT_TARGET = PACKAGE_DIR / "data" / "prompts.db"

FULL_TABLES = ["archive_config", "label_dimensions", "labels", "taxonomy_labels"]
SUBSET_TABLES = ["prompts", "prompt_labels", "media_labels", "prompt_translations", "images"]


def create_table(conn: sqlite3.Connection, source: sqlite3.Connection, name: str) -> str:
    sql = source.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()[0]
    conn.execute(sql)
    return sql


def main() -> int:
    parser = argparse.ArgumentParser(description="Build slim prompts.db by stratified style sampling")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument("--quota", type=int, default=80, help="每个 visual_style 标签保留的条数")
    parser.add_argument("--subject-quota", type=int, default=25, help="每个 subject_type 标签保留的条数（提升常见主体名词召回）")
    parser.add_argument("--subject-top", type=int, default=40, help="取样本量最大的前 N 个 subject_type 标签")
    args = parser.parse_args()

    if not args.source.is_file():
        print(f"源库不存在：{args.source}")
        return 1
    rng = random.Random(20261112)

    with closing(sqlite3.connect(f"file:{args.source.resolve().as_posix()}?mode=ro&immutable=1", uri=True)) as src:
        src.row_factory = sqlite3.Row
        # 1) 分层抽样：每个 visual_style 标签挑 quota 条
        style_labels = [
            r[0] for r in src.execute(
                """SELECT l.id FROM labels l
                   JOIN label_dimensions ld ON ld.id=l.dimension_id
                   WHERE ld.key='visual_style'"""
            )
        ]
        picked: set[str] = set()
        per_label = {}

        def sample_by_label(label_ids: list[int], quota: int) -> None:
            for label_id in label_ids:
                rows = src.execute(
                    """
                    SELECT DISTINCT pl.tweet_id,
                           (SELECT count(*) FROM prompt_labels x
                             WHERE x.tweet_id=pl.tweet_id AND x.taxonomy_version='oip-visual-v2') AS richness,
                           length(p.prompt_text) AS plen
                    FROM prompt_labels pl
                    JOIN prompts p ON p.tweet_id=pl.tweet_id
                    WHERE pl.label_id=? AND pl.taxonomy_version='oip-visual-v2'
                      AND length(p.prompt_text) BETWEEN 200 AND 6000
                      AND EXISTS (SELECT 1 FROM images i WHERE i.tweet_id=pl.tweet_id)
                      AND EXISTS (SELECT 1 FROM prompt_translations t WHERE t.tweet_id=pl.tweet_id)
                    """,
                    (label_id,),
                ).fetchall()
                # 打分：标签丰富度高优先；长度 400~2500 加分；再随机打散
                scored = []
                for r in rows:
                    s = r["richness"] * 2.0 + (2.0 if 400 <= r["plen"] <= 2500 else 0.0) + rng.random()
                    scored.append((s, r["tweet_id"]))
                scored.sort(reverse=True)
                picked.update(tid for _, tid in scored[:quota])

        # 1) 按视觉风格分层（主层）
        style_labels = [
            r[0] for r in src.execute(
                """SELECT l.id FROM labels l
                   JOIN label_dimensions ld ON ld.id=l.dimension_id
                   WHERE ld.key='visual_style'"""
            )
        ]
        sample_by_label(style_labels, args.quota)
        # 2) 按主体类型分层（补充层：提升「猫/城市/森林」等常见名词的召回）
        subject_labels = [
            r[0] for r in src.execute(
                f"""
                SELECT l.id FROM labels l
                JOIN label_dimensions ld ON ld.id=l.dimension_id
                WHERE ld.key='subject_type'
                  AND (SELECT count(DISTINCT pl.tweet_id) FROM prompt_labels pl
                       WHERE pl.label_id=l.id AND pl.taxonomy_version='oip-visual-v2') >= 20
                ORDER BY (SELECT count(DISTINCT pl.tweet_id) FROM prompt_labels pl
                          WHERE pl.label_id=l.id AND pl.taxonomy_version='oip-visual-v2') DESC
                LIMIT {int(args.subject_top)}
                """
            )
        ]
        sample_by_label(subject_labels, args.subject_quota)
        tweet_ids = sorted(picked)
        print(f"抽样完成：风格层 {len(style_labels)} 标签×≤{args.quota} + 主体层 {len(subject_labels)} 标签×≤{args.subject_quota} → 共 {len(tweet_ids)} 条 prompt")
        ph = ",".join("?" * len(tweet_ids))

        # 2) 建目标库
        args.target.parent.mkdir(parents=True, exist_ok=True)
        tmp = args.target.with_suffix(".db.tmp")
        if tmp.exists():
            tmp.unlink()
        with closing(sqlite3.connect(tmp)) as dst:
            dst.execute("PRAGMA page_size=4096")
            dst.execute("PRAGMA journal_mode=OFF")
            dst.execute("PRAGMA synchronous=OFF")
            for name in FULL_TABLES:
                create_table(dst, src, name)
                cols = len(src.execute(f"PRAGMA table_info([{name}])").fetchall())
                dst.executemany(
                    f"INSERT INTO [{name}] VALUES ({','.join('?' * cols)})",
                    src.execute(f"SELECT * FROM [{name}]").fetchall(),
                )
            for name in SUBSET_TABLES:
                create_table(dst, src, name)
                cols = len(src.execute(f"PRAGMA table_info([{name}])").fetchall())
                rows = src.execute(f"SELECT * FROM [{name}] WHERE tweet_id IN ({ph})", tweet_ids).fetchall()
                dst.executemany(
                    f"INSERT INTO [{name}] VALUES ({','.join('?' * cols)})",
                    [tuple(r) for r in rows],
                )
                print(f"  {name}: {len(rows)} 行")
            # 3) 重建 FTS（沿用原建表 SQL）
            fts_sql = src.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='prompt_fts'"
            ).fetchone()[0]
            dst.execute(fts_sql)
            fts_rows = src.execute(
                f"""
                SELECT p.tweet_id, p.prompt_text, p.author, p.tool,
                       (SELECT group_concat(ld.key || ':' || l.key, ',')
                         FROM (
                           SELECT pl.label_id FROM prompt_labels pl
                           WHERE pl.tweet_id=p.tweet_id AND pl.taxonomy_version='oip-visual-v2'
                         ) plx
                         JOIN labels l ON l.id=plx.label_id
                         JOIN label_dimensions ld ON ld.id=l.dimension_id
                         ORDER BY ld.id, l.id) AS labels_text
                FROM prompts p WHERE p.tweet_id IN ({ph})
                """,
                tweet_ids,
            ).fetchall()
            dst.executemany(
                "INSERT INTO prompt_fts(tweet_id, prompt_text, author, tool, labels_text) VALUES (?,?,?,?,?)",
                [tuple(r) for r in fts_rows],
            )
            print(f"  prompt_fts: {len(fts_rows)} 行")
            dst.commit()
            dst.execute("VACUUM")
            dst.commit()

    size_mb = tmp.stat().st_size / 1024 / 1024
    tmp.replace(args.target)
    print(f"完成：{args.target}  {size_mb:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
