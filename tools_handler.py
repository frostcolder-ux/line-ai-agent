"""
Function Calling (Tool Use) — 工具定義與執行
讓小凡可以把自然語言指令轉為結構化操作。

流程：
1. 呼叫 Claude 時附帶 tools 參數
2. Claude 回傳 stop_reason = "tool_use" 時，取出工具名稱與參數
3. 本模組執行對應的 Python 函式
4. 把 tool_result 送回 Claude
5. Claude 產生最終自然語言回覆
"""
import json
import os
import sys
from data_store import record_harvest, add_task, list_tasks
import farm_bridge

def log(msg: str):
    print(f"[TOOLS] {msg}", flush=True, file=sys.stderr)


# ── 工具定義（傳給 Anthropic API 的 schema） ──────────────────────────────────

TOOLS = [
    {
        "name": "record_harvest",
        "description": (
            "紀錄農場的採收數據。"
            "當使用者說要記錄採收、收成、產量、今天採了多少時使用。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "農場地點，例如：大溪農場、頭城農場、員山農場",
                },
                "crop": {
                    "type": "string",
                    "description": "作物名稱，例如：香茅、薰衣草、玫瑰、羅勒",
                },
                "amount": {
                    "type": "number",
                    "description": "採收數量（純數字）",
                },
                "unit": {
                    "type": "string",
                    "description": "單位，例如：公斤、斤、束、株、顆、包",
                },
            },
            "required": ["location", "crop", "amount", "unit"],
        },
    },
    {
        "name": "add_task",
        "description": (
            "新增待辦事項或任務提醒。"
            "當使用者說要記錄待辦、提醒某件事、建立任務、記得要做某事時使用。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "任務的具體描述",
                },
                "deadline": {
                    "type": "string",
                    "description": "截止日期，例如：2025-12-31、本週五、明天下午三點",
                },
            },
            "required": ["description", "deadline"],
        },
    },
    {
        "name": "query_harvests",
        "description": (
            "查詢農場「真實」採收量（直接讀取農場回報系統的週報資料）。"
            "當使用者問某農場採收了多少、最近產量、這週收成、統計採收時使用。"
            "例如「大溪這週採收多少」「最近四週各農場採收量」。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "farm": {
                    "type": "string",
                    "description": "農場名稱片段（可選，不填則查全部農場），例：大溪、頭城",
                },
                "weeks": {
                    "type": "integer",
                    "description": "要查詢最近幾週（可選，預設 4，最多 12）",
                },
            },
        },
    },
    {
        "name": "check_report_status",
        "description": (
            "查詢本週各農場的週報繳交狀態：誰交了、誰還沒交、有無採收異常。"
            "當老闆問「這週誰還沒交週報」「週報進度」「哪些農場沒回報」"
            "「本週回報狀況」時使用。這是直接讀農場回報系統的即時資料。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "week_start": {
                    "type": "string",
                    "description": "指定週的起始日 YYYY-MM-DD（可選，不填＝本週）",
                },
            },
        },
    },
    {
        "name": "list_tasks",
        "description": (
            "列出目前的待辦事項。"
            "當使用者問有什麼待辦、還有哪些任務沒完成時使用。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "篩選狀態：pending（未完成）或 done（已完成），預設 pending",
                    "enum": ["pending", "done"],
                },
            },
        },
    },
    {
        "name": "notify_boss",
        "description": (
            "主動傳 LINE 私訊給老闆（農場主人）。"
            "當使用者說「告訴老闆」、「通知老闆」、「跟老闆說」、「傳訊息給老闆」、"
            "「幫我告訴老闆」、「讓老闆知道」時使用。"
            "這個工具會真的發出 LINE 訊息，不只是記錄。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "要傳給老闆的訊息內容，用繁體中文，清楚說明事情",
                },
            },
            "required": ["message"],
        },
    },
]


# ── 老闆私訊（直接呼叫 LINE API，避免循環引用 app.py）────────────────────────

def _tool_notify_boss(message: str) -> str:
    """
    真正發 LINE 私訊給老闆。
    優先讀 BOSS_LINE_USER_ID 環境變數，其次讀 config.json boss_user_id。
    """
    import requests

    # 取老闆 ID
    boss_id = os.environ.get("BOSS_LINE_USER_ID", "").strip()
    if not boss_id:
        try:
            import json as _json
            cfg_path = os.path.join(os.path.dirname(__file__), "config.json")
            with open(cfg_path, encoding="utf-8") as f:
                boss_id = _json.load(f).get("boss_user_id", "").strip()
        except Exception:
            pass

    if not boss_id:
        log("notify_boss tool: boss_id 未設定")
        return json.dumps({
            "success": False,
            "error": "老闆 ID 未設定，請在 Render 環境變數設定 BOSS_LINE_USER_ID",
        }, ensure_ascii=False)

    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    try:
        resp = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"to": boss_id, "messages": [{"type": "text", "text": message}]},
            timeout=10,
        )
        resp.raise_for_status()
        log(f"notify_boss tool: 發送成功 → {boss_id[:10]}...")
        return json.dumps({
            "success": True,
            "message": f"✅ 訊息已成功傳送給老闆：「{message}」",
        }, ensure_ascii=False)
    except Exception as e:
        log(f"notify_boss tool ERROR: {e}")
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


