import os
import io
import sqlite3
from datetime import datetime, timedelta, date

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

DB_PATH = os.environ.get("DB_PATH", "lab.db")
CATEGORIES = ["公用试剂", "特殊试剂", "试剂盒", "酶", "细胞", "抗体", "耗材", "细胞培养", "设备"]

def localtime():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def localdate():
    return date.today().strftime("%Y-%m-%d")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    db = get_db()
    now = localtime()
    for stmt in [
        f"""CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, quantity REAL NOT NULL DEFAULT 0,
            unit TEXT NOT NULL DEFAULT '', location TEXT DEFAULT '',
            category TEXT NOT NULL DEFAULT '耗材', expiry_date TEXT,
            daily_consumption REAL NOT NULL DEFAULT 0,
            min_threshold REAL NOT NULL DEFAULT 0,
            supplier TEXT DEFAULT '', price REAL DEFAULT 0,
            notes TEXT DEFAULT '', weekly_check INTEGER NOT NULL DEFAULT 0,
            last_checked TEXT, created_at TEXT NOT NULL DEFAULT '{now}',
            updated_at TEXT NOT NULL DEFAULT '{now}')""",
        f"""CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nickname TEXT NOT NULL DEFAULT '', ip TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
            wish_name TEXT, wish_category TEXT, wish_quantity REAL,
            wish_unit TEXT, wish_price REAL,
            created_at TEXT NOT NULL DEFAULT '{now}')""",
        f"""CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nickname TEXT NOT NULL DEFAULT '', ip TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT '{now}')""",
        f"""CREATE TABLE IF NOT EXISTS check_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER NOT NULL,
            nickname TEXT NOT NULL DEFAULT '', ip TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL, note TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '{now}')""",
    ]:
        db.execute(stmt)
    db.commit()
    db.close()

init_db()

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

def ip_of(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "unknown")

def add_log(db, ip, nickname, action):
    db.execute("INSERT INTO logs (nickname, ip, action, created_at) VALUES (?,?,?,?)",
               (nickname, ip, action, localtime()))

def item_status(item: dict) -> tuple:
    qty = item.get("quantity", 0)
    threshold = item.get("min_threshold", 0)
    daily = item.get("daily_consumption", 0)
    expiry = item.get("expiry_date")
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
    if item.get("weekly_check"):
        return True
    return item_status(item)[0] in ("red", "yellow")

def enrich_items(items):
    for item in items:
        item["status_level"], item["status_text"] = item_status(item)
        item["est_remaining"] = ""
        d = item.get("daily_consumption", 0)
        q = item.get("quantity", 0)
        if d > 0 and q > 0:
            item["est_remaining"] = f"约{int(q / d)}天"

# --- Pages ---

@app.get("/check", response_class=HTMLResponse)
async def check_page(request: Request):
    db = get_db()
    items = [dict(r) for r in db.execute("SELECT * FROM items ORDER BY category, name")]
    enrich_items(items)
    check_items = [it for it in items if needs_check(it)]
    cr = [dict(r) for r in db.execute("""
        SELECT c.*, i.name as item_name FROM check_records c
        LEFT JOIN items i ON i.id = c.item_id ORDER BY c.id DESC LIMIT 30""")]
    db.close()
    return templates.TemplateResponse("check.html", {
        "request": request, "items": [dict(it) for it in check_items],
        "categories": tuple(CATEGORIES), "check_records": [dict(r) for r in cr]})

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    db = get_db()
    items = [dict(r) for r in db.execute("SELECT * FROM items ORDER BY id DESC")]
    enrich_items(items)
    total_count = len(items)
    total_value_str = f"{sum(it.get('price') or 0 for it in items):.0f}"
    warnings = [it for it in items if it["status_level"] in ("red", "yellow")]
    db.close()
    return templates.TemplateResponse("index.html", {
        "request": request,
        "items": [dict(i) for i in items],
        "categories": tuple(CATEGORIES),
        "total_count": total_count,
        "total_value_str": total_value_str,
        "warnings": [dict(w) for w in warnings],
    })

