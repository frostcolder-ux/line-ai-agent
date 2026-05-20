"""
資料庫連線模組
支援雙模式：
  - 有 DATABASE_URL → 使用 PostgreSQL（資料永久保存，不受部署影響）
  - 無 DATABASE_URL → 使用本機 JSON 檔（本地開發用）

使用方式：
  from db import is_postgres, get_conn
"""
import os
import sys

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()


def is_postgres() -> bool:
    """判斷是否使用 PostgreSQL 模式。"""
    return bool(DATABASE_URL)


def get_conn():
    """
    取得 PostgreSQL 連線。
    注意：用完請手動 close()，或用 with 語法。
    """
    import psycopg2
    return psycopg2.connect(DATABASE_URL)


def init_db():
    """
    初始化資料庫 schema，若 table 已存在則跳過。
    在 app 啟動時呼叫一次即可。
    """
    if not is_postgres():
        print("[DB] No DATABASE_URL, using local JSON files", file=sys.stderr, flush=True)
        return

    sql = """
        CREATE EXTENSION IF NOT EXISTS vector;

        CREATE TABLE IF NOT EXISTS knowledge_docs (
            id          VARCHAR(8)   PRIMARY KEY,
            filename    TEXT         NOT NULL,
            content     TEXT         NOT NULL,
            chars       INTEGER      NOT NULL,
            uploaded_at TEXT         NOT NULL,
            preview     TEXT         NOT NULL
        );

        CREATE TABLE IF NOT EXISTS knowledge_chunks (
            id          SERIAL       PRIMARY KEY,
            doc_id      TEXT         NOT NULL,
            filename    TEXT         NOT NULL,
            chunk_idx   INTEGER      NOT NULL,
            content     TEXT         NOT NULL,
            embedding   vector(512),
            created_at  TEXT         NOT NULL
        );

        CREATE TABLE IF NOT EXISTS farm_harvests (
            id          SERIAL       PRIMARY KEY,
            location    TEXT         NOT NULL,
            crop        TEXT         NOT NULL,
            amount      REAL         NOT NULL,
            unit        TEXT         NOT NULL,
            timestamp   TEXT         NOT NULL
        );

        CREATE TABLE IF NOT EXISTS farm_tasks (
            id          SERIAL       PRIMARY KEY,
            description TEXT         NOT NULL,
            deadline    TEXT         NOT NULL,
            status      TEXT         NOT NULL DEFAULT 'pending',
            created_at  TEXT         NOT NULL
        );
    """
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(sql)
        conn.commit()
        cur.close()
        conn.close()
        print("[DB] PostgreSQL tables ready ✓", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[DB] init_db error: {e}", file=sys.stderr, flush=True)
