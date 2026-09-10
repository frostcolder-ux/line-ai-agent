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
        "name": "query_impact_data",
        "description": (
            "查詢 ESG 影響力數據（Impact Data）：弱勢就業工時、轉銜一般職場人數、"
            "獨立完成任務數、企業來訪人次、採收量、堆肥量、物種觀察數等。"
            "當使用者問「這季的影響力數據」「就業工時多少」「ESG 指標」"
            "「服務了多少人次」「要給企業的數據」時使用。"
            "資料直接讀農場回報系統的週報，是真實數字。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "weeks": {
                    "type": "integer",
                    "description": "要統計最近幾週（可選，預設 4，最多 52）",
                },
                "farm": {
                    "type": "string",
                    "description": "農場名稱片段（可選，不填＝全部農場）",
                },
            },
        },
    },
    {
        "name": "check_esg_documents",
        "description": (
            "查詢各農場的合規文件收集狀態：誰已經交了文件、誰完全沒有。"
            "當使用者問「哪些農場還沒交文件」「ESG 文件進度」「合規文件收齊了嗎」時使用。"
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "check_shipments",
        "description": (
            "查詢出貨單與物流狀態：待出貨、已出貨、缺貨運單號的單子。"
            "當使用者問「還有哪些沒出貨」「出貨進度」「最近出了幾單」"
            "「哪些單子沒填單號」時使用。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["pending", "shipped"],
                    "description": "篩選狀態（可選）：pending=待出貨，shipped=已出貨",
                },
                "days": {
                    "type": "integer",
                    "description": "查最近幾天（可選，預設 30，最多 365）",
                },
            },
        },
    },
    {
        "name": "list_meetings",
        "description": (
            "查詢會議安排與待追蹤的決議事項。"
            "當使用者問「最近有什麼會」「下次開會什麼時候」「會議決議追蹤」"
            "「上次開會決定什麼」時使用。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days_ahead": {
                    "type": "integer",
                    "description": "往後查幾天（可選，預設 30）",
                },
                "days_back": {
                    "type": "integer",
                    "description": "往前查幾天（可選，預設 14）",
                },
            },
        },
    },
    {
        "name": "schedule_meeting",
        "description": (
            "建立／排定一場會議。"
            "當使用者說「安排一場會議」「下週三下午兩點開月會」"
            "「跟苗栗約線上會議」時使用。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "會議名稱"},
                "meet_at": {
                    "type": "string",
                    "description": "會議時間，ISO 格式 YYYY-MM-DDTHH:MM，例 2026-09-20T14:00",
                },
                "location": {"type": "string", "description": "地點或線上會議連結（可選）"},
                "scope": {"type": "string", "description": "與會組織，逗號分隔（可選）"},
                "agenda": {"type": "string", "description": "議程（可選）"},
            },
            "required": ["title", "meet_at"],
        },
    },
    {
        "name": "record_meeting_minutes",
        "description": (
            "記錄某場會議的會議紀錄與決議待辦事項，並把會議標記為已完成。"
            "當使用者說「記錄今天的會議紀錄」「這次會議決議是…」時使用。"
            "要先用 list_meetings 找到會議 id。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "meeting_id": {"type": "integer", "description": "會議 id"},
                "minutes": {"type": "string", "description": "會議紀錄與決議內容"},
                "actions": {
                    "type": "array",
                    "description": "決議產生的待辦事項清單（可選）",
                    "items": {
                        "type": "object",
                        "properties": {
                            "description": {"type": "string", "description": "待辦內容"},
                            "owner": {"type": "string", "description": "負責人或單位"},
                            "due_date": {"type": "string", "description": "期限 YYYY-MM-DD"},
                        },
                        "required": ["description"],
                    },
                },
            },
            "required": ["meeting_id", "minutes"],
        },
    },
    {
        "name": "complete_meeting_action",
        "description": (
            "把某項會議決議待辦標記為已完成。"
            "當使用者說「第 3 項決議做完了」「把那個待辦結掉」時使用。"
            "要先用 list_meetings 取得待辦的 action_id。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action_id": {"type": "integer", "description": "待辦事項 id"},
            },
            "required": ["action_id"],
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

        elif tool_name == "query_impact_data":
            data = farm_bridge.get_impact(
                weeks=int(tool_input.get("weeks") or 4),
                farm=tool_input.get("farm", ""),
            )
            if not data.get("ok"):
                return json.dumps({"success": False,
                                   "error": data.get("error", "無法連線農場回報系統")},
                                  ensure_ascii=False)
            return json.dumps({
                "success": True,
                "summary": farm_bridge.format_impact(data),
                "totals": data.get("totals", {}),
                "report_count": data.get("report_count", 0),
            }, ensure_ascii=False)

        elif tool_name == "check_esg_documents":
            data = farm_bridge.get_esg_docs()
            if not data.get("ok"):
                return json.dumps({"success": False,
                                   "error": data.get("error", "無法連線農場回報系統")},
                                  ensure_ascii=False)
            return json.dumps({
                "success": True,
                "summary": farm_bridge.format_esg_docs(data),
                "total_documents": data.get("total_documents", 0),
            }, ensure_ascii=False)

        elif tool_name == "check_shipments":
            data = farm_bridge.get_shipments(
                status=tool_input.get("status", ""),
                days=int(tool_input.get("days") or 30),
            )
            if not data.get("ok"):
                return json.dumps({"success": False,
                                   "error": data.get("error", "無法連線農場回報系統")},
                                  ensure_ascii=False)
            return json.dumps({
                "success": True,
                "summary": farm_bridge.format_shipments(data),
                "pending_count": data.get("pending_count", 0),
                "missing_tracking_count": data.get("missing_tracking_count", 0),
            }, ensure_ascii=False)

        elif tool_name == "list_meetings":
            data = farm_bridge.get_meetings(
                days_ahead=int(tool_input.get("days_ahead") or 30),
                days_back=int(tool_input.get("days_back") or 14),
            )
            if not data.get("ok"):
                return json.dumps({"success": False,
                                   "error": data.get("error", "無法連線農場回報系統")},
                                  ensure_ascii=False)
            return json.dumps({
                "success": True,
                "summary": farm_bridge.format_meetings(data),
                "meetings": data.get("meetings", []),
                "pending_actions": data.get("pending_actions", []),
            }, ensure_ascii=False)

        elif tool_name == "schedule_meeting":
            data = farm_bridge.create_meeting(
                title=tool_input["title"],
                meet_at=tool_input["meet_at"],
                location=tool_input.get("location", ""),
                scope=tool_input.get("scope", ""),
                agenda=tool_input.get("agenda", ""),
            )
            if not data.get("ok") or data.get("error"):
                return json.dumps({"success": False,
                                   "error": data.get("error", "建立會議失敗")},
                                  ensure_ascii=False)
            m = data.get("meeting", {})
            when = (m.get("meet_at") or "").replace("T", " ")[:16]
            return json.dumps({
                "success": True,
                "message": f"已排定會議「{m.get('title')}」{when}",
                "meeting_id": m.get("id"),
            }, ensure_ascii=False)

        elif tool_name == "record_meeting_minutes":
            data = farm_bridge.record_minutes(
                meeting_id=int(tool_input["meeting_id"]),
                minutes=tool_input["minutes"],
                actions=tool_input.get("actions") or [],
            )
            if not data.get("ok") or data.get("error"):
                return json.dumps({"success": False,
                                   "error": data.get("error", "記錄會議紀錄失敗")},
                                  ensure_ascii=False)
            m = data.get("meeting", {})
            return json.dumps({
                "success": True,
                "message": f"已記錄「{m.get('title')}」的會議紀錄，"
                           f"共 {len(m.get('actions', []))} 項決議待辦",
                "actions": m.get("actions", []),
            }, ensure_ascii=False)

        elif tool_name == "complete_meeting_action":
            data = farm_bridge.complete_action(int(tool_input["action_id"]))
            if not data.get("ok") or data.get("error"):
                return json.dumps({"success": False,
                                   "error": data.get("error", "更新待辦失敗")},
                                  ensure_ascii=False)
            return json.dumps({
                "success": True,
                "message": f"已完成：{data.get('description', '')}",
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
