import os
import json
from functools import wraps
from flask import (
    Blueprint, render_template, request,
    redirect, url_for, session, flash,
)

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "selvans2024")
BASE_DIR = os.path.dirname(__file__)

PROMPT_PATH = os.path.join(BASE_DIR, "prompts", "selvans.txt")
KB_PATH = os.path.join(BASE_DIR, "prompts", "knowledge_base.txt")
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


@admin_bp.route("/knowledge", methods=["GET", "POST"])
@login_required
def knowledge():
    if request.method == "POST":
        content = request.form.get("content", "")
        write_file(KB_PATH, content)
        flash("✅ 知識庫已儲存並立即生效！", "success")
        return redirect(url_for("admin.knowledge"))
    content = read_file(KB_PATH)
    return render_template("admin/knowledge.html", content=content)


@admin_bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    cfg = load_config()
    if request.method == "POST":
        cfg["text_model"] = request.form.get("text_model", cfg["text_model"]).strip()
        cfg["image_model"] = request.form.get("image_model", cfg["image_model"]).strip()
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
