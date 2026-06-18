import os
import io
import json
import urllib.request
import urllib.error
from datetime import datetime, timedelta, date
from typing import Optional

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

CATEGORIES = ["公用试剂", "特殊试剂", "试剂盒", "酶", "细胞", "抗体", "耗材", "细胞培养", "设备"]

USE_TURSO = bool(os.environ.get("TURSO_URL"))
TURSO_URL = os.environ.get("TURSO_URL", "")
TURSO_TOKEN = os.environ.get("TURSO_TOKEN", "")
TURSO_API = TURSO_URL.replace("libsql://", "https://") + "/v2/pipeline" if USE_TURSO else ""

def _turso_request(requests: list) -> list:
    body = json.dumps({"requests": requests}).encode()
    req = urllib.request.Request(TURSO_API, data=body,
        headers={"Authorization": f"Bearer {TURSO_TOKEN}", "Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req).read())["results"]

def _turso_exec(sql: str, args: list = None) -> dict:
    stmt = {"sql": sql}
    if args:
        stmt["args"] = [{"type": "text", "value": str(a)} for a in args]
    results = _turso_request([{"type": "execute", "stmt": stmt}])
    return results[0] if results else {"type": "error", "error": "no result"}

def _turso_fetch(sql: str, args: list = None) -> list[dict]:
    result = _turso_exec(sql, args)
    if result["type"] == "error":
        return []
    data = result["response"]["result"]
    cols = [c["name"] for c in data.get("cols", [])]
    return [dict(zip(cols, [v.get("value", "") for v in row])) for row in data.get("rows", [])]

def localtime():
    tz = os.environ.get("TZ")
    if tz:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M:%S")
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def localdate():
    tz = os.environ.get("TZ")
    if tz:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(tz)).strftime("%Y-%m-%d")
    return date.today().strftime("%Y-%m-%d")

def init_db():
    now = localtime()
    for stmt in [
        f"CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, quantity REAL DEFAULT 0, unit TEXT DEFAULT '', location TEXT DEFAULT '', category TEXT DEFAULT '耗材', expiry_date TEXT, daily_consumption REAL DEFAULT 0, min_threshold REAL DEFAULT 0, supplier TEXT DEFAULT '', price REAL DEFAULT 0, notes TEXT DEFAULT '', weekly_check INTEGER DEFAULT 0, last_checked TEXT, created_at TEXT DEFAULT '{now}', updated_at TEXT DEFAULT '{now}')",
        f"CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, nickname TEXT DEFAULT '', ip TEXT DEFAULT '', content TEXT DEFAULT '', status TEXT DEFAULT 'pending', wish_name TEXT, wish_category TEXT, wish_quantity REAL, wish_unit TEXT, wish_price REAL, created_at TEXT DEFAULT '{now}')",
        f"CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY AUTOINCREMENT, nickname TEXT DEFAULT '', ip TEXT DEFAULT '', action TEXT NOT NULL, created_at TEXT DEFAULT '{now}')",
        f"CREATE TABLE IF NOT EXISTS check_records (id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER NOT NULL, nickname TEXT DEFAULT '', ip TEXT DEFAULT '', action TEXT NOT NULL, note TEXT DEFAULT '', created_at TEXT DEFAULT '{now}')",
    ]:
        if USE_TURSO:
            _turso_exec(stmt)
        else:
            import sqlite3
            db = sqlite3.connect("lab.db")
            db.execute(stmt)
            db.commit()
            db.close()

init_db()

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
_template_registry = {}
for _name in ["check.html", "index.html", "messages.html", "logs.html"]:
    try:
        _template_registry[_name] = templates.env.get_template(_name)
    except Exception:
        pass

def _render(name, request, **kwargs):
    t = _template_registry.get(name) or templates.env.get_template(name)
    return HTMLResponse(t.render(request=request, **kwargs))

def ip_of(request):
    fwd = request.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "unknown")

def add_log(ip, nickname, action):
    _turso_exec("INSERT INTO logs (nickname, ip, action, created_at) VALUES (?,?,?,?)",
                [nickname, ip, action, localtime()])

def item_status(item):
    qty = float(item.get("quantity", 0))
    threshold = float(item.get("min_threshold", 0))
    daily = float(item.get("daily_consumption", 0))
    expiry = item.get("expiry_date") or ""
    if qty <= 0:
        return ("red", "已用完")
    if expiry:
        try:
            if datetime.strptime(expiry, "%Y-%m-%d").date() <= date.today():
                return ("red", "已过期")
            if datetime.strptime(expiry, "%Y-%m-%d").date() <= date.today() + timedelta(days=7):
                return ("yellow", "即将过期")
        except (ValueError, TypeError):
            pass
    if threshold > 0 and qty <= threshold:
        return ("yellow", "低于阈值")
    if daily > 0:
        days_left = qty / daily
        if days_left <= 14:
            return ("yellow", f"约{int(days_left)}天后用完")
    return ("green", "充足")

def needs_check(item):
    if int(item.get("weekly_check", 0)):
        return True
    return item_status(item)[0] in ("red", "yellow")

