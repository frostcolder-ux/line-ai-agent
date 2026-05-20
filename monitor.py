"""
背景監控模組 — Background Monitoring & Alert

設計重點（解決 Webhook 3 秒超時問題）：
─────────────────────────────────────────
LINE 要求 Webhook 必須在 3 秒內回應 200 OK，否則視為失敗。
本模組的所有 Claude API 呼叫都在「獨立背景執行緒」中進行：

  Webhook 請求進來
      │
      ├─ buffer_message()   ← 只是 list.append()，< 0.1ms，立刻返回
      │
      └─ 回傳 "OK" 給 LINE  ← 完全不等待 AI

獨立執行緒（每 N 秒）：
  取出 buffer → 呼叫 Claude（可能 2-5 秒）→ 發送 Push → 清空 buffer

執行緒與主 Flask 程序完全解耦，互不影響。
"""
import sys
import json
import threading
import time
from collections import defaultdict

def log(msg: str):
    print(f"[MONITOR] {msg}", flush=True, file=sys.stderr)


# ── 訊息暫存區 ────────────────────────────────────────────────────────────────
# 結構：{ctx_key: [{"user_id": str, "text": str, "ts": float}, ...]}
_buffer: dict[str, list] = defaultdict(list)
_lock = threading.Lock()  # 保護 _buffer 的執行緒安全


def buffer_message(ctx_key: str, user_id: str, text: str):
    """
    把訊息加入監控暫存區。
    在 Webhook handler 中呼叫，必須極快（< 1ms）。
    只做 list.append，不做任何 I/O 或網路請求。
    """
    with _lock:
        _buffer[ctx_key].append({
            "user_id": user_id,
            "text": text[:200],  # 截斷過長的訊息
            "ts": time.time(),
        })


def _drain_buffer() -> dict[str, list]:
    """原子性取出所有訊息並清空 buffer（執行緒安全）。"""
    with _lock:
        snapshot = {k: list(v) for k, v in _buffer.items() if v}
        for k in snapshot:
            _buffer[k] = []
        return snapshot


# ── Claude 預警分析 ───────────────────────────────────────────────────────────

_MONITOR_SYSTEM = """你是農場群組的智慧監控助理，負責分析群組對話，判斷是否有需要老闆立即注意的狀況。

需要發出預警的情況（舉例）：
- 植物病蟲害、大量死亡或異常生長
- 員工發生意外或緊急安全事故
- 客戶嚴重投訴、強烈不滿
- 重要進度嚴重落後（例如出貨、契作回報）
- 設備重大故障、重大損失

**輸出規則：只輸出 JSON，不要任何多餘文字或 markdown。**

有預警時：
{"alert": true, "severity": "low|medium|high", "summary": "一句話摘要（15字內）", "details": "詳細說明"}

無預警時：
{"alert": false}"""


def _analyze(claude_client, model: str, ctx_key: str, messages: list) -> dict | None:
    """把一批訊息送給 Claude 分析，回傳預警 dict 或 None。"""
    # 組合對話文字
    lines = [f"[用戶{m['user_id'][:6]}]: {m['text']}" for m in messages]
    conversation = "\n".join(lines)

    try:
        resp = claude_client.messages.create(
            model=model,
            max_tokens=200,
            system=_MONITOR_SYSTEM,
            messages=[{
                "role": "user",
                "content": f"群組 {ctx_key} 的最近對話：\n\n{conversation}",
            }],
        )
        raw = resp.content[0].text.strip()
        log(f"Monitor raw response: {raw[:100]}")
        return json.loads(raw)
    except json.JSONDecodeError:
        log("Monitor: Claude returned non-JSON, ignoring")
        return None
    except Exception as e:
        log(f"Monitor analysis error: {type(e).__name__}: {e}")
        return None


def _format_alert(ctx_key: str, alert: dict) -> str:
    """把 alert dict 格式化成 LINE 訊息文字。"""
    emoji = {"low": "🟡", "medium": "🟠", "high": "🔴"}.get(
        alert.get("severity", "low"), "⚠️"
    )
    return (
        f"{emoji}【小凡預警】\n"
        f"來源群組：{ctx_key}\n"
        f"嚴重程度：{alert.get('severity', '?').upper()}\n"
        f"摘要：{alert.get('summary', '')}\n"
        f"詳情：{alert.get('details', '')}"
    )


# ── 背景監控執行緒 ────────────────────────────────────────────────────────────

def start_monitor(claude_client, get_config, push_fn):
    """
    啟動背景監控執行緒（daemon thread，程式結束時自動終止）。

    參數：
        claude_client  : anthropic.Anthropic 實例
        get_config     : callable，回傳當前 APP_CONFIG dict（每次都重新讀，支援動態設定）
        push_fn        : callable(target_id: str, text: str)，發送 LINE Push Message
    """

    def _worker():
        log("Background monitor thread started ✓")
        while True:
            cfg = get_config()
            interval  = cfg.get("monitor_interval_seconds", 300)
            min_msgs  = cfg.get("monitor_min_messages", 5)
            boss_id   = cfg.get("boss_user_id", "").strip()
            model     = cfg.get("text_model", "claude-3-5-haiku-20241022")

            # 等待下一個分析週期
            time.sleep(interval)

            if not boss_id:
                # 尚未設定老闆 User ID，不發送預警
                continue

            snapshot = _drain_buffer()
            if not snapshot:
                continue

            for ctx_key, msgs in snapshot.items():
                if len(msgs) < min_msgs:
                    log(f"Skip {ctx_key}: {len(msgs)} msgs < threshold {min_msgs}")
                    continue

                log(f"Analyzing {len(msgs)} msgs from {ctx_key}")
                result = _analyze(claude_client, model, ctx_key, msgs)

                if result and result.get("alert"):
                    text = _format_alert(ctx_key, result)
                    try:
                        push_fn(boss_id, text)
                        log(f"Alert pushed to boss: {result.get('summary')}")
                    except Exception as e:
                        log(f"Failed to push alert: {e}")

    t = threading.Thread(target=_worker, daemon=True, name="monitor-thread")
    t.start()
    log("Monitor thread launched")
    return t