# ── 工具執行器 ────────────────────────────────────────────────────────────────

def execute_tool(tool_name: str, tool_input: dict) -> str:
    """執行工具並回傳 JSON 字串給 Claude。"""
    log(f"Executing: {tool_name}({tool_input})")
    try:
        if tool_name == "record_harvest":
            result = record_harvest(
                location=tool_input["location"],
                crop=tool_input["crop"],
                amount=float(tool_input["amount"]),
                unit=tool_input["unit"],
            )
            return json.dumps({
                "success": True,
                "message": f"已記錄：{result['location']} 採收 {result['crop']} {result['amount']}{result['unit']}",
                "record_id": result["id"],
                "timestamp": result["timestamp"],
            }, ensure_ascii=False)

        elif tool_name == "add_task":
            result = add_task(
                description=tool_input["description"],
                deadline=tool_input["deadline"],
            )
            return json.dumps({
                "success": True,
                "message": f"已新增待辦：{result['description']}（截止：{result['deadline']}）",
                "task_id": result["id"],
            }, ensure_ascii=False)

        elif tool_name == "query_harvests":
            weeks = int(tool_input.get("weeks") or 4)
            data = farm_bridge.query_real_harvest(
                farm=tool_input.get("farm", ""),
                weeks=weeks,
            )
            if not data.get("ok"):
                return json.dumps({
                    "success": False,
                    "error": data.get("error", "無法連線農場回報系統"),
                }, ensure_ascii=False)
            return json.dumps({
                "success": True,
                "summary": farm_bridge.format_harvest(data),
                "data": data.get("farms", []),
            }, ensure_ascii=False)

        elif tool_name == "check_report_status":
            data = farm_bridge.get_report_status(
                week_start=tool_input.get("week_start", ""),
            )
            if not data.get("ok"):
                return json.dumps({
                    "success": False,
                    "error": data.get("error", "無法連線農場回報系統"),
                }, ensure_ascii=False)
            return json.dumps({
                "success": True,
                "summary": farm_bridge.format_status_digest(data, for_boss=True),
                "submitted_count": data.get("submitted_count"),
                "missing_count": data.get("missing_count"),
                "missing": [m["farm"] for m in data.get("missing", [])],
                "anomalies": data.get("anomalies", []),
            }, ensure_ascii=False)

        elif tool_name == "list_tasks":
            tasks = list_tasks(status=tool_input.get("status", "pending"))
            return json.dumps({
                "success": True,
                "count": len(tasks),
                "tasks": tasks,
            }, ensure_ascii=False)

        elif tool_name == "notify_boss":
            return _tool_notify_boss(tool_input["message"])

        else:
            return json.dumps({"success": False, "error": f"未知工具：{tool_name}"}, ensure_ascii=False)

    except Exception as e:
        log(f"Tool error [{tool_name}]: {e}")
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


# ── 多輪 Tool Use 對話流程 ────────────────────────────────────────────────────

def run_with_tools(claude_client, model: str, system: str, messages: list) -> tuple[str, list]:
    """
    執行帶有 tool use 的 Claude 對話。
    自動處理「Claude 要求工具 → 執行工具 → 回傳結果 → Claude 回覆」的多輪流程。

    回傳：(最終文字回覆, 更新後的 messages)

    解決 Webhook 超時問題的說明：
    此函式只在背景呼叫（由 handle_message 發起，而 handle_message
    已在 Flask worker 執行緒中執行，不阻塞主 Webhook 回應）。
    Webhook 端點本身只做「把任務放入佇列」的動作（< 1ms），
    立刻回傳 OK 給 LINE，完全不等待 AI 回覆。
    （詳見 app.py 的 callback route 說明）
    """
    MAX_ROUNDS = 5  # 防止無限工具呼叫迴圈

    for round_num in range(MAX_ROUNDS):
        response = claude_client.messages.create(
            model=model,
            max_tokens=1024,
            system=system,
            tools=TOOLS,
            messages=messages,
        )
        log(f"Tool round {round_num + 1}: stop_reason={response.stop_reason}")

        # ── 純文字回覆，流程結束 ──
        if response.stop_reason != "tool_use":
            reply_text = "".join(
                block.text for block in response.content if hasattr(block, "text")
            )
            messages.append({"role": "assistant", "content": reply_text})
            return reply_text, messages

        # ── Claude 要求呼叫工具 ──
        # 1. 把 Claude 的回應（含 tool_use block）存入歷史
        messages.append({"role": "assistant", "content": response.content})

        # 2. 執行所有工具，收集結果
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result_str = execute_tool(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_str,
                })

        # 3. 把工具結果送回給 Claude，進入下一輪
        messages.append({"role": "user", "content": tool_results})

    # 超過最大輪數
    log("Max tool rounds reached, returning fallback")
    return "抱歉，我處理這個指令花了太多步驟，請試著換個說法。", messages
