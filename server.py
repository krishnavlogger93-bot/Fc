#!/usr/bin/env python3
"""
E-commerce server: storefront API, order system and secured admin API.
Data is stored in Google Cloud Firestore (see firestore_client.py).

    export GOOGLE_APPLICATION_CREDENTIALS=/secure/path/service-account.json
    python3 server.py --seed     # first run: demo products and a banner
    python3 server.py            # serves http://127.0.0.1:8000  (admin panel at /admin)

Environment variables:
    GOOGLE_APPLICATION_CREDENTIALS   path to a Firebase service-account JSON key (local runs).
                                     Leave unset on Cloud Run: the attached service account is used.
    ADMIN_USERNAME, ADMIN_PASSWORD   admin login. If ADMIN_PASSWORD is unset on first run,
                                     a random password is generated and printed once.
    HOST, PORT                       bind address (default 127.0.0.1:8000)
    SECURE_COOKIES=1                 mark the admin cookie Secure (use when served over HTTPS)
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM, ADMIN_EMAIL
                                     optional: email the admin on every new order
"""
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import smtplib
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

import firestore_client as fsc

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE, "static")
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))
SECURE_COOKIES = os.environ.get("SECURE_COOKIES") == "1"
STORE_TZ = timezone(timedelta(hours=5, minutes=30))  # IST, for display timestamps

MAX_JSON_BYTES = 1_000_000  # product photos travel inside the JSON as data URLs (~500 KB max)
SESSION_SECONDS = 12 * 3600
PBKDF2_ROUNDS = 310_000
MAX_QTY_PER_ORDER = 10
MAX_ATTEMPTS = 8  # optimistic-concurrency retries for stock and status changes
ORDER_STATUSES = ("pending", "confirmed", "shipped", "delivered", "cancelled")

IMAGE_DATA_RE = re.compile(r"data:image/(jpeg|png|webp);base64,([A-Za-z0-9+/=]+)")
MAX_IMAGE_CHARS = 700_000  # Firestore documents are capped at 1 MiB, so photos are stored inline and kept small
ORDER_ID_RE = re.compile(r"ORD-[0-9A-F]{10}")
DOC_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
USERNAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
MOBILE_RE = re.compile(r"[6-9]\d{9}")
PIN_RE = re.compile(r"[1-9]\d{5}")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8", ".svg": "image/svg+xml",
    ".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp",
}
CSP = ("default-src 'self'; img-src 'self' data:; "
       "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; script-src 'self'; connect-src 'self'; "
       "form-action 'self'; frame-ancestors 'none'; base-uri 'self'")

_state_lock = threading.Lock()
_attempts = {}      # rate-limit buckets: key -> recent timestamps
fs = None           # Firestore client, created in main()
DUMMY_HASH = None   # used so login takes the same time for unknown usernames


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------- setup and passwords

def now_us():
    """Microsecond timestamp: orders sort by it, and the admin alerts use it as a cursor."""
    return time.time_ns() // 1000


def now_display():
    return datetime.now(STORE_TZ).strftime("%Y-%m-%d %H:%M:%S")


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
    return f"pbkdf2${salt.hex()}${digest.hex()}"


def check_password(password, stored):
    try:
        _, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), PBKDF2_ROUNDS)
    return hmac.compare_digest(digest.hex(), digest_hex)


def ensure_admin():
    if fs.query("admins", limit=1):
        return
    username = os.environ.get("ADMIN_USERNAME", "admin")
    if not USERNAME_RE.fullmatch(username):
        sys.exit("ADMIN_USERNAME may only contain letters, digits, '.', '_' and '-'.")
    password = os.environ.get("ADMIN_PASSWORD")
    generated = password is None
    if generated:
        password = secrets.token_urlsafe(12)
    try:
        fs.create("admins", {"pw_hash": hash_password(password), "created_at": now_display()}, doc_id=username)
    except fsc.Conflict:
        return
    print(f"[setup] Admin account created. Username: {username}")
    if generated:
        print(f"[setup] Generated password (shown once): {password}")
        print("[setup] Set ADMIN_PASSWORD before the first run to choose your own.")


