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
from datetime import datetime

import work_capture

def log(msg: str):
    print(f"[MONITOR] {msg}", flush=True, file=sys.stderr)


# ── 訊息暫存區：已經搬進資料庫 ────────────────────────────────────────────────
#
# 這裡以前有一個記憶體 buffer（buffer_message / _drain_buffer）。
# 2026-09-14 移除：這支跑在 Render 免費方案上，行程會休眠也會自己重啟，
# 實測 5 分鐘內就被換掉一次——囤在記憶體的訊息全部蒸發，
# 分析永遠等不到那一刻。現在 webhook 收到就寫進 pending_messages
# （見 work_capture.queue_message），這個模組只負責把佇列消化掉。


# ── Claude 預警分析 ───────────────────────────────────────────────────────────

_MONITOR_SYSTEM = """你是農場群組的智慧監控助理。看一批群組對話，同時做兩件事。

## 任務一：判斷有沒有需要老闆立即注意的狀況

需要發出預警的情況（舉例）：
- 植物病蟲害、大量死亡或異常生長
- 員工發生意外或緊急安全事故
- 客戶嚴重投訴、強烈不滿
- 重要進度嚴重落後（例如出貨、契作回報）
- 設備重大故障、重大損失

## 任務二：撈出「別人要思凡這邊做的事」

鐵則：

1. **只抓要求思凡／農場主人／小凡去做的事。** 夥伴之間互相拜託、
   對方說自己要做什麼、閒聊、報進度——都不算。寧可漏抓也不要亂抓，
   一份塞滿雜訊的清單第二天就沒人看了。
2. 一句話包含好幾件事就拆成好幾筆。
3. **raw 一定要是原句逐字**，不要改寫。日後要靠它回頭確認人家到底講了什麼。
4. 期限：對話裡有「明天」「這週」「月底前」就依照**該則訊息的日期**換算成
   YYYY-MM-DD。推不出來就留空字串，**不要猜一個日期**。
5. asker 抄訊息前面標示的說話者名稱，said_at 抄前面標的時間。
6. kind 只能挑：quote(報價) sample(送樣出貨) meeting(開會拜訪) doc(文件表單)
   data(數據報表) content(文案素材) visit(接待導覽) admin(行政雜務)
   decision(要老闆決定) other(其他)
7. blocking 填「要開始做之前還缺什麼」，沒有就留空。
8. urgency：對方明講很急或期限三天內＝high；有期限但不趕＝mid；沒期限＝low。

**輸出規則：只輸出 JSON，不要任何多餘文字或 markdown。**

{
  "alert": true/false,
  "severity": "low|medium|high",
  "summary": "一句話摘要（15字內）",
  "details": "詳細說明",
  "requests": [{
    "title": "要做什麼，一句話，30字內",
    "detail": "補充，沒有就空字串",
    "raw": "原句逐字",
    "asker": "誰交辦",
    "said_at": "YYYY-MM-DD HH:MM",
    "kind": "quote|sample|meeting|doc|data|content|visit|admin|decision|other",
    "project": "相關的專案／客戶／對象",
    "due": "YYYY-MM-DD 或空字串",
    "urgency": "high|mid|low",
    "blocking": "開始前還缺什麼",
    "confidence": "high|medium|low"
  }]
}

alert 為 false 時 severity/summary/details 給空字串即可。
沒有任何交辦時 requests 給空陣列。兩件事互不影響：
可以有預警沒交辦，也可以有交辦沒預警。"""

# 原本只判斷預警，200 就夠；現在同一次還要吐出交辦清單，不放寬會被截斷，
# 截斷的 JSON 解析失敗 → 連預警都一起沒了。
_MAX_TOKENS = 2000