def enrich(items):
    for it in items:
        it["status_level"], it["status_text"] = item_status(it)
        it["est_remaining"] = ""
        d = float(it.get("daily_consumption", 0))
        q = float(it.get("quantity", 0))
        if d > 0 and q > 0:
            it["est_remaining"] = f"约{int(q / d)}天"

# --- Pages ---

@app.get("/check", response_class=HTMLResponse)
async def check_page(request: Request):
    items = _turso_fetch("SELECT * FROM items ORDER BY category, name")
    enrich(items)
    check_items = [it for it in items if needs_check(it)]
    cr = _turso_fetch("SELECT c.*, i.name as item_name FROM check_records c LEFT JOIN items i ON i.id = c.item_id ORDER BY c.id DESC LIMIT 30")
    return _render("check.html", request, items=check_items, categories=tuple(CATEGORIES), check_records=cr)

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    items = _turso_fetch("SELECT * FROM items ORDER BY id DESC")
    enrich(items)
    total_count = len(items)
    total_value_str = f"{sum(float(it.get('price', 0) or 0) for it in items):.0f}"
    warnings = [it for it in items if it["status_level"] in ("red", "yellow")]
    return _render("index.html", request, items=items, categories=tuple(CATEGORIES),
                   total_count=total_count, total_value_str=total_value_str, warnings=warnings)

@app.get("/messages", response_class=HTMLResponse)
async def messages_page(request: Request):
    msgs = _turso_fetch("SELECT * FROM messages ORDER BY id DESC")
    return _render("messages.html", request, messages=msgs, categories=tuple(CATEGORIES))

@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    logs = _turso_fetch("SELECT * FROM logs ORDER BY id DESC LIMIT 200")
    return _render("logs.html", request, logs=logs)

# --- API: Items ---

@app.get("/api/items")
async def api_items(request: Request):
    cat = request.query_params.get("category", "")
    search = request.query_params.get("search", "")
    q = "SELECT * FROM items WHERE 1=1"; p = []
    if cat: q += " AND category = ?"; p.append(cat)
    if search: q += " AND name LIKE ?"; p.append(f"%{search}%")
    q += " ORDER BY id DESC"
    items = _turso_fetch(q, p)
    enrich(items)
    return items

@app.post("/api/items")
async def api_items_add(request: Request):
    form = await request.form()
    ip = ip_of(request)
    nickname = form.get("nickname", "")
    now = localtime()
    cols = "name, quantity, unit, location, category, expiry_date, daily_consumption, min_threshold, supplier, price, notes, created_at, updated_at"
    vals = [form.get("name",""), str(float(form.get("quantity",0))), form.get("unit",""),
            form.get("location",""), form.get("category","耗材"), form.get("expiry_date") or "",
            str(float(form.get("daily_consumption",0))), str(float(form.get("min_threshold",0))),
            form.get("supplier",""), str(float(form.get("price",0)or 0)), form.get("notes",""), now, now]
    _turso_exec(f"INSERT INTO items ({cols}) VALUES ({','.join(['?']*13)})", vals)
    add_log(ip, nickname, f"添加物品: {form.get('name','')}")
    return RedirectResponse("/", status_code=303)

@app.put("/api/items/{item_id}")
async def api_items_update(item_id: int, request: Request):
    form = await request.form()
    ip = ip_of(request)
    nickname = form.get("nickname", "")
    now = localtime()
    if form.get("_quick") == "1":
        old = _turso_fetch("SELECT name, quantity FROM items WHERE id=?", [str(item_id)])
        if old:
            new_qty = str(float(form.get("quantity", 0)))
            _turso_exec("UPDATE items SET quantity=?, updated_at=? WHERE id=?", [new_qty, now, str(item_id)])
            add_log(ip, nickname, f"快速调量: {old[0]['name']} {old[0]['quantity']}→{new_qty}")
        return RedirectResponse("/", status_code=303)
    _turso_exec("UPDATE items SET name=?,quantity=?,unit=?,location=?,category=?,expiry_date=?,daily_consumption=?,min_threshold=?,supplier=?,price=?,notes=?,updated_at=? WHERE id=?", [
        form.get("name",""), str(float(form.get("quantity",0))), form.get("unit",""),
        form.get("location",""), form.get("category","耗材"), form.get("expiry_date") or "",
        str(float(form.get("daily_consumption",0))), str(float(form.get("min_threshold",0))),
        form.get("supplier",""), str(float(form.get("price",0)or 0)), form.get("notes",""), now, str(item_id)])
    add_log(ip, nickname, f"编辑物品: {form.get('name','')}")
    return RedirectResponse("/", status_code=303)

@app.delete("/api/items/{item_id}")
async def api_items_delete(item_id: int, request: Request):
    ip = ip_of(request)
    nickname = request.query_params.get("nickname", "")
    item = _turso_fetch("SELECT name FROM items WHERE id=?", [str(item_id)])
    if item:
        _turso_exec("DELETE FROM items WHERE id=?", [str(item_id)])
        add_log(ip, nickname, f"删除物品: {item[0]['name']}")
    return {"ok": True}

