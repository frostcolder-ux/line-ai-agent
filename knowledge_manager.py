"""
Knowledge Base Manager
雙模式：有 DATABASE_URL → PostgreSQL（資料永久保存）；無 → 本機 JSON 檔。
"""
import os
import json
import uuid
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(__file__)
KB_DIR   = os.path.join(BASE_DIR, "knowledge_db")
KB_PATH  = os.path.join(KB_DIR, "db.json")


def log(msg):
    print(f"[KB] {msg}", file=sys.stderr, flush=True)


# ══════════════════════════════════════════════════════════════════════════════
# PostgreSQL 實作
# ══════════════════════════════════════════════════════════════════════════════

def _pg_load_kb() -> dict:
    from db import get_conn
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute(
        "SELECT id, filename, content, chars, uploaded_at, preview "
        "FROM knowledge_docs ORDER BY uploaded_at"
    )
    rows = cur.fetchall()
    cur.close(); conn.close()
    docs = [
        {"id": r[0], "filename": r[1], "content": r[2],
         "chars": r[3], "uploaded_at": r[4], "preview": r[5]}
        for r in rows
    ]
    return {"documents": docs}


def _pg_add_document(filename: str, content: str) -> dict:
    from db import get_conn
    doc_id  = str(uuid.uuid4())[:8]
    now     = datetime.now().strftime("%Y-%m-%d %H:%M")
    preview = content[:200].strip()
    chars   = len(content)
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute(
        "INSERT INTO knowledge_docs (id, filename, content, chars, uploaded_at, preview) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (doc_id, filename, content, chars, now, preview)
    )
    conn.commit(); cur.close(); conn.close()
    log(f"PG: added {filename} ({chars} chars)")
    return {"id": doc_id, "filename": filename, "content": content,
            "chars": chars, "uploaded_at": now, "preview": preview}


def _pg_delete_document(doc_id: str) -> bool:
    from db import get_conn
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("DELETE FROM knowledge_docs WHERE id = %s", (doc_id,))
    deleted = cur.rowcount > 0
    conn.commit(); cur.close(); conn.close()
    return deleted


def _pg_clear_all() -> int:
    from db import get_conn
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM knowledge_docs")
    count = cur.fetchone()[0]
    cur.execute("DELETE FROM knowledge_docs")
    conn.commit(); cur.close(); conn.close()
    return count


def _pg_get_all_content() -> str:
    from db import get_conn
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("SELECT filename, content FROM knowledge_docs ORDER BY uploaded_at")
    rows = cur.fetchall()
    cur.close(); conn.close()
    if not rows:
        return ""
    parts = [f"### 文件來源：{r[0]}\n{r[1]}" for r in rows]
    return "\n\n---\n\n".join(parts)


# ══════════════════════════════════════════════════════════════════════════════
# JSON 檔實作（本地開發 / 無 DB 備援）
# ══════════════════════════════════════════════════════════════════════════════

def _json_load_kb() -> dict:
    try:
        with open(KB_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"documents": []}