def _analyze(claude_client, model: str, ctx_key: str, messages: list) -> dict | None:
    """把一批訊息送給 Claude，一次拿到預警判斷與交辦清單。"""
    # 帶上時間與顯示名稱：模型要靠這兩樣才推得出「明天」是哪一天、誰交辦的
    lines = []
    for m in messages:
        when = datetime.fromtimestamp(m.get("ts", time.time())
                                      ).strftime("%Y-%m-%d %H:%M")
        who = m.get("who") or f"用戶{m['user_id'][:6]}"
        lines.append(f"[{when}] {who}: {m['text']}")
    conversation = "\n".join(lines)

    try:
        resp = claude_client.messages.create(
            model=model,
            max_tokens=_MAX_TOKENS,
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

def _group_name(ctx_key: str, get_config) -> str:
    """設定檔裡有登記名稱就用，沒有就退回 group id。"""
    for g in (get_config() or {}).get("partner_groups", []) or []:
        if g.get("group_id") == ctx_key:
            return g.get("name", "") or ctx_key
    return ctx_key


def make_analyzer(claude_client, get_config, push_fn, profile_fn=None):
    """做出一個 analyze_fn(group_id, msgs)，給 work_capture.process_pending 用。

    一次呼叫同時處理兩件事：有預警就立刻推播，交辦則交給呼叫端存起來。
    兩條路共用同一次 API——訊息本來就要看一遍，沒道理看兩次。
    """
    def analyze(group_id: str, msgs: list) -> dict:
        cfg = get_config() or {}
        model = cfg.get("text_model", "claude-haiku-4-5-20251001")
        for m in msgs:
            if not m.get("who"):
                m["who"] = work_capture.resolve_name(
                    group_id, m.get("user_id", ""), profile_fn)
        log(f"Analyzing {len(msgs)} msgs from {group_id}")
        result = _analyze(claude_client, model, group_id, msgs) or {}
        if result.get("alert"):
            try:
                push_fn(_format_alert(group_id, result))
                log(f"Alert sent to boss: {result.get('summary')}")
            except Exception as e:
                log(f"Failed to send alert: {e}")
        if not cfg.get("capture_requests", True):
            result["requests"] = []
        return result

    return analyze


def start_monitor(claude_client, get_config, push_fn, profile_fn=None):
    """
    啟動背景監控執行緒（daemon thread，程式結束時自動終止）。

    ★ 2026-09-14 改寫：訊息不再囤在記憶體。這支跑在 Render 免費方案上，
      行程會休眠也會自己重啟，實測 5 分鐘內就被換掉一次——囤著的訊息
      全部蒸發，等於這條線永遠不會有東西。現在收到就寫進 pending_messages，
      這裡只負責「定期把佇列消化掉」，行程死幾次都不影響。
      真正保證會被處理的是 brand-db 按同步時那一下（見 app.py 的
      /internal/work-requests），這個執行緒只是讓預警不用等人按。

    參數：
        claude_client  : anthropic.Anthropic 實例
        get_config     : callable，回傳當前 APP_CONFIG dict
        push_fn        : callable(text: str)，發送預警私訊給老闆
        profile_fn     : callable(group_id, user_id) -> 顯示名稱（可省略）
    """
    analyze = make_analyzer(claude_client, get_config, push_fn, profile_fn)

    def _worker():
        log("Background monitor thread started ✓")
        while True:
            cfg      = get_config()
            interval = cfg.get("monitor_interval_seconds", 300)
            min_msgs = cfg.get("monitor_min_messages", 5)
            time.sleep(interval)

            try:
                pending = work_capture.pending_count()
                if pending < min_msgs:
                    # 不夠就先擺著。不會掉——躺在資料庫裡，
                    # 老闆按同步時一定會被處理到。
                    if pending:
                        log(f"佇列 {pending} 則，未達門檻 {min_msgs}，等下一輪")
                    continue
                work_capture.process_pending(
                    analyze,
                    group_name_fn=lambda g: _group_name(g, get_config))
            except Exception as e:
                log(f"worker error: {type(e).__name__}: {e}")

    t = threading.Thread(target=_worker, daemon=True, name="monitor-thread")
    t.start()
    log("Monitor thread launched")
    return t