def seed_demo():
    if fs.query("products", limit=1):
        print("[seed] Products already exist; skipping.")
        return
    demo = [
        ("Obsidian Chrono Watch", "Stainless steel case, sapphire crystal, 100m water resistance.", 4999_00, 3499_00, 12, True),
        ("Midnight Leather Wallet", "Full-grain leather, RFID-blocking sleeve, six card slots.", 1899_00, None, 30, False),
        ("Noir Wireless Earbuds", "Active noise cancellation, 30-hour battery, IPX5 sweat resistance.", 3299_00, 2599_00, 8, True),
    ]
    for name, desc, price, disc, stock, featured in demo:
        fs.create("products", {
            "name": name, "description": desc, "price_paise": price, "discount_paise": disc,
            "stock": stock, "image": None, "featured": featured, "active": True,
            "created_us": now_us(), "created_at": now_display(),
        })
    fs.create("banners", {
        "title": "Festive Sale — up to 30% off", "subtitle": "Limited stock on selected watches and audio.",
        "image": None, "link_product_id": None, "active": True, "sort": 0, "created_us": now_us(),
    })
    print("[seed] Demo products and banner added.")


# ---------------------------------------------------------------- validation

def text(value, label, min_len, max_len):
    if not isinstance(value, str):
        raise ApiError(400, f"{label} is required")
    value = value.strip()
    if len(value) < min_len:
        raise ApiError(400, f"{label} must be at least {min_len} characters")
    if len(value) > max_len:
        raise ApiError(400, f"{label} must be under {max_len} characters")
    return value


def integer(value, label, low, high):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ApiError(400, f"{label} must be a whole number")
    if not low <= number <= high:
        raise ApiError(400, f"{label} must be between {low} and {high}")
    return number


def doc_id(value, label):
    s = str(value or "").strip()
    if not DOC_ID_RE.fullmatch(s):
        raise ApiError(400, f"{label} is invalid")
    return s


def to_paise(value, label, required):
    if value in (None, ""):
        if required:
            raise ApiError(400, f"{label} is required")
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise ApiError(400, f"{label} must be a number")
    if not Decimal("0") <= amount <= Decimal("10000000"):
        raise ApiError(400, f"{label} is out of range")
    return int((amount * 100).to_integral_value())


def normalize_mobile(value):
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if not MOBILE_RE.fullmatch(digits):
        raise ApiError(400, "Enter a valid 10-digit Indian mobile number")
    return digits


def sniff_image(raw):
    if raw[:3] == b"\xff\xd8\xff":
        return "jpg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    return None


def valid_image(value):
    """Accepts a base64 data URL and checks that the bytes really are a JPG, PNG or WebP."""
    if value in (None, ""):
        return None
    if not isinstance(value, str) or len(value) > MAX_IMAGE_CHARS:
        raise ApiError(400, "Image is too large. Please choose a smaller photo.")
    m = IMAGE_DATA_RE.fullmatch(value)
    if not m:
        raise ApiError(400, "Image is invalid. Please upload it again.")
    try:
        raw = base64.b64decode(m[2], validate=True)
    except (binascii.Error, ValueError):
        raise ApiError(400, "Image is invalid. Please upload it again.")
    if sniff_image(raw) is None:
        raise ApiError(400, "Upload a JPG, PNG or WebP image")
    return value


def parse_product(d):
    price = to_paise(d.get("price"), "Price", required=True)
    discount = to_paise(d.get("discount_price"), "Discount price", required=False)
    if discount is not None and discount >= price:
        raise ApiError(400, "Discount price must be lower than the regular price")
    return {
        "name": text(d.get("name"), "Product name", 2, 120),
        "description": text(d.get("description") or "", "Description", 0, 2000),
        "price_paise": price,
        "discount_paise": discount,
        "stock": integer(d.get("stock", 0), "Stock", 0, 100_000),
        "image": valid_image(d.get("image")),
        "featured": bool(d.get("featured")),
        "active": d.get("active") is not False,
    }