@app.get("/messages", response_class=HTMLResponse)
async def messages_page(request: Request):
    db = get_db()
    msgs = [dict(r) for r in db.execute("SELECT * FROM messages ORDER BY id DESC")]
    db.close()
    return templates.TemplateResponse("messages.html", {
        "request": request, "messages": [dict(m) for m in msgs], "categories": tuple(CATEGORIES)})

@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    db = get_db()
    logs = [dict(r) for r in db.execute("SELECT * FROM logs ORDER BY id DESC LIMIT 200")]
    db.close()
    return templates.TemplateResponse("logs.html", {"request": request, "logs": [dict(l) for l in logs]})

# --- API: Items ---

@app.get("/api/items")
async def api_items(request: Request):
    db = get_db()
    cat = request.query_params.get("category", "")
    search = request.query_params.get("search", "")
    q = "SELECT * FROM items WHERE 1=1"; p = []
    if cat: q += " AND category = ?"; p.append(cat)
    if search: q += " AND name LIKE ?"; p.append(f"%{search}%")
    q += " ORDER BY id DESC"
    items = [dict(r) for r in db.execute(q, p)]
    enrich_items(items)
    db.close()
    return items

@app.post("/api/items")
async def api_items_add(request: Request):
    form = await request.form()
    ip = ip_of(request)
    nickname = form.get("nickname", "")
    now = localtime()
    db = get_db()
    db.execute("""INSERT INTO items (name, quantity, unit, location, category, expiry_date,
        daily_consumption, min_threshold, supplier, price, notes, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        form.get("name", ""), float(form.get("quantity", 0)), form.get("unit", ""),
        form.get("location", ""), form.get("category", "耗材"),
        form.get("expiry_date") or None, float(form.get("daily_consumption", 0)),
        float(form.get("min_threshold", 0)), form.get("supplier", ""),
        float(form.get("price", 0) or 0), form.get("notes", ""), now, now))
    add_log(db, ip, nickname, f"添加物品: {form.get('name', '')}")
    db.commit()
    db.close()
    return RedirectResponse("/", status_code=303)

@app.put("/api/items/{item_id}")
async def api_items_update(item_id: int, request: Request):
    form = await request.form()
    ip = ip_of(request)
    nickname = form.get("nickname", "")
    now = localtime()
    db = get_db()
    if form.get("_quick") == "1":
        old = db.execute("SELECT name, quantity FROM items WHERE id=?", (item_id,)).fetchone()
        if old:
            new_qty = float(form.get("quantity", 0))
            db.execute("UPDATE items SET quantity=?, updated_at=? WHERE id=?", (new_qty, now, item_id))
            add_log(db, ip, nickname, f"快速调量: {old['name']} {old['quantity']}→{new_qty}")
            db.commit()
        db.close()
        return RedirectResponse("/", status_code=303)
    db.execute("""UPDATE items SET name=?, quantity=?, unit=?, location=?, category=?,
        expiry_date=?, daily_consumption=?, min_threshold=?, supplier=?, price=?,
        notes=?, updated_at=? WHERE id=?""", (
        form.get("name", ""), float(form.get("quantity", 0)), form.get("unit", ""),
        form.get("location", ""), form.get("category", "耗材"),
        form.get("expiry_date") or None, float(form.get("daily_consumption", 0)),
        float(form.get("min_threshold", 0)), form.get("supplier", ""),
        float(form.get("price", 0) or 0), form.get("notes", ""), now, item_id))
    add_log(db, ip, nickname, f"编辑物品: {form.get('name', '')}")
    db.commit()
    db.close()
    return RedirectResponse("/", status_code=303)

@app.delete("/api/items/{item_id}")
async def api_items_delete(item_id: int, request: Request):
    ip = ip_of(request)
    nickname = request.query_params.get("nickname", "")
    db = get_db()
    item = db.execute("SELECT name FROM items WHERE id=?", (item_id,)).fetchone()
    if item:
        db.execute("DELETE FROM items WHERE id=?", (item_id,))
        add_log(db, ip, nickname, f"删除物品: {item['name']}")
        db.commit()
    db.close()
    return {"ok": True}

# --- API: Check ---

@app.post("/api/check/{item_id}/confirm")
async def check_confirm(item_id: int, request: Request):
    form = await request.form()
    ip = ip_of(request)
    nickname = form.get("nickname", "")
    new_qty = form.get("quantity")
    now = localtime()
    db = get_db()
    item = db.execute("SELECT name, quantity FROM items WHERE id=?", (item_id,)).fetchone()
    if item:
        if new_qty is not None:
            old_qty = item["quantity"]
            qty = float(new_qty)
            db.execute("UPDATE items SET quantity=?, last_checked=?, updated_at=? WHERE id=?",
                       (qty, localdate(), now, item_id))
            db.execute("INSERT INTO check_records (item_id, nickname, ip, action, note, created_at) VALUES (?,?,?,?,?,?)",
                       (item_id, nickname, ip, "确认数量", f"{old_qty}→{qty}", now))
            add_log(db, ip, nickname, f"周检确认: {item['name']} {old_qty}→{qty}")
        else:
            db.execute("UPDATE items SET last_checked=?, updated_at=? WHERE id=?",
                       (localdate(), now, item_id))
            db.execute("INSERT INTO check_records (item_id, nickname, ip, action, note, created_at) VALUES (?,?,?,?,?,?)",
                       (item_id, nickname, ip, "确认", "", now))
            add_log(db, ip, nickname, f"周检确认: {item['name']}")
        db.commit()
    db.close()
    return RedirectResponse("/check", status_code=303)

# --- API: Messages ---

@app.post("/api/messages")
async def api_messages_add(request: Request):
    form = await request.form()
    ip = ip_of(request)
    nickname = form.get("nickname", "")
    content = form.get("content", "")
    now = localtime()
    db = get_db()
    db.execute("""INSERT INTO messages (nickname, ip, content, wish_name, wish_category,
        wish_quantity, wish_unit, wish_price, created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
        (nickname, ip, content,
         form.get("wish_name") or None, form.get("wish_category") or None,
         float(form.get("wish_quantity", 0)) if form.get("wish_quantity") else None,
         form.get("wish_unit") or None,
         float(form.get("wish_price", 0)) if form.get("wish_price") else None, now))
    add_log(db, ip, nickname, "提交愿望单" if form.get("wish_name") else "发布留言")
    db.commit()
    db.close()
    return RedirectResponse("/messages", status_code=303)

@app.post("/api/messages/{msg_id}/toggle")
async def api_messages_toggle(msg_id: int, request: Request):
    form = await request.form()
    ip = ip_of(request)
    nickname = form.get("nickname", "")
    now = localtime()
    db = get_db()
    msg = db.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
    if msg:
        new_status = "done" if msg["status"] == "pending" else "pending"
        db.execute("UPDATE messages SET status=? WHERE id=?", (new_status, msg_id))
        if new_status == "done" and msg["wish_name"] and form.get("add_to_items"):
            db.execute("INSERT INTO items (name, category, quantity, unit, price, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                       (msg["wish_name"], msg["wish_category"] or "耗材",
                        msg["wish_quantity"] or 1, msg["wish_unit"] or "",
                        msg["wish_price"] or 0, now, now))
            add_log(db, ip, nickname, f"愿望单转入清单: {msg['wish_name']}")
        add_log(db, ip, nickname, f"标记留言 {msg_id} 为{new_status}")
        db.commit()
    db.close()
    return RedirectResponse("/messages", status_code=303)

# --- Export ---

@app.get("/export")
async def export_items(request: Request):
    db = get_db()
    items = [dict(r) for r in db.execute("SELECT * FROM items ORDER BY category, name")]
    enrich_items(items)
    db.close()
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "物资清单"
    ws.append(["名称", "分类", "数量", "单位", "位置", "过期日期", "日消耗", "最低阈值", "供应商", "单价(¥)", "备注", "状态"])
    for it in items:
        ws.append([it.get("name"), it.get("category"), it.get("quantity"), it.get("unit"),
                   it.get("location"), it.get("expiry_date") or "", it.get("daily_consumption"),
                   it.get("min_threshold"), it.get("supplier"), it.get("price") or 0,
                   it.get("notes"), it.get("status_text")])
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return StreamingResponse(output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=inventory.xlsx"})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
