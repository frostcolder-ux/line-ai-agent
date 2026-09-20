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

        -- 收到但還沒分析的群組訊息。
        -- ★ 為什麼不放記憶體：Render 免費方案會休眠、也會自己重啟，
        --   行程一沒就把暫存沖掉。2026-09-14 實測 5 分鐘內就被清一次，
        --   「安靜群組囤 30 分鐘再分析」的設計在這種環境下永遠等不到。
        CREATE TABLE IF NOT EXISTS pending_messages (
            id         SERIAL  PRIMARY KEY,
            group_id   TEXT    NOT NULL DEFAULT '',
            group_name TEXT    NOT NULL DEFAULT '',
            user_id    TEXT    NOT NULL DEFAULT '',
            who        TEXT    NOT NULL DEFAULT '',
            text       TEXT    NOT NULL DEFAULT '',
            said_at    TEXT    NOT NULL DEFAULT '',
            processed  INTEGER NOT NULL DEFAULT 0,
            created_at TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_pending_unprocessed
            ON pending_messages(processed, group_id);

        -- 群組裡抓到的交辦。小凡本來就看得到每一則訊息，
        -- 以前分析完就丟掉；留下來之後由老闆本機的 brand-db 來拉。
        -- fingerprint 唯一：同一則訊息被分析兩次也只會有一筆。
        CREATE TABLE IF NOT EXISTS captured_requests (
            id          SERIAL       PRIMARY KEY,
            group_id    TEXT         NOT NULL DEFAULT '',
            group_name  TEXT         NOT NULL DEFAULT '',
            asker       TEXT         NOT NULL DEFAULT '',
            said_at     TEXT         NOT NULL DEFAULT '',
            raw         TEXT         NOT NULL DEFAULT '',
            title       TEXT         NOT NULL DEFAULT '',
            detail      TEXT         NOT NULL DEFAULT '',
            kind        TEXT         NOT NULL DEFAULT 'other',
            project     TEXT         NOT NULL DEFAULT '',
            due         TEXT         NOT NULL DEFAULT '',
            urgency     TEXT         NOT NULL DEFAULT 'mid',
            blocking    TEXT         NOT NULL DEFAULT '',
            confidence  TEXT         NOT NULL DEFAULT '',
            fingerprint TEXT         NOT NULL UNIQUE,
            created_at  TEXT         NOT NULL
        );

        -- 哪些群組可以使喚小凡（見 access.py）。
        -- 沒有這張表以前，任何人都能把小凡拉進自己的群組，它就會開始推播與監聽。
        CREATE TABLE IF NOT EXISTS line_access (
            group_id   TEXT PRIMARY KEY,
            name       TEXT NOT NULL DEFAULT '',
            code       TEXT NOT NULL DEFAULT '',
            status     TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT '',
            decided_at TEXT NOT NULL DEFAULT ''
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
