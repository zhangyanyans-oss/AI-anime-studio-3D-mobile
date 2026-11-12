"""Read-only access to the Open Image Prompts SQLite archive (ported for AI-anime-studio).

DB 查找顺序：
1. 环境变量 OIP_DB_PATH（.env 里配置，指向任意一份 prompts.db，例如 open-image-prompts 仓库解压出的库）
2. 本包内 oip/data/prompts.db（自带库，若存在则优先级第二）
3. oip/data/prompts.db.gz 存在时自动解压展开（首次约需十几秒）
数据库始终只读打开（mode=ro&immutable=1），绝不写入。
"""
from __future__ import annotations

import gzip
import os
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = PACKAGE_DIR / "data" / "prompts.db"
DEFAULT_ARCHIVE_PATH = PACKAGE_DIR / "data" / "prompts.db.gz"


def env_db_path() -> Path | None:
    raw = (os.getenv("OIP_DB_PATH") or "").strip().strip('"')
    if raw and Path(raw).is_file():
        return Path(raw)
    return None


def resolve_db_path() -> Path | None:
    """返回当前可用的 prompts.db 路径；不存在则尝试从 gz 展开；都失败返回 None。"""
    found = env_db_path()
    if found:
        return found
    if DEFAULT_DB_PATH.is_file():
        return DEFAULT_DB_PATH
    if DEFAULT_ARCHIVE_PATH.is_file():
        try:
            return ensure_working_database(DEFAULT_DB_PATH, DEFAULT_ARCHIVE_PATH)
        except Exception as exc:  # 展开失败不拖垮主服务
            print(f"[oip] 展开 prompts.db.gz 失败：{exc}")
            return None
    return None


def _fingerprint(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def ensure_working_database(
    db_path: Path = DEFAULT_DB_PATH,
    archive_path: Path = DEFAULT_ARCHIVE_PATH,
) -> Path:
    """Expand the versioned archive once and refresh it when the archive changes."""
    db_path = Path(db_path)
    archive_path = Path(archive_path)
    if not archive_path.is_file():
        raise FileNotFoundError(f"SQLite archive not found at {archive_path}")
    with archive_path.open("rb") as archive:
        if archive.read(32).startswith(b"version https://git-lfs"):
            raise RuntimeError("The SQLite archive is still a Git LFS pointer.")

    fingerprint = _fingerprint(archive_path)
    stamp_path = db_path.with_suffix(f"{db_path.suffix}.source")
    try:
        if db_path.is_file() and stamp_path.read_text(encoding="utf-8").strip() == fingerprint:
            return db_path
    except OSError:
        pass

    db_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = db_path.with_name(f"{db_path.name}.{os.getpid()}.tmp")
    temporary_stamp = stamp_path.with_name(f"{stamp_path.name}.{os.getpid()}.tmp")
    try:
        with gzip.open(archive_path, "rb") as source, temporary.open("wb") as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)
        with closing(sqlite3.connect(temporary)) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                raise RuntimeError("expanded SQLite archive failed integrity_check")
        os.replace(temporary, db_path)
        temporary_stamp.write_text(f"{fingerprint}\n", encoding="utf-8")
        os.replace(temporary_stamp, stamp_path)
    finally:
        temporary.unlink(missing_ok=True)
        temporary_stamp.unlink(missing_ok=True)
    return db_path


def connect_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{Path(path).resolve().as_posix()}?mode=ro&immutable=1",
        uri=True,
        timeout=5,
    )
    connection.row_factory = sqlite3.Row
    return connection


def table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def active_taxonomy_version(connection: sqlite3.Connection) -> str:
    if not table_exists(connection, "archive_config"):
        return "legacy"
    row = connection.execute(
        "SELECT value FROM archive_config WHERE key='active_taxonomy_version'"
    ).fetchone()
    return str(row[0]) if row else "legacy"
