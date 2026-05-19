import os
import json
from functools import wraps
from flask import (
    Blueprint, render_template, request,
    redirect, url_for, session, flash,
    jsonify, Response,
)

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "selvans2024")
BASE_DIR = os.path.dirname(__file__)

PROMPT_PATH = os.path.join(BASE_DIR, "prompts", "selvans.txt")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")


# ── Helpers ──────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin.login"))
        return f(*args, **kwargs)
    return decorated


def read_file(path: str, default: str = "") -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return default


def write_file(path: str, content: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "text_model": "claude-haiku-4-5-20251001",
            "image_model": "claude-sonnet-4-5-20251001",
            "max_history_turns": 20,
            "image_window_minutes": 30,
            "strict_kb_mode": False,
        }


def save_config(cfg: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ── Routes ───────────────────────────────────────────────────────────────────

@admin_bp.route("/")
@login_required
def dashboard():
    from app import debug_webhooks, recent_images, conversation_history
    import time
    stats = {
        "recent_webhooks": len(debug_webhooks),
        "tracked_images": len(recent_images),
        "active_users": len(conversation_history),
        "last_events": [],
    }
    for wb in reversed(debug_webhooks[-5:]):
        for evt in wb.get("events", []):
            msg = evt.get("message", {})
            stats["last_events"].append({
                "type": msg.get("type", "?"),
                "source": evt.get("source", {}).get("type", "?"),
            })
    cfg = load_config()
    return render_template("admin/dashboard.html", stats=stats, cfg=cfg)


@admin_bp.route("/login", methods=["GET", "POST"])
def login():
    if session.get("admin_logged_in"):
        return redirect(url_for("admin.dashboard"))
    error = None
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["admin_logged_in"] = True
            session.permanent = True
            return redirect(url_for("admin.dashboard"))
        error = "密碼錯誤，請再試一次。"
    return render_template("admin/login.html", error=error)


@admin_bp.route("/logout")
def logout():
    session.pop("admin_logged_in", None)
    return redirect(url_for("admin.login"))


@admin_bp.route("/prompt", methods=["GET", "POST"])
@login_required
def prompt():
    if request.method == "POST":
        content = request.form.get("content", "")
        write_file(PROMPT_PATH, content)
        # Reload system prompt in app
        import app as main_app
        main_app.SYSTEM_PROMPT = content
        flash("✅ 系統 Prompt 已儲存並立即生效！", "success")
        return redirect(url_for("admin.prompt"))
    content = read_file(PROMPT_PATH)
    return render_template("admin/prompt.html", content=content)


# ── Knowledge Base ────────────────────────────────────────────────────────────

@admin_bp.route("/knowledge")
@login_required
def knowledge():
    from knowledge_manager import get_kb_stats
    stats = get_kb_stats()
    cfg = load_config()
    return render_template("admin/knowledge.html",
                           stats=stats,
                           strict_mode=cfg.get("strict_kb_mode", False))


@admin_bp.route("/knowledge/upload", methods=["POST"])
@login_required
def knowledge_upload():
    from knowledge_manager import extract_text, add_document
    files = request.files.getlist("files")
    if not files:
        flash("⚠️ 請選擇至少一個檔案", "warning")
        return redirect(url_for("admin.knowledge"))

    success_count = 0
    errors = []
    for f in files:
        if not f.filename:
            continue
        try:
            file_bytes = f.read()
            if len(file_bytes) > 10 * 1024 * 1024:
                errors.append(f"{f.filename}：檔案超過 10 MB 限制")
                continue
            content = extract_text(file_bytes, f.filename)
            if not content.strip():
                errors.append(f"{f.filename}：無法提取文字內容")
                continue
            add_document(f.filename, content)
            success_count += 1
        except ValueError as e:
            errors.append(f"{f.filename}：{e}")
        except Exception as e:
            errors.append(f"{f.filename}：解析失敗（{type(e).__name__}）")

    if success_count:
        flash(f"✅ 成功上傳 {success_count} 份文件！", "success")
    for err in errors:
        flash(f"❌ {err}", "danger")

    return redirect(url_for("admin.knowledge"))


@admin_bp.route("/knowledge/delete/<doc_id>", methods=["POST"])
@login_required
def knowledge_delete(doc_id):
    from knowledge_manager import delete_document
    if delete_document(doc_id):
        flash("✅ 文件已刪除", "success")
    else:
        flash("⚠️ 找不到該文件", "warning")
    return redirect(url_for("admin.knowledge"))


@admin_bp.route("/knowledge/clear", methods=["POST"])
@login_required
def knowledge_clear():
    from knowledge_manager import clear_all
    count = clear_all()
    flash(f"✅ 已清空所有知識庫文件（共 {count} 份）", "success")
    return redirect(url_for("admin.knowledge"))


@admin_bp.route("/knowledge/export")
@login_required
def knowledge_export():
    from knowledge_manager import load_kb
    db = load_kb()
    data = json.dumps(db, ensure_ascii=False, indent=2)
    return Response(
        data,
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=knowledge_backup.json"}
    )


@admin_bp.route("/knowledge/import", methods=["POST"])
@login_required
def knowledge_import():
    from knowledge_manager import load_kb, save_kb
    f = request.files.get("backup_file")
    if not f or not f.filename:
        flash("⚠️ 請選擇備份 JSON 檔案", "warning")
        return redirect(url_for("admin.knowledge"))
    try:
        data = json.loads(f.read().decode("utf-8"))
        if "documents" not in data or not isinstance(data["documents"], list):
            raise ValueError("格式不正確")
        save_kb(data)
        flash(f"✅ 已匯入 {len(data['documents'])} 份文件", "success")
    except Exception as e:
        flash(f"❌ 匯入失敗：{e}", "danger")
    return redirect(url_for("admin.knowledge"))


# ── Settings ──────────────────────────────────────────────────────────────────

@admin_bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    cfg = load_config()
    if request.method == "POST":
        cfg["text_model"] = request.form.get("text_model", cfg["text_model"]).strip()
        cfg["image_model"] = request.form.get("image_model", cfg["image_model"]).strip()
        cfg["strict_kb_mode"] = "strict_kb_mode" in request.form
        try:
            cfg["max_history_turns"] = int(request.form.get("max_history_turns", 20))
            cfg["image_window_minutes"] = int(request.form.get("image_window_minutes", 30))
        except ValueError:
            flash("⚠️ 數值格式錯誤", "danger")
            return redirect(url_for("admin.settings"))
        save_config(cfg)
        # Apply to running app
        import app as main_app
        main_app.APP_CONFIG.update(cfg)
        flash("✅ 設定已儲存並立即生效！", "success")
        return redirect(url_for("admin.settings"))
    return render_template("admin/settings.html", cfg=cfg)


@admin_bp.route("/logs")
@login_required
def logs():
    from app import debug_webhooks
    return render_template("admin/logs.html", webhooks=list(reversed(debug_webhooks)))