def parse_banner(d):
    link = d.get("link_product_id")
    return {
        "title": text(d.get("title"), "Banner title", 2, 120),
        "subtitle": text(d.get("subtitle") or "", "Subtitle", 0, 200),
        "image": valid_image(d.get("image")),
        "link_product_id": doc_id(link, "Linked product") if link not in (None, "") else None,
        "active": d.get("active") is not False,
        "sort": integer(d.get("sort", 0), "Sort order", 0, 999),
    }


# ---------------------------------------------------------------- serializers

def money(paise):
    return None if paise is None else paise / 100


def product_out(r):
    return {
        "id": r["id"], "name": r.get("name", ""), "description": r.get("description", ""),
        "price": money(r.get("price_paise")), "discount_price": money(r.get("discount_paise")),
        "stock": r.get("stock", 0), "image": r.get("image"),
        "featured": bool(r.get("featured")), "active": bool(r.get("active", True)),
    }


def banner_out(r):
    return {"id": r["id"], "title": r.get("title", ""), "subtitle": r.get("subtitle", ""),
            "image": r.get("image"), "link_product_id": r.get("link_product_id"),
            "active": bool(r.get("active", True)), "sort": r.get("sort", 0)}


def order_out(r):
    return {
        "id": r["id"], "created_at": r.get("created_at", ""), "status": r.get("status"),
        "customer_name": r.get("customer_name"), "mobile": r.get("mobile"), "address": r.get("address"),
        "city": r.get("city"), "state": r.get("state"), "pin": r.get("pin"),
        "product_id": r.get("product_id"), "product_name": r.get("product_name"),
        "unit_price": money(r.get("unit_price_paise")), "quantity": r.get("quantity"),
        "total": money(r.get("total_paise")),
    }


# ---------------------------------------------------------------- catalogue

def get_product(pid, public=True):
    p = fs.get("products", pid)
    if p is None or (public and not p.get("active", True)):
        return None
    return p


def list_products(public=True):
    rows = fs.list_all("products")
    if public:
        rows = [r for r in rows if r.get("active", True)]
    rows.sort(key=lambda r: (bool(r.get("featured")), r.get("created_us", 0)), reverse=True)
    return [product_out(r) for r in rows]


def list_banners(public=True):
    rows = fs.list_all("banners")
    if public:
        rows = [r for r in rows if r.get("active", True)]
    rows.sort(key=lambda r: (r.get("sort", 0), r.get("created_us", 0)))
    return [banner_out(r) for r in rows]


def delete_doc(coll, did):
    try:
        fs.commit([fs.w_delete(coll, did)])
    except fsc.PreconditionFailed:
        raise ApiError(404, "Not found")


def update_doc_fields(coll, did, values, label):
    """Replace a product/banner, keeping its creation stamp."""
    existing = fs.get(coll, did)
    if existing is None:
        raise ApiError(404, f"{label} not found")
    fields = {**values,
              "created_us": existing.get("created_us", now_us()),
              "created_at": existing.get("created_at", now_display())}
    try:
        fs.commit([fs.w_replace(coll, did, fields, existing["_update_time"])])
    except fsc.PreconditionFailed:
        raise ApiError(409, "This was just changed by someone else. Reload and try again.")


# ---------------------------------------------------------------- orders