# --- API: Check ---

@app.post("/api/check/{item_id}/confirm")
async def check_confirm(item_id: int, request: Request):
    form = await request.form()
    ip = ip_of(request)
    nickname = form.get("nickname", "")
    new_qty = form.get("quantity")
    now = localtime()
    item = _turso_fetch("SELECT name, quantity FROM items WHERE id=?", [str(item_id)])
    if item:
        if new_qty is not None:
            old_qty = item[0]["quantity"]
            qty = str(float(new_qty))
            _turso_exec("UPDATE items SET quantity=?, last_checked=?, updated_at=? WHERE id=?", [qty, localdate(), now, str(item_id)])
            _turso_exec("INSERT INTO check_records (item_id, nickname, ip, action, note, created_at) VALUES (?,?,?,?,?,?)",
                       [str(item_id), nickname, ip, "确认数量", f"{old_qty}→{qty}", now])
            add_log(ip, nickname, f"周检确认: {item[0]['name']} {old_qty}→{qty}")
        else:
            _turso_exec("UPDATE items SET last_checked=?, updated_at=? WHERE id=?", [localdate(), now, str(item_id)])
            _turso_exec("INSERT INTO check_records (item_id, nickname, ip, action, note, created_at) VALUES (?,?,?,?,?,?)",
                       [str(item_id), nickname, ip, "确认", "", now])
            add_log(ip, nickname, f"周检确认: {item[0]['name']}")
    return RedirectResponse("/check", status_code=303)

# --- API: Messages ---

@app.post("/api/messages")
async def api_messages_add(request: Request):
    form = await request.form()
    ip = ip_of(request)
    nickname = form.get("nickname", "")
    content = form.get("content", "")
    now = localtime()
    _turso_exec("INSERT INTO messages (nickname, ip, content, wish_name, wish_category, wish_quantity, wish_unit, wish_price, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
               [nickname, ip, content, form.get("wish_name") or "", form.get("wish_category") or "",
                str(float(form.get("wish_quantity",0))) if form.get("wish_quantity") else "",
                form.get("wish_unit") or "", str(float(form.get("wish_price",0))) if form.get("wish_price") else "", now])
    add_log(ip, nickname, "提交愿望单" if form.get("wish_name") else "发布留言")
    return RedirectResponse("/messages", status_code=303)

@app.post("/api/messages/{msg_id}/toggle")
async def api_messages_toggle(msg_id: int, request: Request):
    form = await request.form()
    ip = ip_of(request)
    nickname = form.get("nickname", "")
    now = localtime()
    msg = _turso_fetch("SELECT * FROM messages WHERE id=?", [str(msg_id)])
    if msg:
        m = msg[0]
        new_status = "done" if m["status"] == "pending" else "pending"
        _turso_exec("UPDATE messages SET status=? WHERE id=?", [new_status, str(msg_id)])
        if new_status == "done" and m.get("wish_name") and form.get("add_to_items"):
            _turso_exec("INSERT INTO items (name, category, quantity, unit, price, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                       [m["wish_name"], m.get("wish_category") or "耗材",
                        str(m.get("wish_quantity") or 1), m.get("wish_unit") or "",
                        str(m.get("wish_price") or 0), now, now])
            add_log(ip, nickname, f"愿望单转入清单: {m['wish_name']}")
        add_log(ip, nickname, f"标记留言 {msg_id} 为{new_status}")
    return RedirectResponse("/messages", status_code=303)

@app.delete("/api/messages/{msg_id}")
async def api_messages_delete(msg_id: int, request: Request):
    ip = ip_of(request)
    nickname = request.query_params.get("nickname", "")
    msgs = _turso_fetch("SELECT * FROM messages WHERE id=?", [str(msg_id)])
    if msgs:
        _turso_exec("DELETE FROM messages WHERE id=?", [str(msg_id)])
        add_log(ip, nickname, f"删除留言: {msgs[0].get('wish_name') or msgs[0].get('content','')[:30]}")
    return RedirectResponse("/messages", status_code=303)

# --- Export ---

@app.get("/export")
async def export_items(request: Request):
    items = _turso_fetch("SELECT * FROM items ORDER BY category, name")
    enrich(items)
    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active; ws.title = "物资清单"
    ws.append(["名称", "分类", "数量", "单位", "位置", "过期日期", "日消耗", "最低阈值", "供应商", "单价(¥)", "备注", "状态"])
    for it in items:
        ws.append([it.get("name"), it.get("category"), it.get("quantity"), it.get("unit"),
                   it.get("location"), it.get("expiry_date") or "", it.get("daily_consumption"),
                   it.get("min_threshold"), it.get("supplier"), it.get("price") or 0,
                   it.get("notes"), it.get("status_text")])
    output = io.BytesIO(); wb.save(output); output.seek(0)
    return StreamingResponse(output, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            headers={"Content-Disposition": "attachment; filename=inventory.xlsx"})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))