def _json_save_kb(db: dict):
    os.makedirs(KB_DIR, exist_ok=True)
    with open(KB_PATH, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


def _json_add_document(filename: str, content: str) -> dict:
    db  = _json_load_kb()
    doc = {
        "id":          str(uuid.uuid4())[:8],
        "filename":    filename,
        "content":     content,
        "chars":       len(content),
        "uploaded_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "preview":     content[:200].strip(),
    }
    db["documents"].append(doc)
    _json_save_kb(db)
    return doc


def _json_delete_document(doc_id: str) -> bool:
    db     = _json_load_kb()
    before = len(db["documents"])
    db["documents"] = [d for d in db["documents"] if d["id"] != doc_id]
    if len(db["documents"]) < before:
        _json_save_kb(db)
        return True
    return False


def _json_clear_all() -> int:
    db    = _json_load_kb()
    count = len(db["documents"])
    _json_save_kb({"documents": []})
    return count


def _json_get_all_content() -> str:
    db = _json_load_kb()
    if not db["documents"]:
        return ""
    parts = [f"### 文件來源：{d['filename']}\n{d['content']}" for d in db["documents"]]
    return "\n\n---\n\n".join(parts)


# ══════════════════════════════════════════════════════════════════════════════
# 公開 API（自動選擇模式）
# ══════════════════════════════════════════════════════════════════════════════

def _use_pg() -> bool:
    from db import is_postgres
    return is_postgres()


def load_kb() -> dict:
    return _pg_load_kb() if _use_pg() else _json_load_kb()


def save_kb(db: dict):
    """匯入備份用（只支援 JSON 模式，PG 模式請用 add_document）"""
    if _use_pg():
        _pg_clear_all()
        for doc in db.get("documents", []):
            _pg_add_document(doc["filename"], doc["content"])
    else:
        _json_save_kb(db)


def add_document(filename: str, content: str) -> dict:
    return _pg_add_document(filename, content) if _use_pg() else _json_add_document(filename, content)


def delete_document(doc_id: str) -> bool:
    return _pg_delete_document(doc_id) if _use_pg() else _json_delete_document(doc_id)


def clear_all() -> int:
    return _pg_clear_all() if _use_pg() else _json_clear_all()


def get_all_content() -> str:
    try:
        return _pg_get_all_content() if _use_pg() else _json_get_all_content()
    except Exception as e:
        log(f"get_all_content error: {e}")
        return ""


def get_kb_stats() -> dict:
    db   = load_kb()
    docs = db["documents"]
    total_chars = sum(d.get("chars", 0) for d in docs)
    return {
        "document_count":   len(docs),
        "total_chars":      total_chars,
        "estimated_tokens": total_chars // 4,
        "documents":        docs,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 文字解析
# ══════════════════════════════════════════════════════════════════════════════

SUPPORTED_EXTENSIONS = {"txt", "md", "pdf", "docx", "csv"}


def extract_text(file_bytes: bytes, filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"不支援的格式 .{ext}（支援：PDF、DOCX、TXT、MD、CSV）")

    if ext in ("txt", "md"):
        for enc in ("utf-8", "utf-8-sig", "big5", "gbk"):
            try:
                return file_bytes.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return file_bytes.decode("utf-8", errors="replace")

    if ext == "pdf":
        try:
            from pypdf import PdfReader
            import io
            reader = PdfReader(io.BytesIO(file_bytes))
            pages  = []
            for i, page in enumerate(reader.pages):
                text = page.extract_text() or ""
                if text.strip():
                    pages.append(f"[第 {i+1} 頁]\n{text.strip()}")
            if not pages:
                raise ValueError("PDF 中未能提取到文字（可能是掃描圖檔，需 OCR）")
            return "\n\n".join(pages)
        except ImportError:
            raise ValueError("伺服器尚未安裝 pypdf")
        except Exception as e:
            raise ValueError(f"PDF 解析失敗：{e}")

    if ext == "docx":
        try:
            from docx import Document
            import io
            doc   = Document(io.BytesIO(file_bytes))
            paras = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
            table_rows = []
            for table in doc.tables:
                for row in table.rows:
                    cells = [c.text.strip() for c in row.cells if c.text.strip()]
                    if cells:
                        table_rows.append(" | ".join(cells))
            all_text = paras + (["[表格資料]"] + table_rows if table_rows else [])
            return "\n\n".join(all_text)
        except ImportError:
            raise ValueError("伺服器尚未安裝 python-docx")
        except Exception as e:
            raise ValueError(f"DOCX 解析失敗：{e}")

    if ext == "csv":
        for enc in ("utf-8-sig", "utf-8", "big5"):
            try:
                return file_bytes.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return file_bytes.decode("utf-8", errors="replace")

    return ""
