import re, sqlite3, statistics
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE = Path(__file__).parent
DB = str(BASE / "data.db")
RIYAL_PER_DAY = 3.0      # قيمة يوم التوصيل بالريال (لحساب الأفضل قيمة)
SUSPICIOUS_RATIO = 1.3   # سعر أعلى من وسيط تاريخه بـ30% = مشبوه
GOOD_DEAL_RATIO = 0.85   # سعر أقل من الوسيط بـ15% = صفقة حقيقية
MIN_HISTORY = 5

app = FastAPI(title="Hakeem Price Compare")

AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
TASHKEEL = re.compile("[\u064B-\u0652\u0640]")


def normalize(s: str) -> str:
    """توحيد النص العربي للبحث: إزالة التشكيل، توحيد الهمزات والياء والتاء المربوطة."""
    s = s.translate(AR_DIGITS).lower().strip()
    s = TASHKEEL.sub("", s)
    s = re.sub("[إأآ]", "ا", s)
    s = s.replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
    return re.sub(r"\s+", " ", s)


def now():
    return datetime.utcnow().isoformat()


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY, name TEXT UNIQUE, search_key TEXT);
    CREATE TABLE IF NOT EXISTS prices(id INTEGER PRIMARY KEY, product_id INT, store TEXT,
        price REAL, shipping REAL DEFAULT 0, delivery_days INT DEFAULT 0, url TEXT, ts TEXT);
    CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY, product_id INT, target REAL,
        contact TEXT, triggered INT DEFAULT 0, triggered_at TEXT, hit_store TEXT, hit_price REAL);
    CREATE TABLE IF NOT EXISTS deals(id INTEGER PRIMARY KEY, title TEXT, store TEXT,
        price REAL, url TEXT, votes INT DEFAULT 0, verified INT DEFAULT 0, ts TEXT);
    CREATE TABLE IF NOT EXISTS flags(id INTEGER PRIMARY KEY, product_id INT, store TEXT,
        type TEXT, detail TEXT, ts TEXT);
    CREATE INDEX IF NOT EXISTS idx_prices_pid ON prices(product_id, store, id);
    CREATE INDEX IF NOT EXISTS idx_prices_ts ON prices(product_id, ts);
    CREATE INDEX IF NOT EXISTS idx_alerts_contact ON alerts(contact);
    CREATE INDEX IF NOT EXISTS idx_alerts_pid ON alerts(product_id, triggered);
    CREATE INDEX IF NOT EXISTS idx_products_key ON products(search_key);
    """)
    if not c.execute("SELECT 1 FROM products LIMIT 1").fetchone():
        seed(c)
    c.commit()
    c.close()


def seed(c):
    data = {
        "آيفون 15 برو": [("نون", 4300, 0, 2), ("أمازون", 4250, 15, 3), ("إكسترا", 4400, 0, 1)],
        "لابتوب ألعاب ASUS TUF": [("نون", 3900, 0, 3), ("جرير", 3800, 20, 2), ("إكسترا", 4000, 0, 2)],
        "أرز بسمتي 10 كجم": [("بنده", 55, 0, 1), ("الدانوب", 52, 0, 1), ("نون", 58, 0, 2)],
        "زيت زيتون 1 لتر": [("بنده", 38, 0, 1), ("الدانوب", 41, 0, 1), ("نون", 36, 0, 2)],
    }
    for name, offers in data.items():
        pid = get_or_create_product(c, name)
        for store, price, ship, days in offers:
            c.execute("INSERT INTO prices(product_id,store,price,shipping,delivery_days,url,ts) VALUES(?,?,?,?,?,?,?)",
                      (pid, store, price, ship, days, "https://example.com", now()))


def get_or_create_product(c, name):
    row = c.execute("SELECT id FROM products WHERE name=?", (name,)).fetchone()
    if row:
        return row["id"]
    return c.execute("INSERT INTO products(name,search_key) VALUES(?,?)",
                     (name, normalize(name))).lastrowid


def latest_offers(c, pid):
    """آخر سعر لكل متجر لمنتج معين، مرتّبة حسب السعر الكلي."""
    rows = c.execute("""
        SELECT p.* FROM prices p
        JOIN (SELECT store, MAX(id) mid FROM prices WHERE product_id=? GROUP BY store) m ON p.id=m.mid
        """, (pid,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["total"] = round(d["price"] + d["shipping"], 2)
        out.append(d)
    return sorted(out, key=lambda x: x["total"])


def best_value(offers):
    """الأفضل قيمة: السعر الكلي + قيمة زمن التوصيل بالريال لكل يوم."""
    if not offers:
        return None
    return min(offers, key=lambda o: o["total"] + RIYAL_PER_DAY * o["delivery_days"])


# ---------- models ----------
class PriceIn(BaseModel):
    product: str
    store: str
    price: float
    shipping: float = 0
    delivery_days: int = 0
    url: str = ""


class AlertIn(BaseModel):
    product: str
    target: float
    contact: str


class CartIn(BaseModel):
    items: list[str]


class DealIn(BaseModel):
    title: str
    store: str
    price: float
    url: str


class AskIn(BaseModel):
    text: str


# ---------- 1) بحث + مقارنة + توفير + إشارات الشبهة ----------
@app.get("/api/search")
def search(q: str = ""):
    c = db()
    rows = c.execute("SELECT * FROM products WHERE search_key LIKE ?", (f"%{normalize(q)}%",)).fetchall()
    res = []
    for p in rows:
        offers = latest_offers(c, p["id"])
        if not offers:
            continue
        flags = [dict(f) for f in c.execute(
            "SELECT store, type, detail FROM flags WHERE product_id=? ORDER BY id DESC LIMIT 5", (p["id"],))]
        res.append({
            "id": p["id"], "name": p["name"], "offers": offers,
            "cheapest": offers[0], "best_value": best_value(offers),
            "savings": round(offers[-1]["total"] - offers[0]["total"], 2) if len(offers) > 1 else 0,
            "flags": flags,
        })
    c.close()
    return res


# ---------- إضافة سعر + كشف التلاعب + تفعيل التنبيهات ----------
@app.post("/api/prices")
def add_price(p: PriceIn):
    c = db()
    pid = get_or_create_product(c, p.product)
    hist = [r["price"] for r in c.execute(
        "SELECT price FROM prices WHERE product_id=? AND store=? ORDER BY id DESC LIMIT 30", (pid, p.store))]
    flagged = None
    if len(hist) >= MIN_HISTORY:
        med = statistics.median(hist)
        if p.price > med * SUSPICIOUS_RATIO:
            flagged = "price_inflate"
            detail = f"السعر {p.price} أعلى من وسيطه {round(med, 2)} بأكثر من 30%"
        elif p.price <= med * GOOD_DEAL_RATIO:
            flagged = "good_deal"
            detail = f"السعر {p.price} أقل من وسيطه {round(med, 2)} بأكثر من 15%"
        if flagged:
            c.execute("INSERT INTO flags(product_id,store,type,detail,ts) VALUES(?,?,?,?,?)",
                      (pid, p.store, flagged, detail, now()))
    total = p.price + p.shipping
    c.execute("INSERT INTO prices(product_id,store,price,shipping,delivery_days,url,ts) VALUES(?,?,?,?,?,?,?)",
              (pid, p.store, p.price, p.shipping, p.delivery_days, p.url, now()))
    c.execute("""UPDATE alerts SET triggered=1, triggered_at=?, hit_store=?, hit_price=?
                 WHERE product_id=? AND triggered=0 AND target>=?""",
              (now(), p.store, total, pid, total))
    c.commit()
    c.close()
    return {"ok": True, "product_id": pid, "flag": flagged}


# ---------- 2) تتبع السعر الذكي ----------
@app.post("/api/alerts")
def add_alert(a: AlertIn):
    c = db()
    row = c.execute("SELECT id FROM products WHERE search_key=?", (normalize(a.product),)).fetchone()
    if not row:
        raise HTTPException(404, "المنتج غير موجود")
    c.execute("INSERT INTO alerts(product_id,target,contact) VALUES(?,?,?)", (row["id"], a.target, a.contact))
    c.commit()
    c.close()
    return {"ok": True}


@app.get("/api/alerts")
def my_alerts(contact: str):
    c = db()
    rows = c.execute("""SELECT a.*, p.name FROM alerts a JOIN products p ON p.id=a.product_id
                        WHERE contact=? ORDER BY a.id DESC""", (contact,)).fetchall()
    c.close()
    return [dict(r) for r in rows]


@app.get("/api/alerts/triggered")
def triggered_alerts(contact: str):
    """التنبيهات التي تحقق هدفها ولم تُرسل بعد (لتستخدمها مهمة الإرسال لاحقاً)."""
    c = db()
    rows = c.execute("""SELECT a.*, p.name FROM alerts a JOIN products p ON p.id=a.product_id
                        WHERE contact=? AND triggered=1 ORDER BY triggered_at DESC""", (contact,)).fetchall()
    c.close()
    return [dict(r) for r in rows]


# ---------- 3) مقارنة السلة الكاملة ----------
@app.post("/api/cart")
def cart(body: CartIn):
    c = db()
    stores, missing, found = {}, [], 0
    for item in body.items:
        p = c.execute("SELECT id FROM products WHERE search_key LIKE ?", (f"%{normalize(item)}%",)).fetchone()
        if not p:
            missing.append(item)
            continue
        found += 1
        for o in latest_offers(c, p["id"]):
            s = stores.setdefault(o["store"], {"store": o["store"], "total": 0, "count": 0})
            s["total"] = round(s["total"] + o["total"], 2)
            s["count"] += 1
    c.close()
    result = sorted(stores.values(), key=lambda s: (-s["count"], s["total"]))
    for s in result:
        s["complete"] = s["count"] == found
    return {"results": result, "missing": missing}


# ---------- 4) توقعات الأسعار ----------
@app.get("/api/predict")
def predict(product_id: int):
    c = db()
    rows = c.execute("""SELECT substr(ts,1,10) d, MIN(price+shipping) m FROM prices
                        WHERE product_id=? GROUP BY d ORDER BY d""", (product_id,)).fetchall()
    c.close()
    series = [r["m"] for r in rows]
    if len(series) < 3:
        return {"advice": "بيانات غير كافية للتوقع", "history": series}
    slope = statistics.linear_regression(list(range(len(series))), series).slope
    pct = slope / series[-1] * 100
    if pct < -0.5:
        advice = "السعر في اتجاه نازل — يُنصح بالانتظار"
    elif pct > 0.5:
        advice = "السعر في اتجاه صاعد — الأفضل الشراء الآن"
    else:
        advice = "السعر مستقر"
    return {"advice": advice, "trend_pct_per_day": round(pct, 2), "history": series}


# ---------- 5) بدائل أرخص تلقائياً ----------
@app.get("/api/alternatives")
def alternatives(product_id: int):
    c = db()
    base = c.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not base:
        raise HTTPException(404, "المنتج غير موجود")
    base_offers = latest_offers(c, product_id)
    if not base_offers:
        c.close()
        return []
    base_total = best_value(base_offers)["total"]
    words = set(base["search_key"].split())
    alts = []
    for p in c.execute("SELECT * FROM products WHERE id!=?", (product_id,)).fetchall():
        pw = set(p["search_key"].split())
        if not words or len(words & pw) / len(words) < 0.5:
            continue
        bv = best_value(latest_offers(c, p["id"]))
        if bv and bv["total"] < base_total:
            alts.append({"product_id": p["id"], "name": p["name"], "store": bv["store"],
                         "total": bv["total"], "saving": round(base_total - bv["total"], 2)})
    c.close()
    return sorted(alts, key=lambda x: x["total"])


# ---------- 6) مجتمع العروض (تحقق آلي + تصويت) ----------
@app.post("/api/deals")
def add_deal(d: DealIn):
    c = db()
    title_key = normalize(d.title)
    verified = 1 if d.url.startswith("https://") and d.price > 0 else 0
    if verified:
        for p in c.execute("SELECT * FROM products").fetchall():
            if p["search_key"] in title_key or title_key in p["search_key"]:
                offers = latest_offers(c, p["id"])
                if offers and d.price > statistics.median(o["price"] for o in offers) * 1.2:
                    verified = 0
    c.execute("INSERT INTO deals(title,store,price,url,verified,ts) VALUES(?,?,?,?,?,?)",
              (d.title, d.store, d.price, d.url, verified, now()))
    c.commit()
    c.close()
    return {"ok": True, "verified": bool(verified)}


@app.get("/api/deals")
def deals():
    c = db()
    rows = c.execute("SELECT * FROM deals WHERE verified=1 ORDER BY votes DESC, id DESC").fetchall()
    c.close()
    return [dict(r) for r in rows]


@app.post("/api/deals/{deal_id}/vote")
def vote(deal_id: int, up: bool = True):
    c = db()
    c.execute("UPDATE deals SET votes=votes+? WHERE id=?", (1 if up else -1, deal_id))
    c.commit()
    c.close()
    return {"ok": True}


# ---------- 7) مساعد التسوق ----------
CATEGORIES = {
    "جوال": ["ايفون", "سامسونج", "جوال", "هاتف"],
    "لابتوب": ["لابتوب", "كمبيوتر", "macbook", "asus"],
    "بقالة": ["ارز", "زيت", "سكر", "حليب", "مقاضي"],
}
STOP = {normalize(w) for w in ["أبحث", "ابحث", "عن", "بميزانية", "ميزانية", "ريال", "لي",
                               "أبغى", "ابغى", "في", "من", "ب", "و"]}


@app.post("/api/assistant")
def assistant(a: AskIn):
    text = normalize(a.text)
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", text)]
    budget = max(nums) if nums else None
    words = [w for w in re.findall(r"\w+", text) if not w.isdigit() and w not in STOP]

    terms = list(words)
    for cat, syn in CATEGORIES.items():
        syn_n = [normalize(s) for s in syn]
        if normalize(cat) in words or any(w in syn_n for w in words):
            terms += syn_n

    c = db()
    picks = []
    for p in c.execute("SELECT * FROM products").fetchall():
        if terms and not any(t in p["search_key"] for t in terms):
            continue
        bv = best_value(latest_offers(c, p["id"]))
        if bv and (budget is None or bv["total"] <= budget):
            picks.append({"product": p["name"], "store": bv["store"], "total": bv["total"],
                          "delivery_days": bv["delivery_days"]})
    c.close()
    return {"budget": budget, "picks": sorted(picks, key=lambda x: x["total"])}


init()


@app.get("/")
def home():
    return FileResponse(BASE / "static" / "index.html")


app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