def place_order(d):
    name = text(d.get("customer_name"), "Full name", 2, 100)
    mobile = normalize_mobile(d.get("mobile"))
    address = text(d.get("address"), "Delivery address", 8, 300)
    city = text(d.get("city"), "City", 2, 80)
    state = text(d.get("state"), "State", 2, 80)
    pin = str(d.get("pin", "")).strip()
    if not PIN_RE.fullmatch(pin):
        raise ApiError(400, "Enter a valid 6-digit PIN code")
    product_id = doc_id(d.get("product_id"), "Product")
    qty = integer(d.get("quantity"), "Quantity", 1, MAX_QTY_PER_ORDER)

    for _ in range(MAX_ATTEMPTS):
        product = get_product(product_id, public=True)
        if product is None:
            raise ApiError(404, "This product is no longer available")
        stock = product.get("stock", 0)
        if stock < qty:
            if stock <= 0:
                raise ApiError(409, "Sorry, this item just sold out")
            raise ApiError(409, f"Only {stock} left in stock")
        unit = product["discount_paise"] if product.get("discount_paise") is not None else product["price_paise"]
        total = unit * qty
        order_id = "ORD-" + secrets.token_hex(5).upper()
        order = {
            "status": "pending", "customer_name": name, "mobile": mobile, "address": address,
            "city": city, "state": state, "pin": pin, "product_id": product_id,
            "product_name": product["name"], "unit_price_paise": unit, "quantity": qty,
            "total_paise": total, "created_us": now_us(), "created_at": now_display(),
        }
        try:
            # Stock decrement and order creation succeed or fail together.
            fs.commit([
                fs.w_increment("products", product_id, "stock", -qty, product["_update_time"]),
                fs.w_create("orders", order_id, order),
            ])
        except (fsc.PreconditionFailed, fsc.Conflict):
            continue  # someone else changed the stock meanwhile (or an ID clash): re-read and retry
        notify_admin(order_id, name, product["name"], qty, total)
        return {"order_id": order_id, "status": "pending", "product_name": product["name"],
                "quantity": qty, "total": money(total)}
    raise ApiError(409, "Please try again in a moment")


def track_order(d):
    order_id = text(d.get("order_id"), "Order ID", 6, 30).upper()
    mobile = normalize_mobile(d.get("mobile"))
    o = fs.get("orders", order_id) if ORDER_ID_RE.fullmatch(order_id) else None
    # Same generic message for "not found" and "wrong mobile" so the lookup can't be used to probe
    if o is None or not hmac.compare_digest(str(o.get("mobile", "")), mobile):
        raise ApiError(404, "No order matches that Order ID and mobile number")
    # Tracking never returns the address or other personal details
    return {"order_id": o["id"], "status": o.get("status"), "product_name": o.get("product_name"),
            "quantity": o.get("quantity"), "total": money(o.get("total_paise")), "placed_on": o.get("created_at")}


def set_order_status(order_id, status):
    if status not in ORDER_STATUSES:
        raise ApiError(400, "Invalid status")
    for _ in range(MAX_ATTEMPTS):
        order = fs.get("orders", order_id)
        if order is None:
            raise ApiError(404, "Order not found")
        if order.get("status") == "cancelled" and status != "cancelled":
            raise ApiError(409, "Cancelled orders cannot be reopened")
        writes = []
        if status == "cancelled" and order.get("status") != "cancelled" and order.get("product_id"):
            product = fs.get("products", order["product_id"])
            if product is not None:  # return the items to stock
                writes.append(fs.w_increment("products", order["product_id"], "stock",
                                             order.get("quantity", 0), product["_update_time"]))
        updated = {k: v for k, v in order.items() if not k.startswith("_") and k != "id"}
        updated["status"] = status
        writes.append(fs.w_replace("orders", order_id, updated, order["_update_time"]))
        try:
            fs.commit(writes)
            break
        except fsc.PreconditionFailed:
            continue
    else:
        raise ApiError(409, "Please try again in a moment")
    return order_out(fs.get("orders", order_id))


