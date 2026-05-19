"""
Knowledge Base Manager
Handles file upload, text extraction, storage and retrieval.
All documents stored in knowledge_db/db.json (persists between restarts).
"""

import os
import json
import uuid
from datetime import datetime

BASE_DIR = os.path.dirname(__file__)
KB_DIR = os.path.join(BASE_DIR, "knowledge_db")
KB_PATH = os.path.join(KB_DIR, "db.json")


# ── Storage ──────────────────────────────────────────────────────────────────

def load_kb() -> dict:
    try:
        with open(KB_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"documents": []}


def save_kb(db: dict):
    os.makedirs(KB_DIR, exist_ok=True)
    with open(KB_PATH, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


# ── CRUD ─────────────────────────────────────────────────────────────────────

def add_document(filename: str, content: str) -> dict:
    db = load_kb()
    doc = {
        "id": str(uuid.uuid4())[:8],
        "filename": filename,
        "content": content,
        "chars": len(content),
        "uploaded_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "preview": content[:200].strip(),
    }
    db["documents"].append(doc)
    save_kb(db)
    return doc


def delete_document(doc_id: str) -> bool:
    db = load_kb()
    before = len(db["documents"])
    db["documents"] = [d for d in db["documents"] if d["id"] != doc_id]
    if len(db["documents"]) < before:
        save_kb(db)
        return True
    return False


def clear_all() -> int:
    db = load_kb()
    count = len(db["documents"])
    save_kb({"documents": []})
    return count


def get_kb_stats() -> dict:
    db = load_kb()
    total_chars = sum(d.get("chars", 0) for d in db["documents"])
    return {
        "document_count": len(db["documents"]),
        "total_chars": total_chars,
        "estimated_tokens": total_chars // 4,
        "documents": db["documents"],
    }


def get_all_content() -> str:
    """Return all KB content formatted for injection into system prompt."""
    db = load_kb()
    if not db["documents"]:
        return ""
    parts = []
    for doc in db["documents"]:
        parts.append(f"### 文件來源：{doc['filename']}\n{doc['content']}")
    return "\n\n---\n\n".join(parts)


# ── Text Extraction ──────────────────────────────────────────────────────────

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
            pages = []
            for i, page in enumerate(reader.pages):
                text = page.extract_text() or ""
                if text.strip():
                    pages.append(f"[第 {i + 1} 頁]\n{text.strip()}")
            if not pages:
                raise ValueError("PDF 中未能提取到文字（可能是掃描圖檔，需 OCR）")
            return "\n\n".join(pages)
        except ImportError:
            raise ValueError("伺服器尚未安裝 pypdf，請重新部署")
        except Exception as e:
            raise ValueError(f"PDF 解析失敗：{e}")

    if ext == "docx":
        try:
            from docx import Document
            import io
            doc = Document(io.BytesIO(file_bytes))
            paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
            tables_text = []
            for table in doc.tables:
                for row in table.rows:
                    cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                    if cells:
                        tables_text.append(" | ".join(cells))
            all_text = paragraphs + (["[表格資料]"] + tables_text if tables_text else [])
            return "\n\n".join(all_text)
        except ImportError:
            raise ValueError("伺服器尚未安裝 python-docx，請重新部署")
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