def dashboard_stats():
    orders = fs.query("orders")
    by_status = {s: 0 for s in ORDER_STATUSES}
    revenue = 0
    for o in orders:
        st = o.get("status")
        if st in by_status:
            by_status[st] += 1
        if st != "cancelled":
            revenue += o.get("total_paise") or 0
    products = fs.list_all("products")
    return {
        "orders_by_status": by_status,
        "total_orders": len(orders),
        "revenue": money(revenue),
        "products": len(products),
        "low_stock": sum(1 for p in products if p.get("active", True) and p.get("stock", 0) <= 5),
    }


def new_orders_since(qs):
    newest = fs.query("orders", order=("created_us", True), limit=1)
    latest = newest[0]["created_us"] if newest else 0
    if "after" not in qs:  # first poll: just report the current position
        return {"latest": latest, "new": []}
    try:
        after = max(0, int(qs["after"][0] or 0))
    except ValueError:
        after = latest
    rows = fs.query("orders", where=("created_us", "GREATER_THAN", after),
                    order=("created_us", False), limit=50)
    if rows:
        latest = rows[-1]["created_us"]  # ascending, so the cursor never skips an order
    else:
        latest = after
    return {"latest": latest, "new": [
        {"id": r["id"], "customer_name": r.get("customer_name"), "product_name": r.get("product_name"),
         "quantity": r.get("quantity"), "total": money(r.get("total_paise")), "created_at": r.get("created_at")}
        for r in rows]}


def notify_admin(order_id, customer, product, qty, total_paise):
    """Optional email alert. Runs in a background thread so checkout never waits on SMTP."""
    host, to = os.environ.get("SMTP_HOST"), os.environ.get("ADMIN_EMAIL")
    if not host or not to:
        return
    msg = EmailMessage()
    msg["Subject"] = f"New order {order_id} - Rs {total_paise / 100:,.2f}"
    msg["From"] = os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER") or to
    msg["To"] = to
    msg.set_content(f"Order {order_id} placed.\nProduct: {product} x {qty}\n"
                    f"Total: Rs {total_paise / 100:,.2f}\nCustomer: {customer}\n\n"
                    "Open the admin dashboard to see the delivery details.")

    def send():
        try:
            with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=20) as smtp:
                smtp.starttls()
                if os.environ.get("SMTP_USER"):
                    smtp.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASS", ""))
                smtp.send_message(msg)
        except Exception:
            traceback.print_exc()

    threading.Thread(target=send, daemon=True).start()


# ---------------------------------------------------------------- sessions and rate limiting

def allow(key, limit, window):
    now = time.time()
    with _state_lock:
        recent = [t for t in _attempts.get(key, []) if now - t < window]
        allowed = len(recent) < limit
        if allowed:
            recent.append(now)
        _attempts[key] = recent
        return allowed


def _session_key(token):
    # Only a hash of the token is stored, so a leaked database can't be used to log in
    return hashlib.sha256(token.encode()).hexdigest()


def create_session():
    token = secrets.token_urlsafe(32)
    fs.create("sessions", {"expires_us": now_us() + SESSION_SECONDS * 1_000_000}, doc_id=_session_key(token))
    return token


def valid_session(token):
    if not token:
        return False
    s = fs.get("sessions", _session_key(token))
    if s and s.get("expires_us", 0) > now_us():
        return True
    if s:
        end_session(token)
    return False


def end_session(token):
    if not token:
        return
    try:
        fs.commit([fs.w_delete("sessions", _session_key(token))])
    except fsc.FirestoreError:
        pass  # already gone


def cookie_header(value, max_age):
    header = f"sid={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}"
    return header + ("; Secure" if SECURE_COOKIES else "")


# ---------------------------------------------------------------- HTTP layer

class Handler(BaseHTTPRequestHandler):
    server_version = "Shop"
    sys_version = ""

    def log_message(self, *args):
        # Intentionally silent: request bodies contain customer details.
        pass

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_PATCH(self):
        self.dispatch("PATCH")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def dispatch(self, method):
        try:
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            if path.startswith("/api/"):
                return self.api(method, path, parse_qs(parsed.query))
            if method != "GET":
                raise ApiError(405, "Method not allowed")
            return self.serve_static(path)
        except ApiError as e:
            self.send_json(e.status, {"error": e.message})
        except fsc.FirestoreError:
            traceback.print_exc()
            self.send_json(503, {"error": "The store is temporarily unavailable. Please try again."})
        except Exception:
            traceback.print_exc()
            self.send_json(500, {"error": "Something went wrong. Please try again."})

    # ---- responses

    def send_bytes(self, status, data, content_type, extra_headers=()):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        for name, value in extra_headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, status, payload, extra_headers=()):
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_bytes(status, data, "application/json; charset=utf-8",
                        [("Cache-Control", "no-store")] + list(extra_headers))

    # ---- request helpers

    def read_body(self, limit):
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            raise ApiError(413, "That file or request is too large")
        return self.rfile.read(length) if length > 0 else b""

    def read_json(self):
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            raise ApiError(415, "Expected a JSON request")
        raw = self.read_body(MAX_JSON_BYTES)
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            raise ApiError(400, "Invalid request data")
        if not isinstance(data, dict):
            raise ApiError(400, "Invalid request data")
        return data

    def session_token(self):
        try:
            jar = cookies.SimpleCookie(self.headers.get("Cookie") or "")
        except cookies.CookieError:
            return None
        return jar["sid"].value if "sid" in jar else None

    # ---- routing

    def api(self, method, path, qs):
        client = self.client_address[0]

        if method == "GET" and path == "/api/banners":
            return self.send_json(200, {"banners": list_banners(public=True)})
        if method == "GET" and path == "/api/products":
            return self.send_json(200, {"products": list_products(public=True)})
        m = re.fullmatch(r"/api/products/(" + DOC_ID_RE.pattern + r")", path)
        if m and method == "GET":
            p = get_product(m[1], public=True)
            if not p:
                raise ApiError(404, "Product not found")
            return self.send_json(200, {"product": product_out(p)})
        if method == "POST" and path == "/api/orders":
            return self.send_json(201, place_order(self.read_json()))
        if method == "POST" and path == "/api/orders/track":
            if not allow("track:" + client, 30, 600):
                raise ApiError(429, "Too many requests. Please wait a few minutes.")
            return self.send_json(200, track_order(self.read_json()))

        if method == "POST" and path == "/api/admin/login":
            if not allow("login:" + client, 10, 900):
                raise ApiError(429, "Too many login attempts. Try again in 15 minutes.")
            d = self.read_json()
            username = str(d.get("username", ""))[:64].strip()
            user = fs.get("admins", username) if USERNAME_RE.fullmatch(username) else None
            stored = user.get("pw_hash", "") if user else DUMMY_HASH
            ok = check_password(str(d.get("password", "")), stored)
            if user is None or not ok:
                raise ApiError(401, "Invalid username or password")
            token = create_session()
            return self.send_json(200, {"ok": True}, [("Set-Cookie", cookie_header(token, SESSION_SECONDS))])

        if method == "POST" and path == "/api/admin/logout":
            end_session(self.session_token())
            return self.send_json(200, {"ok": True}, [("Set-Cookie", cookie_header("", 0))])

        if path.startswith("/api/admin/"):
            if not valid_session(self.session_token()):
                raise ApiError(401, "Please log in to continue")
            return self.admin_api(method, path, qs)

        raise ApiError(404, "Not found")

    def admin_api(self, method, path, qs):
        if path == "/api/admin/me" and method == "GET":
            return self.send_json(200, {"ok": True})
        if path == "/api/admin/stats" and method == "GET":
            return self.send_json(200, dashboard_stats())
        if path == "/api/admin/notifications" and method == "GET":
            return self.send_json(200, new_orders_since(qs))

        if path == "/api/admin/orders" and method == "GET":
            status = qs.get("status", [""])[0]
            if status and status not in ORDER_STATUSES:
                raise ApiError(400, "Invalid status")
            rows = fs.query("orders", order=("created_us", True), limit=500)
            if status:
                rows = [r for r in rows if r.get("status") == status]
            return self.send_json(200, {"orders": [order_out(r) for r in rows]})

        m = re.fullmatch(r"/api/admin/orders/(" + ORDER_ID_RE.pattern + r")", path)
        if m and method == "PATCH":
            data = self.read_json()
            return self.send_json(200, {"order": set_order_status(m[1], str(data.get("status", "")))})

        if path == "/api/admin/products":
            if method == "GET":
                return self.send_json(200, {"products": list_products(public=False)})
            if method == "POST":
                values = parse_product(self.read_json())
                values["created_us"] = now_us()
                values["created_at"] = now_display()
                return self.send_json(201, {"id": fs.create("products", values)["id"]})

        m = re.fullmatch(r"/api/admin/products/(" + DOC_ID_RE.pattern + r")", path)
        if m:
            pid = m[1]
            if method == "PUT":
                update_doc_fields("products", pid, parse_product(self.read_json()), "Product")
                return self.send_json(200, {"ok": True})
            if method == "DELETE":
                delete_doc("products", pid)  # past orders keep their own snapshot of the product
                return self.send_json(200, {"ok": True})

        if path == "/api/admin/banners":
            if method == "GET":
                return self.send_json(200, {"banners": list_banners(public=False)})
            if method == "POST":
                values = parse_banner(self.read_json())
                values["created_us"] = now_us()
                values["created_at"] = now_display()
                return self.send_json(201, {"id": fs.create("banners", values)["id"]})

        m = re.fullmatch(r"/api/admin/banners/(" + DOC_ID_RE.pattern + r")", path)
        if m:
            bid = m[1]
            if method == "PUT":
                update_doc_fields("banners", bid, parse_banner(self.read_json()), "Banner")
                return self.send_json(200, {"ok": True})
            if method == "DELETE":
                delete_doc("banners", bid)
                return self.send_json(200, {"ok": True})

        raise ApiError(404, "Not found")

    def serve_static(self, path):
        if path in ("", "/"):
            file_name = "index.html"
        elif path in ("/admin", "/admin/"):
            file_name = "admin.html"
        else:
            file_name = path.lstrip("/")
        root = os.path.realpath(STATIC_DIR)
        full = os.path.realpath(os.path.join(root, file_name))
        if not full.startswith(root + os.sep) or not os.path.isfile(full):
            raise ApiError(404, "Not found")
        ext = os.path.splitext(full)[1]
        with open(full, "rb") as f:
            data = f.read()
        headers = [("Cache-Control", "no-cache")]
        if ext == ".html":
            headers.append(("Content-Security-Policy", CSP))
        self.send_bytes(200, data, CONTENT_TYPES.get(ext, "application/octet-stream"), headers)


def main():
    global fs, DUMMY_HASH
    creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or None  # unset on Cloud Run
    key_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON") or None  # for Render and similar hosts
    if creds and not os.path.isfile(creds):
        sys.exit("GOOGLE_APPLICATION_CREDENTIALS points to a file that does not exist.")
    if key_json:
        try:
            key_info = json.loads(key_json)
        except ValueError:
            sys.exit("FIREBASE_SERVICE_ACCOUNT_JSON is not valid JSON. Paste the whole key file content.")
        fs = fsc.Client(credentials_info=key_info)
    else:
        fs = fsc.Client(creds)
    try:
        ensure_admin()  # also confirms that Firestore is reachable
    except fsc.FirestoreError as e:
        sys.exit(f"Could not connect to Firestore: {e.message}")
    DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
    if "--seed" in sys.argv or os.environ.get("SEED") == "1":  # SEED=1 for hosts where you can't pass flags
        seed_demo()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Store:  http://{HOST}:{PORT}/")
    print(f"Admin:  http://{HOST}:{PORT}/admin")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
