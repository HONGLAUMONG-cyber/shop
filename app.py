from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP
from functools import wraps
import hashlib
import hmac
import html
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
import uuid
from urllib.parse import quote, urlencode, urlsplit

# Load environment variables before other imports
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / '.env')

from i18n import LANGUAGES, translate

from cryptography.fernet import Fernet
from flask import (
    Flask,
    abort,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
import httpx
from werkzeug.middleware.proxy_fix import ProxyFix


CARD_TYPES = {
    "viettel": "Viettel",
    "vinaphone": "VinaPhone",
    "mobifone": "MobiFone",
    "vietnamobile": "Vietnamobile",
    "zing": "Zing",
    "garena": "Garena",
    "gate": "Gate",
    "vcoin": "Vcoin",
}
FAIL_STATUSES = {
    "failed",
    "cancelled",
    "canceled",
    "refunded",
    "rejected",
    "error",
    "expired",
}
log = logging.getLogger("zenlig.shop")
_worker_lock = threading.Lock()
_worker_started_paths: set[str] = set()


class ShopError(Exception):
    pass


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.config.update(
        SECRET_KEY=os.getenv("SHOP_SESSION_SECRET", ""),
        DB_PATH=os.getenv("DB_PATH", ""),
        BOT_TOKEN=os.getenv("BOT_TOKEN", ""),
        QCST_API_KEY=os.getenv("QCST_API_KEY", ""),
        QCST_BASE_URL=os.getenv("QCST_BASE_URL", "https://api.qcst.tech").rstrip("/"),
        TELEGRAM_BOT_USERNAME=os.getenv("TELEGRAM_BOT_USERNAME", "").lstrip("@"),
        ADMIN_IDS=os.getenv("ADMIN_IDS", ""),
        MARKUP_PERCENT=os.getenv("MARKUP_PERCENT", "45"),
        BANK_CODE=os.getenv("BANK_CODE", ""),
        BANK_ACCOUNT=os.getenv("BANK_ACCOUNT", ""),
        ACCOUNT_NAME=os.getenv("ACCOUNT_NAME", ""),
        USDT_TRC20_ADDRESS=os.getenv("USDT_TRC20_ADDRESS", ""),
        USDT_VND_RATE=os.getenv("USDT_VND_RATE", "22000"),
        MIN_TOPUP=os.getenv("MIN_TOPUP", "10000"),
        MAX_TOPUP=os.getenv("MAX_TOPUP", "50000000"),
        TEST_PRODUCT_IDS=os.getenv("TEST_PRODUCT_IDS", ""),
        PPAY_PARTNER_ID=os.getenv("PPAY_PARTNER_ID", ""),
        PPAY_PARTNER_KEY=os.getenv("PPAY_PARTNER_KEY", ""),
        PPAY_API_URL=os.getenv("PPAY_API_URL", "https://ppay.vn/chargingws/v2"),
        DOITHE_CARD_ENCRYPTION_KEY=os.getenv("DOITHE_CARD_ENCRYPTION_KEY", ""),
        ENABLE_ORDER_WORKER=os.getenv("SHOP_ORDER_WORKER", "1") == "1",
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 14,
    )
    if test_config:
        app.config.update(test_config)

    missing = [
        key
        for key in (
            "SECRET_KEY",
            "DB_PATH",
            "BOT_TOKEN",
            "QCST_API_KEY",
            "TELEGRAM_BOT_USERNAME",
            "BANK_CODE",
            "BANK_ACCOUNT",
            "ACCOUNT_NAME",
            "USDT_TRC20_ADDRESS",
        )
        if not str(app.config.get(key, "")).strip()
    ]
    if missing:
        raise RuntimeError("Thiếu cấu hình bắt buộc: " + ", ".join(missing))
    if len(str(app.config["SECRET_KEY"])) < 32:
        raise RuntimeError("SHOP_SESSION_SECRET phải có ít nhất 32 ký tự")
    db_path = Path(app.config["DB_PATH"])
    if not db_path.is_absolute():
        raise RuntimeError("DB_PATH phải là đường dẫn tuyệt đối tới shop.db hiện có")
    if not db_path.is_file():
        raise RuntimeError(f"Không tìm thấy database shop hiện có: {db_path}")
    try:
        app.config["ADMIN_IDS"] = {
            int(item.strip())
            for item in str(app.config["ADMIN_IDS"]).split(",")
            if item.strip()
        }
        app.config["MARKUP_PERCENT"] = Decimal(str(app.config["MARKUP_PERCENT"]))
        app.config["USDT_VND_RATE"] = Decimal(str(app.config["USDT_VND_RATE"]))
        app.config["MIN_TOPUP"] = int(app.config["MIN_TOPUP"])
        app.config["MAX_TOPUP"] = int(app.config["MAX_TOPUP"])
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RuntimeError("Cấu hình giá, tỷ giá, giới hạn hoặc ADMIN_IDS không hợp lệ") from exc
    if not app.config["ADMIN_IDS"]:
        raise RuntimeError("ADMIN_IDS phải có ít nhất một Telegram ID quản trị")
    if (
        not app.config["USDT_VND_RATE"].is_finite()
        or app.config["USDT_VND_RATE"] <= 0
        or app.config["MIN_TOPUP"] <= 0
        or app.config["MAX_TOPUP"] < app.config["MIN_TOPUP"]
    ):
        raise RuntimeError("Tỷ giá hoặc giới hạn nạp tiền không hợp lệ")
    encryption_key = str(app.config["DOITHE_CARD_ENCRYPTION_KEY"]).strip()
    app.extensions["card_encryptor"] = (
        Fernet(encryption_key.encode()) if encryption_key else None
    )
    ppay_partner_id = str(app.config["PPAY_PARTNER_ID"]).strip()
    ppay_partner_key = str(app.config["PPAY_PARTNER_KEY"]).strip()
    if ppay_partner_id and ppay_partner_key and encryption_key:
        ppay_url = urlsplit(str(app.config["PPAY_API_URL"]).rstrip("/"))
        if (
            ppay_url.scheme != "https"
            or ppay_url.hostname != "ppay.vn"
            or ppay_url.path != "/chargingws/v2"
            or ppay_url.username is not None
            or ppay_url.password is not None
            or ppay_url.query
            or ppay_url.fragment
        ):
            raise RuntimeError("PPAY_API_URL phải là https://ppay.vn/chargingws/v2")


    init_schema(app.config["DB_PATH"])

    @app.before_request
    def load_current_account():
        g.account = None
        g.wallet = None
        account_id = session.get("account_id")
        if account_id is not None:
            g.account = get_db().execute(
                "SELECT * FROM web_accounts WHERE id=?", (account_id,)
            ).fetchone()
            if g.account:
                g.wallet = get_db().execute(
                    "SELECT * FROM users WHERE id=?",
                    (g.account["wallet_user_id"],),
                ).fetchone()
        if request.method == "POST":
            supplied = request.form.get("csrf_token", "")
            expected = session.get("csrf_token", "")
            if not expected or not hmac.compare_digest(supplied, expected):
                abort(400, "Yêu cầu không hợp lệ, vui lòng tải lại trang.")

    @app.context_processor
    def inject_template_values():
        return {
            "csrf_token": csrf_token,
            "money": money,
            "price_for": lambda product: sell_price(app, product),
            "card_types": CARD_TYPES,
            "current_account": getattr(g, "account", None),
            "current_wallet": getattr(g, "wallet", None),
            "is_admin": is_admin(),
            "telegram_widget_url": telegram_widget_url,
            "languages": LANGUAGES,
            "current_language": current_language(),
            "tr": template_translate,
            "product_category": product_category,
            "product_name": lambda product: localized_product_field(product, "name"),
            "product_description": lambda product: localized_product_field(product, "description"),
            "product_warranty": lambda product: localized_product_field(product, "warranty"),
            "product_available": is_available,
            "product_stock": stock_quantity,
        }

    @app.template_filter("datetime")
    def format_datetime(value):
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(value)))

    @app.template_filter("json_pretty")
    def json_pretty(value):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            pass
        return json.dumps(value, ensure_ascii=False, indent=2)

    @app.template_filter("topup_method")
    def render_topup_method(value):
        return topup_method_label(value)

    @app.teardown_appcontext
    def close_db(_error=None):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    @app.get("/")
    def index():
        products = []
        query = request.args.get("q", "").strip()
        category_filter = request.args.get("category", "").strip()
        all_categories = set()
        try:
            all_products = fetch_products(app)
            for product in all_products:
                if not visible_product(product, g.account, app.config["ADMIN_IDS"]):
                    continue
                all_categories.add(product_category(product))
                if query:
                    searchable = (
                        localized_product_field(product, "name")
                        + " "
                        + localized_product_field(product, "description")
                        + " "
                        + product.get("code", "")
                    ).lower()
                    if query.lower() not in searchable:
                        continue
                if category_filter and product_category(product) != category_filter:
                    continue
                products.append(product)
        except (ShopError, httpx.HTTPError):
            log.exception("Không tải được danh sách sản phẩm QCST")
            flash("Hiện chưa tải được sản phẩm. Vui lòng thử lại sau.", "error")
        categories = sorted(all_categories)
        return render_template(
            "index.html",
            products=products,
            query=query,
            category_filter=category_filter,
            categories=categories,
        )

    @app.get("/products/<path:product_id>")
    @login_required
    def product_page(product_id: str):
        try:
            product = fetch_product(app, product_id)
        except (ShopError, httpx.HTTPError):
            log.exception("Không tải được sản phẩm QCST")
            flash("Không tải được sản phẩm. Vui lòng thử lại.", "error")
            return redirect(url_for("index"))
        if not visible_product(product, g.account, app.config["ADMIN_IDS"]):
            abort(404)
        return render_template("product.html", product=product)

    @app.post("/orders/<path:product_id>")
    @login_required
    def create_order(product_id: str):
        try:
            product = fetch_product(app, product_id)
        except (ShopError, httpx.HTTPError):
            log.exception("Không tải được sản phẩm trước khi tạo đơn")
            flash("Không xác minh được giá và tồn kho. Chưa tạo đơn.", "error")
            return redirect(url_for("index"))
        if not visible_product(product, g.account, app.config["ADMIN_IDS"]):
            abort(404)
        try:
            qty = int(request.form.get("quantity", ""))
        except ValueError:
            flash("Số lượng phải là số nguyên dương.", "error")
            return redirect(url_for("product_page", product_id=product_id))
        minimum = int(product.get("min_quantity") or 1)
        maximum = product.get("max_quantity")
        stock = stock_quantity(product)
        fixed = product.get("fixed_quantity")
        if fixed:
            qty = int(fixed)
        if (
            qty < minimum
            or (maximum is not None and qty > int(maximum))
            or (stock is not None and qty > stock)
            or not is_available(product)
        ):
            flash("Số lượng không hợp lệ hoặc sản phẩm đã hết hàng.", "error")
            return redirect(url_for("product_page", product_id=product_id))

        unit_price = sell_price(app, product)
        subtotal = unit_price * qty
        conn = get_db()
        try:
            with transaction(conn):
                user = conn.execute(
                    "SELECT balance,next_discount_percent FROM users WHERE id=?",
                    (g.account["wallet_user_id"],),
                ).fetchone()
                if user is None:
                    raise ShopError("Không tìm thấy ví người dùng.")
                discount = int(user["next_discount_percent"] or 0)
                total = subtotal * (100 - discount) // 100
                if int(user["balance"]) < total:
                    raise ShopError("Số dư không đủ; hãy nạp tiền rồi thử lại.")
                cur = conn.execute(
                    "INSERT INTO orders("
                    "user_id,product_id,product_name,qty,unit_price,total,status,"
                    "discount_percent,created_at,source"
                    ") VALUES(?,?,?,?,?,?,'creating',?,?, 'web')",
                    (
                        g.account["wallet_user_id"],
                        str(product["id"]),
                        str(product.get("name") or "Sản phẩm"),
                        qty,
                        unit_price,
                        total,
                        discount,
                        int(time.time()),
                    ),
                )
                order_id = int(cur.lastrowid)
                client_order_id = f"web{order_id}-{uuid.uuid4().hex[:10]}"
                conn.execute(
                    "UPDATE orders SET client_order_id=? WHERE id=?",
                    (client_order_id, order_id),
                )
                changed = conn.execute(
                    "UPDATE users SET balance=balance-?,next_discount_percent=0 "
                    "WHERE id=? AND balance>=?",
                    (total, g.account["wallet_user_id"], total),
                ).rowcount
                if changed != 1:
                    raise ShopError("Số dư vừa thay đổi. Tải lại trang và thử lại.")
        except ShopError as exc:
            flash(str(exc), "error")
            return redirect(url_for("product_page", product_id=product_id))
        except sqlite3.Error:
            log.exception("Không tạo được giao dịch đơn hàng")
            flash("Chưa tạo được đơn hàng do lỗi cơ sở dữ liệu.", "error")
            return redirect(url_for("product_page", product_id=product_id))

        payload = {
            "client_order_id": client_order_id,
            "product_id": product["id"],
            "quantity": qty,
            "customer_inputs": [],
            "locale": "vi",
            "max_unit_price": product["price"],
        }
        try:
            result = qcst_request(
                app,
                "POST",
                "/v1/orders",
                json=payload,
                headers={"Idempotency-Key": client_order_id},
            )["data"]
        except QCSTRejected as exc:
            if exc.retryable:
                mark_order_review(app.config["DB_PATH"], order_id)
                alert_admins(app, f"Đơn web #{order_id} cần kiểm tra QCST: {html.escape(exc.detail)}")
                flash("QCST chưa xác nhận kết quả; đơn được chuyển kiểm tra, chưa hoàn tiền.", "warning")
            else:
                refund_order(app.config["DB_PATH"], order_id, "failed")
                flash("QCST từ chối đơn; số dư đã được hoàn lại.", "error")
            return redirect(url_for("orders"))
        except (httpx.HTTPError, ShopError):
            log.exception("Kết quả tạo đơn QCST không xác định cho đơn #%s", order_id)
            mark_order_review(app.config["DB_PATH"], order_id)
            alert_admins(app, f"Đơn web #{order_id} có kết quả QCST chưa xác định, vui lòng kiểm tra.")
            flash("Chưa xác định được trạng thái từ QCST; đơn đã chuyển admin kiểm tra, chưa hoàn tiền.", "warning")
            return redirect(url_for("orders"))

        qcst_order_id = result.get("id")
        if not qcst_order_id:
            mark_order_review(app.config["DB_PATH"], order_id)
            alert_admins(app, f"QCST không trả mã đơn cho đơn web #{order_id}; cần kiểm tra thủ công.")
            flash("QCST chưa trả mã đơn hợp lệ; đơn đã chuyển admin kiểm tra.", "warning")
            return redirect(url_for("orders"))
        with sqlite_connection(app.config["DB_PATH"]) as conn:
            conn.execute(
                "UPDATE orders SET qcst_order_id=?,status='processing' "
                "WHERE id=? AND status='creating'",
                (str(qcst_order_id), order_id),
            )
            conn.commit()
        stage_referral_reward(
            app.config["DB_PATH"], order_id, total, app.config["USDT_VND_RATE"]
        )
        flash(f"Đã tạo đơn #{order_id}. Đơn sẽ được cập nhật tự động.", "success")
        return redirect(url_for("orders"))

    @app.get("/orders")
    @login_required
    def orders():
        rows = get_db().execute(
            "SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 100",
            (g.account["wallet_user_id"],),
        ).fetchall()
        return render_template("orders.html", orders=rows)

    @app.get("/topups")
    @login_required
    def topups():
        rows = get_db().execute(
            "SELECT t.*,d.card_type,d.denomination FROM topups t "
            "LEFT JOIN card_topup_details d ON d.topup_id=t.id "
            "WHERE t.user_id=? ORDER BY t.id DESC LIMIT 100",
            (g.account["wallet_user_id"],),
        ).fetchall()
        return render_template("topups.html", topups=rows)

    @app.post("/topups/bank")
    @login_required
    def create_bank_topup():
        amount = parse_vnd(request.form.get("amount", ""))
        if amount is None:
            flash("Nhập số tiền VND hợp lệ.", "error")
            return redirect(url_for("topups"))
        try:
            topup_id, code = create_topup(
                app.config["DB_PATH"],
                g.account["wallet_user_id"],
                amount,
                "bank",
            )
        except ShopError as exc:
            flash(str(exc), "error")
            return redirect(url_for("topups"))
        return redirect(url_for("topup_detail", topup_id=topup_id))

    @app.post("/topups/usdt")
    @login_required
    def create_usdt_topup():
        value = request.form.get("usdt_amount", "").strip()
        if not re.fullmatch(r"\d{1,12}(?:\.\d{1,6})?", value):
            flash("Số USDT không hợp lệ (tối đa 6 chữ số thập phân).", "error")
            return redirect(url_for("topups"))
        try:
            usdt = Decimal(value)
        except InvalidOperation:
            flash("Số USDT không hợp lệ.", "error")
            return redirect(url_for("topups"))
        if not usdt.is_finite() or usdt <= 0:
            flash("Số USDT phải lớn hơn 0.", "error")
            return redirect(url_for("topups"))
        amount = int(
            (usdt * app.config["USDT_VND_RATE"]).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
        if not valid_topup_amount(app, amount):
            flash("Số tiền quy đổi nằm ngoài giới hạn nạp.", "error")
            return redirect(url_for("topups"))
        try:
            topup_id, _ = create_topup(
                app.config["DB_PATH"],
                g.account["wallet_user_id"],
                amount,
                "usdt",
                usdt_amount=format(usdt, "f"),
            )
        except ShopError as exc:
            flash(str(exc), "error")
            return redirect(url_for("topups"))
        return redirect(url_for("topup_detail", topup_id=topup_id))

    @app.post("/topups/card")
    @login_required
    def create_card_topup():
        encryptor = app.extensions["card_encryptor"]
        if not app.config["PPAY_PARTNER_ID"] or not app.config["PPAY_PARTNER_KEY"] or not encryptor:
            flash("Nạp thẻ chưa được cấu hình an toàn; vui lòng dùng phương thức khác.", "error")
            return redirect(url_for("topups"))
        card_type = request.form.get("card_type", "").strip().lower()
        pin = request.form.get("card_pin", "").strip()
        serial = request.form.get("serial", "").strip()
        try:
            denomination = int(request.form.get("denomination", ""))
        except ValueError:
            denomination = 0
        if (
            card_type not in CARD_TYPES
            or not valid_topup_amount(app, denomination)
            or not pin.isdigit()
            or not 6 <= len(pin) <= 30
            or not serial.isdigit()
            or not 6 <= len(serial) <= 30
            or hmac.compare_digest(pin, serial)
        ):
            flash("Thông tin thẻ không hợp lệ. Chỉ gửi thẻ chưa sử dụng.", "error")
            return redirect(url_for("topups"))
        try:
            topup_id, code = create_card_topup(
                app.config["DB_PATH"],
                g.account["wallet_user_id"],
                card_type,
                denomination,
                pin,
                serial,
                encryptor,
            )
        except ShopError as exc:
            flash(str(exc), "error")
            return redirect(url_for("topups"))

        fields = ppay_fields(app, card_type, pin, serial, denomination, topup_id)
        try:
            response = httpx.post(
                app.config["PPAY_API_URL"],
                files={key: (None, str(value)) for key, value in fields.items()},
                timeout=30,
            )
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, ValueError):
            log.exception("Không xác định được kết quả gửi thẻ PPay #%s", topup_id)
            set_topup_status(app.config["DB_PATH"], topup_id, "processing")
            notified = alert_admins(
                app,
                f"Thẻ PPay #{topup_id}: không rõ PPay đã nhận yêu cầu hay chưa. Kiểm tra bằng /cardtopups.",
            )
            flash("PPay chưa trả kết quả xác định; yêu cầu được giữ để admin kiểm tra, không gửi lại thẻ.", "warning")
            if not notified:
                flash("Không gửi được thông báo Telegram; admin có thể xem yêu cầu tại website.", "error")
            return redirect(url_for("topup_detail", topup_id=topup_id))

        status, received = parse_ppay_result(result)
        if status == "99":
            set_topup_status(app.config["DB_PATH"], topup_id, "processing")
            notified = alert_admins(
                app, f"Thẻ PPay #{topup_id} đang được xử lý; kiểm tra bằng /cardtopups."
            )
            flash("PPay đang xử lý thẻ. Admin sẽ kiểm tra trạng thái.", "success")
        elif status in {"1", "2"}:
            accepted = received if received and 0 < received <= denomination else None
            store_card_verification(
                app.config["DB_PATH"], topup_id, accepted, denomination
            )
            notified = alert_admins(
                app, f"PPay xác nhận thẻ #{topup_id}; admin cần kiểm tra và duyệt."
            )
            flash(
                "PPay xác nhận thẻ. Admin cần duyệt số tiền thực nhận."
                if accepted
                else "PPay báo thành công nhưng chưa có số tiền thực nhận; admin cần nhập số tiền.",
                "success",
            )
        elif status == "3":
            reject_card(app.config["DB_PATH"], topup_id)
            notified = alert_admins(app, f"PPay từ chối thẻ #{topup_id}.")
            flash("PPay từ chối thẻ.", "error")
        else:
            set_topup_status(app.config["DB_PATH"], topup_id, "processing")
            notified = alert_admins(
                app,
                f"PPay trả trạng thái không xác định cho thẻ #{topup_id}; kiểm tra bằng /cardtopups.",
            )
            flash("PPay trả trạng thái chưa xác định; admin sẽ kiểm tra.", "warning")
        if not notified:
            flash("Không gửi được thông báo Telegram; admin có thể xem yêu cầu tại website.", "error")
        return redirect(url_for("topup_detail", topup_id=topup_id))

    @app.get("/topups/<int:topup_id>")
    @login_required
    def topup_detail(topup_id: int):
        row = get_db().execute(
            "SELECT t.*,d.card_type,d.denomination FROM topups t "
            "LEFT JOIN card_topup_details d ON d.topup_id=t.id "
            "WHERE t.id=? AND t.user_id=?",
            (topup_id, g.account["wallet_user_id"]),
        ).fetchone()
        if row is None:
            abort(404)
        qr_url = None
        if row["method"] == "bank":
            params = urlencode(
                {
                    "amount": row["amount"],
                    "addInfo": row["code"],
                    "accountName": app.config["ACCOUNT_NAME"],
                }
            )
            qr_url = (
                f"https://img.vietqr.io/image/{quote(app.config['BANK_CODE'])}-"
                f"{quote(app.config['BANK_ACCOUNT'])}-compact2.png?{params}"
            )
        return render_template("topup_detail.html", topup=row, qr_url=qr_url)

    @app.post("/topups/<int:topup_id>/paid")
    @login_required
    def notify_topup_paid(topup_id: int):
        conn = get_db()
        row = conn.execute(
            "SELECT * FROM topups WHERE id=? AND user_id=?",
            (topup_id, g.account["wallet_user_id"]),
        ).fetchone()
        if row is None:
            abort(404)
        if row["method"] == "card":
            abort(400, "Yêu cầu thẻ được PPay cập nhật riêng.")
        if row["status"] != "pending":
            flash("Yêu cầu này đã được gửi hoặc xử lý.", "warning")
            return redirect(url_for("topup_detail", topup_id=topup_id))
        if row["method"] == "usdt" and not valid_txid(request.form.get("txid", "")):
            flash("TXID TRON phải gồm đúng 64 ký tự hex.", "error")
            return redirect(url_for("topup_detail", topup_id=topup_id))
        try:
            with transaction(conn):
                if row["method"] == "usdt":
                    conn.execute(
                        "UPDATE topups SET txid=?,status='notified' "
                        "WHERE id=? AND status='pending' AND txid IS NULL",
                        (request.form["txid"].lower(), topup_id),
                    )
                else:
                    conn.execute(
                        "UPDATE topups SET status='notified' "
                        "WHERE id=? AND status='pending'",
                        (topup_id,),
                    )
                updated = conn.execute(
                    "SELECT changes() AS changed"
                ).fetchone()["changed"]
                if updated != 1:
                    raise ShopError("Yêu cầu đã được gửi hoặc xử lý.")
        except sqlite3.IntegrityError:
            flash("TXID này đã được dùng cho yêu cầu nạp khác.", "error")
            return redirect(url_for("topup_detail", topup_id=topup_id))
        except ShopError as exc:
            flash(str(exc), "warning")
            return redirect(url_for("topup_detail", topup_id=topup_id))
        text = (
            f"Yêu cầu nạp {row['method']} #{topup_id}; user {row['user_id']}; "
            f"VND {row['amount']}; mã {row['code']}"
        )
        if row["method"] == "usdt":
            text += f"; USDT {row['usdt_amount']}; TXID {request.form['txid'].lower()}"
        text += ". Kiểm tra giao dịch rồi xác nhận trong bot (/topups) hoặc website admin."
        notified = alert_admins(app, text, topup_id=topup_id)
        if notified:
            flash("Đã gửi yêu cầu xác nhận cho admin. Số dư sẽ cộng sau khi duyệt.", "success")
        else:
            flash(
                "Yêu cầu đã lưu nhưng thông báo Telegram chưa gửi được; admin vẫn có thể duyệt tại website.",
                "warning",
            )
        return redirect(url_for("topup_detail", topup_id=topup_id))

    @app.get("/account")
    @login_required
    def account():
        session["telegram_state"] = secrets.token_urlsafe(24)
        return render_template("account.html")

    @app.post("/account/credentials")
    @login_required
    def update_credentials():
        email = normalize_email(request.form.get("email", ""))
        password = request.form.get("password", "")
        if not g.account["email"] and email and not password:
            flash("Đặt mật khẩu cùng email để có thể đăng nhập bằng tài khoản website.", "error")
            return redirect(url_for("account"))
        if not g.account["password_hash"] and password and not email:
            flash("Nhập email cùng mật khẩu để hoàn tất đăng nhập website.", "error")
            return redirect(url_for("account"))
        if email and not valid_email(email):
            flash("Địa chỉ email không hợp lệ.", "error")
            return redirect(url_for("account"))
        if password and len(password) < 10:
            flash("Mật khẩu phải có ít nhất 10 ký tự.", "error")
            return redirect(url_for("account"))
        if not email and not password:
            flash("Nhập email hoặc mật khẩu mới.", "error")
            return redirect(url_for("account"))
        try:
            conn = get_db()
            if password:
                conn.execute(
                    "UPDATE web_accounts SET email=COALESCE(NULLIF(?,''),email),"
                    "password_hash=? WHERE id=?",
                    (email, password_hash(password), g.account["id"]),
                )
            elif email:
                conn.execute(
                    "UPDATE web_accounts SET email=? WHERE id=?",
                    (email, g.account["id"]),
                )
            else:
                flash("Nhập email hoặc mật khẩu mới.", "error")
                return redirect(url_for("account"))
            conn.commit()
            g.account = conn.execute(
                "SELECT * FROM web_accounts WHERE id=?", (g.account["id"],)
            ).fetchone()
        except sqlite3.IntegrityError:
            flash("Email này đã được đăng ký.", "error")
            return redirect(url_for("account"))
        flash("Thông tin đăng nhập đã được cập nhật.", "success")
        return redirect(url_for("account"))

    @app.post("/account/language")
    @login_required
    def change_language():
        lang = request.form.get("language", "").strip()
        if lang not in LANGUAGES:
            flash("Ngôn ngữ không hợp lệ.", "error")
            return redirect(url_for("account"))
        conn = get_db()
        conn.execute(
            "UPDATE users SET language=? WHERE id=?",
            (lang, g.account["wallet_user_id"]),
        )
        conn.commit()
        g.wallet = conn.execute(
            "SELECT * FROM users WHERE id=?", (g.account["wallet_user_id"],)
        ).fetchone()
        flash("Ngôn ngữ hiển thị đã được cập nhật.", "success")
        return redirect(url_for("account"))

    @app.get("/coupons")
    @login_required
    def coupons():
        conn = get_db()
        user_coupons = conn.execute(
            "SELECT * FROM coupon_requests WHERE user_id=? ORDER BY created_at DESC LIMIT 20",
            (g.account["wallet_user_id"],),
        ).fetchall()
        return render_template("coupons.html", coupons=user_coupons)

    @app.post("/coupons")
    @login_required
    def submit_coupon():
        code = request.form.get("code", "").strip().upper()
        if not code or len(code) > 50:
            flash("Mã coupon không hợp lệ.", "error")
            return redirect(url_for("coupons"))
        conn = get_db()
        existing = conn.execute(
            "SELECT status FROM coupon_requests WHERE user_id=? AND code=?",
            (g.account["wallet_user_id"], code),
        ).fetchone()
        if existing:
            flash(f"Bạn đã gửi mã coupon này. Trạng thái: {existing['status']}", "error")
            return redirect(url_for("coupons"))
        conn.execute(
            "INSERT INTO coupon_requests(user_id,code,status,created_at) VALUES(?,?,'pending',?)",
            (g.account["wallet_user_id"], code, int(time.time())),
        )
        conn.commit()
        flash("Đã gửi yêu cầu coupon. Admin sẽ xem xét.", "success")
        return redirect(url_for("coupons"))

    @app.get("/admin/coupons")
    @login_required
    @admin_required
    def admin_coupons():
        conn = get_db()
        pending = conn.execute(
            "SELECT c.*,u.id as telegram_id FROM coupon_requests c "
            "JOIN users u ON u.id=c.user_id "
            "WHERE c.status='pending' ORDER BY c.created_at ASC"
        ).fetchall()
        recent = conn.execute(
            "SELECT c.*,u.id as telegram_id FROM coupon_requests c "
            "JOIN users u ON u.id=c.user_id "
            "WHERE c.status!='pending' ORDER BY c.created_at DESC LIMIT 50"
        ).fetchall()
        return render_template("admin_coupons.html", pending=pending, recent=recent)

    @app.post("/admin/coupons/<int:coupon_id>/approve")
    @login_required
    @admin_required
    def approve_coupon(coupon_id: int):
        action = request.form.get("action", "").strip()
        try:
            amount = int(request.form.get("amount", "0"))
        except ValueError:
            amount = 0
        try:
            discount = int(request.form.get("discount", "0"))
        except ValueError:
            discount = 0
        if action not in {"credit", "discount"} or (action == "credit" and amount <= 0) or (action == "discount" and not 0 < discount <= 100):
            flash("Hành động hoặc giá trị không hợp lệ.", "error")
            return redirect(url_for("admin_coupons"))
        conn = get_db()
        coupon = conn.execute(
            "SELECT * FROM coupon_requests WHERE id=?", (coupon_id,)
        ).fetchone()
        if not coupon or coupon["status"] != "pending":
            flash("Coupon không tồn tại hoặc đã xử lý.", "error")
            return redirect(url_for("admin_coupons"))
        with transaction(conn):
            if action == "credit":
                conn.execute(
                    "UPDATE users SET balance=balance+? WHERE id=?",
                    (amount, coupon["user_id"]),
                )
                conn.execute(
                    "UPDATE coupon_requests SET status='approved' WHERE id=?",
                    (coupon_id,),
                )
                flash(f"Đã cộng {money(amount)} cho user {coupon['user_id']}.", "success")
            else:
                conn.execute(
                    "UPDATE users SET next_discount_percent=? WHERE id=?",
                    (discount, coupon["user_id"]),
                )
                conn.execute(
                    "UPDATE coupon_requests SET status='approved' WHERE id=?",
                    (coupon_id,),
                )
                flash(f"Đã đặt giảm giá {discount}% cho đơn kế tiếp của user {coupon['user_id']}.", "success")
        return redirect(url_for("admin_coupons"))

    @app.post("/admin/coupons/<int:coupon_id>/reject")
    @login_required
    @admin_required
    def reject_coupon(coupon_id: int):
        conn = get_db()
        coupon = conn.execute(
            "SELECT status FROM coupon_requests WHERE id=?", (coupon_id,)
        ).fetchone()
        if not coupon or coupon["status"] != "pending":
            flash("Coupon không tồn tại hoặc đã xử lý.", "error")
            return redirect(url_for("admin_coupons"))
        conn.execute(
            "UPDATE coupon_requests SET status='rejected' WHERE id=?", (coupon_id,)
        )
        conn.commit()
        flash(f"Đã từ chối coupon #{coupon_id}.", "success")
        return redirect(url_for("admin_coupons"))

    @app.get("/referrals")
    @login_required
    def referrals():
        conn = get_db()
        wallet = g.wallet
        referrer_id = wallet["referrer_id"] if wallet else None

        # Get user's referral link
        telegram_id = g.account["telegram_id"]
        referral_code = str(telegram_id) if telegram_id else None

        # Count referrals made by this user
        referred_count = conn.execute(
            "SELECT COUNT(*) as count FROM users WHERE referrer_id=?",
            (g.account["wallet_user_id"],),
        ).fetchone()["count"]

        # Get referral rewards
        rewards = conn.execute(
            "SELECT r.*,o.product_name,o.total FROM referral_rewards r "
            "JOIN orders o ON o.id=r.order_id "
            "WHERE r.referrer_id=? OR r.referred_id=? "
            "ORDER BY r.created_at DESC LIMIT 50",
            (g.account["wallet_user_id"], g.account["wallet_user_id"]),
        ).fetchall()

        # Calculate totals
        total_commission = sum(r["commission_amount"] for r in rewards if r["referrer_id"] == g.account["wallet_user_id"] and r["status"] == "settled")
        total_bonus = sum(r["referred_bonus"] + r["referrer_bonus"] for r in rewards if r["status"] == "settled")

        return render_template(
            "referrals.html",
            referral_code=referral_code,
            referrer_id=referrer_id,
            referred_count=referred_count,
            rewards=rewards,
            total_commission=total_commission,
            total_bonus=total_bonus,
        )

    @app.get("/ref/<int:referrer_telegram_id>")
    def referral_landing(referrer_telegram_id: int):
        if not g.account:
            session["pending_referrer"] = referrer_telegram_id
            flash("Đăng ký hoặc đăng nhập để sử dụng link giới thiệu.", "success")
            return redirect(url_for("register"))

        conn = get_db()
        wallet = g.wallet

        # Check if already has referrer
        if wallet and wallet["referrer_id"]:
            flash("Bạn đã được giới thiệu bởi người khác.", "error")
            return redirect(url_for("index"))

        # Check self-referral
        if g.account["telegram_id"] and int(g.account["telegram_id"]) == referrer_telegram_id:
            flash("Không thể tự giới thiệu bản thân.", "error")
            return redirect(url_for("index"))

        # Verify referrer exists
        referrer = conn.execute(
            "SELECT id FROM users WHERE id=?", (referrer_telegram_id,)
        ).fetchone()
        if not referrer:
            flash("Link giới thiệu không hợp lệ.", "error")
            return redirect(url_for("index"))

        # Set referrer
        conn.execute(
            "UPDATE users SET referrer_id=? WHERE id=? AND referrer_id IS NULL",
            (referrer_telegram_id, g.account["wallet_user_id"]),
        )
        conn.commit()
        session.pop("pending_referrer", None)
        flash("Đã ghi nhận người giới thiệu. Bạn sẽ nhận thưởng khi mua hàng lần đầu!", "success")
        return redirect(url_for("index"))

    @app.get("/notifications")
    @login_required
    def notifications():
        conn = get_db()
        settings = conn.execute(
            "SELECT * FROM notification_settings WHERE user_id=?",
            (g.account["wallet_user_id"],),
        ).fetchone()
        if not settings:
            # Create default settings
            conn.execute(
                "INSERT INTO notification_settings(user_id,restock,new_product,buyer,review,giveaway,coupon,flash_sale) "
                "VALUES(?,1,1,1,0,1,1,0)",
                (g.account["wallet_user_id"],),
            )
            conn.commit()
            settings = conn.execute(
                "SELECT * FROM notification_settings WHERE user_id=?",
                (g.account["wallet_user_id"],),
            ).fetchone()
        return render_template("notifications.html", settings=settings)

    @app.post("/notifications")
    @login_required
    def update_notifications():
        conn = get_db()
        restock = 1 if request.form.get("restock") else 0
        new_product = 1 if request.form.get("new_product") else 0
        buyer = 1 if request.form.get("buyer") else 0
        review = 1 if request.form.get("review") else 0
        giveaway = 1 if request.form.get("giveaway") else 0
        coupon = 1 if request.form.get("coupon") else 0
        flash_sale = 1 if request.form.get("flash_sale") else 0

        conn.execute(
            "INSERT OR REPLACE INTO notification_settings"
            "(user_id,restock,new_product,buyer,review,giveaway,coupon,flash_sale) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (g.account["wallet_user_id"], restock, new_product, buyer, review, giveaway, coupon, flash_sale),
        )
        conn.commit()
        flash("Đã cập nhật cài đặt thông báo.", "success")
        return redirect(url_for("notifications"))

    @app.get("/giveaway")
    def giveaway():
        return render_template("giveaway.html")

    @app.get("/support")
    def support():
        return render_template("support.html")

    @app.get("/auth/telegram")
    def telegram_auth():
        values = request.args.to_dict(flat=True)
        received_state = values.pop("state", "")
        is_link = values.pop("link", "") == "1"
        if not validate_telegram_auth(app.config["BOT_TOKEN"], values):
            abort(403, "Xác thực Telegram không hợp lệ hoặc đã hết hạn.")
        expected_state = session.pop("telegram_state", "")
        if not expected_state or not hmac.compare_digest(received_state, expected_state):
            abort(403, "Phiên đăng nhập Telegram đã hết hạn. Hãy tải lại trang.")
        try:
            telegram_id = int(values["id"])
        except (KeyError, ValueError):
            abort(400, "Thiếu Telegram user ID.")
        if is_link:
            if not g.account:
                flash("Đăng nhập website trước khi liên kết Telegram.", "error")
                return redirect(url_for("login"))
            try:
                link_telegram_account(app, g.account["id"], telegram_id)
            except ShopError as exc:
                flash(str(exc), "error")
                return redirect(url_for("account"))
            session["telegram_state"] = None
            flash("Đã liên kết Telegram; số dư và lịch sử đã được gộp an toàn.", "success")
            return redirect(url_for("account"))
        account_id = account_for_telegram(app.config["DB_PATH"], telegram_id)
        session.clear()
        session["account_id"] = account_id
        session.permanent = True
        flash("Đăng nhập Telegram thành công.", "success")
        return redirect(url_for("index"))

    @app.get("/register")
    def register():
        session["telegram_state"] = secrets.token_urlsafe(24)
        return render_template("auth.html", mode="register")

    @app.post("/register")
    def register_post():
        email = normalize_email(request.form.get("email", ""))
        password = request.form.get("password", "")
        if not valid_email(email) or len(password) < 10:
            flash("Nhập email hợp lệ và mật khẩu có ít nhất 10 ký tự.", "error")
            return redirect(url_for("register"))
        try:
            with sqlite_connection(app.config["DB_PATH"]) as conn:
                with transaction(conn):
                    cur = conn.execute(
                        "INSERT INTO web_accounts(email,password_hash,wallet_user_id,created_at) "
                        "VALUES(?,?,0,?)",
                        (email, password_hash(password), int(time.time())),
                    )
                    account_id = int(cur.lastrowid)
                    wallet_id = -account_id
                    conn.execute(
                        "UPDATE web_accounts SET wallet_user_id=? WHERE id=?",
                        (wallet_id, account_id),
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO users(user_id, created_at) VALUES(?, ?)",
                        (wallet_id, int(datetime.now(timezone.utc).timestamp())),
                    )
        except sqlite3.IntegrityError:
            flash("Email này đã được đăng ký.", "error")
            return redirect(url_for("register"))
        login_session(account_id)
        flash("Tạo tài khoản thành công.", "success")
        return redirect(url_for("account"))

    @app.get("/login")
    def login():
        session["telegram_state"] = secrets.token_urlsafe(24)
        return render_template("auth.html", mode="login")

    @app.post("/login")
    def login_post():
        email = normalize_email(request.form.get("email", ""))
        password = request.form.get("password", "")
        account_row = get_db().execute(
            "SELECT id,password_hash FROM web_accounts WHERE email=?",
            (email,),
        ).fetchone()
        if (
            not account_row
            or not account_row["password_hash"]
            or not verify_password(password, account_row["password_hash"])
        ):
            flash("Email hoặc mật khẩu không chính xác.", "error")
            return redirect(url_for("login"))
        login_session(account_row["id"])
        flash("Đăng nhập thành công.", "success")
        return redirect(url_for("index"))

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("index"))

    @app.get("/admin/topups")
    @login_required
    @admin_required
    def admin_topups():
        rows = get_db().execute(
            "SELECT t.*,d.card_type,d.denomination FROM topups t "
            "LEFT JOIN card_topup_details d ON d.topup_id=t.id "
            "WHERE t.status IN ('pending','notified','submitting','processing',"
            "'awaiting_amount','verified') ORDER BY t.id DESC LIMIT 100"
        ).fetchall()
        return render_template("admin_topups.html", topups=rows)

    @app.post("/admin/topups/<int:topup_id>/approve")
    @login_required
    @admin_required
    def approve_topup(topup_id: int):
        try:
            approve_or_reject_topup(app.config["DB_PATH"], topup_id, approve=True)
        except ShopError as exc:
            flash(str(exc), "error")
            return redirect(url_for("admin_topups"))
        notify_topup_user(app, topup_id, approved=True)
        flash(f"Đã duyệt yêu cầu nạp #{topup_id}.", "success")
        return redirect(url_for("admin_topups"))

    @app.post("/admin/topups/<int:topup_id>/reject")
    @login_required
    @admin_required
    def reject_topup_route(topup_id: int):
        try:
            approve_or_reject_topup(app.config["DB_PATH"], topup_id, approve=False)
        except ShopError as exc:
            flash(str(exc), "error")
            return redirect(url_for("admin_topups"))
        notify_topup_user(app, topup_id, approved=False)
        flash(f"Đã từ chối yêu cầu nạp #{topup_id}.", "success")
        return redirect(url_for("admin_topups"))

    @app.post("/admin/topups/<int:topup_id>/amount")
    @login_required
    @admin_required
    def set_card_amount(topup_id: int):
        try:
            amount = int(request.form.get("amount", ""))
        except ValueError:
            amount = 0
        conn = get_db()
        row = conn.execute(
            "SELECT t.status,d.denomination FROM topups t "
            "JOIN card_topup_details d ON d.topup_id=t.id "
            "WHERE t.id=? AND t.method='card'",
            (topup_id,),
        ).fetchone()
        if (
            not row
            or row["status"] not in {"awaiting_amount", "processing", "submitting"}
            or not 0 < amount <= int(row["denomination"])
        ):
            flash("Số tiền thực nhận không hợp lệ hoặc yêu cầu đã được xử lý.", "error")
            return redirect(url_for("admin_topups"))
        with transaction(conn):
            conn.execute(
                "UPDATE topups SET amount=?,status='verified' WHERE id=? "
                "AND method='card' AND status IN ('awaiting_amount','processing','submitting')",
                (amount, topup_id),
            )
            conn.execute(
                "UPDATE card_topup_details SET encrypted_data='' WHERE topup_id=?",
                (topup_id,),
            )
        notify_topup_user(app, topup_id, approved=None)
        flash(f"Đã lưu số tiền thực nhận cho thẻ #{topup_id}; cần duyệt để cộng số dư.", "success")
        return redirect(url_for("admin_topups"))

    if app.config["ENABLE_ORDER_WORKER"]:
        start_order_worker(app.config["DB_PATH"], app.config)
    return app


def csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not getattr(g, "account", None):
            flash("Đăng nhập để tiếp tục.", "error")
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_admin():
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def is_admin() -> bool:
    account = getattr(g, "account", None)
    return bool(
        account
        and account["telegram_id"] is not None
        and int(account["telegram_id"]) in current_app.config["ADMIN_IDS"]
    )


def current_language() -> str:
    wallet = getattr(g, "wallet", None)
    if wallet:
        lang = wallet["language"] if hasattr(wallet, "__getitem__") else getattr(wallet, "language", None)
        if lang in LANGUAGES:
            return lang
    return "vi"


def template_translate(key: str, **values) -> str:
    return translate(current_language(), key, **values)


def product_category(product: dict) -> str:
    """
    Derive product category from QCST metadata. Port of bot product_category().
    Returns category key suitable for translate("category.<key>").
    """
    category = product.get("category", "").strip()
    group = product.get("group", "").strip()
    service_name = product.get("service_name", "").strip()
    metadata = product.get("metadata")
    if isinstance(metadata, dict):
        category = category or metadata.get("category", "").strip()
        group = group or metadata.get("group", "").strip()
        attrs = metadata.get("attributes", {})
        if isinstance(attrs, dict):
            category = category or attrs.get("category", "").strip()
            service_name = service_name or attrs.get("service", "").strip()

    for field in (category, group, service_name):
        if not field:
            continue
        slug = unicodedata.normalize("NFKD", field.lower())
        slug = re.sub(r"[^\w\s-]", "", slug).strip()
        slug = re.sub(r"[-\s]+", "-", slug)
        if slug in {
            "google",
            "gemini",
            "chatgpt",
            "openai",
            "canva",
            "youtube",
            "grok",
            "vpn",
            "claude",
            "capcut",
            "netflix",
            "deepseek",
            "coursera",
            "order",
        }:
            return slug

    name_slug = unicodedata.normalize("NFKD", product.get("name", "").lower())
    name_slug = re.sub(r"[^\w\s-]", "", name_slug).strip()
    for keyword in (
        "google",
        "gemini",
        "chatgpt",
        "gpt",
        "canva",
        "youtube",
        "vpn",
        "claude",
        "capcut",
        "netflix",
        "deepseek",
        "coursera",
    ):
        if keyword in name_slug:
            if keyword == "gpt":
                return "chatgpt"
            return keyword
    return "other"


def localized_product_field(product: dict, field: str) -> str:
    """Return localized field (name/description/warranty) or fallback to default."""
    lang = current_language()
    if lang != "vi":
        localized = product.get(f"{field}_{lang}", "").strip()
        if localized:
            return localized
    return product.get(field, "").strip()


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = open_connection(current_app.config["DB_PATH"])
    return g.db


def open_connection(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=20000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def sqlite_connection(path: str):
    conn = open_connection(path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_schema(path: str) -> None:
    with sqlite_connection(path) as conn:
        required = {"users", "orders", "topups", "card_topup_details"}
        existing = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        missing = required - existing
        if missing:
            raise RuntimeError(
                "Database không phải shop database mong đợi; thiếu bảng: "
                + ", ".join(sorted(missing))
            )
        order_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(orders)")
        }
        if "delivery" not in order_columns:
            conn.execute("ALTER TABLE orders ADD COLUMN delivery TEXT")
        if "source" not in order_columns:
            conn.execute(
                "ALTER TABLE orders ADD COLUMN source TEXT NOT NULL DEFAULT 'telegram'"
            )
        if "qcst_order_id" not in order_columns:
            conn.execute("ALTER TABLE orders ADD COLUMN qcst_order_id TEXT")
        user_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(users)")
        }
        if "language" not in user_columns:
            conn.execute(
                "ALTER TABLE users ADD COLUMN language TEXT NOT NULL DEFAULT 'vi'"
            )
        if "next_discount_percent" not in user_columns:
            conn.execute(
                "ALTER TABLE users ADD COLUMN next_discount_percent INTEGER NOT NULL DEFAULT 0"
            )
        if "referrer_id" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN referrer_id INTEGER")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS coupon_requests(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                code TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notification_settings(
                user_id INTEGER PRIMARY KEY,
                restock INTEGER NOT NULL DEFAULT 1,
                new_product INTEGER NOT NULL DEFAULT 1,
                buyer INTEGER NOT NULL DEFAULT 1,
                review INTEGER NOT NULL DEFAULT 0,
                giveaway INTEGER NOT NULL DEFAULT 1,
                coupon INTEGER NOT NULL DEFAULT 1,
                flash_sale INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS referral_rewards(
                order_id INTEGER PRIMARY KEY,
                referrer_id INTEGER NOT NULL,
                referred_id INTEGER NOT NULL,
                commission_amount INTEGER NOT NULL DEFAULT 0,
                referrer_bonus INTEGER NOT NULL DEFAULT 0,
                referred_bonus INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                approved_at INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS web_accounts(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE,
                password_hash TEXT,
                telegram_id INTEGER UNIQUE,
                wallet_user_id INTEGER NOT NULL UNIQUE,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_web_accounts_email "
            "ON web_accounts(email) WHERE email IS NOT NULL"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_web_accounts_telegram "
            "ON web_accounts(telegram_id) WHERE telegram_id IS NOT NULL"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_topups_txid "
            "ON topups(txid) WHERE txid IS NOT NULL"
        )
        conn.commit()


def login_session(account_id: int) -> None:
    session.clear()
    session["account_id"] = int(account_id)
    session["csrf_token"] = secrets.token_urlsafe(32)
    session.permanent = True


def telegram_widget_url():
    account = getattr(g, "account", None)
    if not account:
        return None
    state = session.get("telegram_state")
    if not state:
        state = secrets.token_urlsafe(24)
        session["telegram_state"] = state
    return url_for(
        "telegram_auth",
        link=1,
        state=state,
        _external=True,
        _scheme="https",
    )


def validate_telegram_auth(bot_token: str, values: dict[str, str]) -> bool:
    received_hash = values.get("hash", "")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", received_hash):
        return False
    try:
        auth_date = int(values.get("auth_date", ""))
        int(values.get("id", ""))
    except ValueError:
        return False
    now = int(time.time())
    if auth_date > now + 30 or now - auth_date > 86400:
        return False
    data_check_string = "\n".join(
        f"{key}={value}"
        for key, value in sorted(values.items())
        if key != "hash"
    )
    secret_key = hashlib.sha256(bot_token.encode()).digest()
    expected = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received_hash.lower())


def account_for_telegram(path: str, telegram_id: int) -> int:
    with sqlite_connection(path) as conn:
        with transaction(conn):
            row = conn.execute(
                "SELECT id FROM web_accounts WHERE telegram_id=?", (telegram_id,)
            ).fetchone()
            if row:
                return int(row["id"])
            conn.execute("INSERT OR IGNORE INTO users(id) VALUES(?)", (telegram_id,))
            cur = conn.execute(
                "INSERT INTO web_accounts(telegram_id,wallet_user_id,created_at) "
                "VALUES(?,?,?)",
                (telegram_id, telegram_id, int(time.time())),
            )
            return int(cur.lastrowid)


def link_telegram_account(app: Flask, account_id: int, telegram_id: int) -> None:
    conn = get_db()
    with transaction(conn):
        account = conn.execute(
            "SELECT * FROM web_accounts WHERE id=?", (account_id,)
        ).fetchone()
        if account is None:
            raise ShopError("Tài khoản website không còn tồn tại.")
        if account["telegram_id"] is not None:
            if int(account["telegram_id"]) == telegram_id:
                return
            raise ShopError("Tài khoản website đã liên kết với Telegram khác.")
        another = conn.execute(
            "SELECT id FROM web_accounts WHERE telegram_id=?", (telegram_id,)
        ).fetchone()
        if another:
            raise ShopError("Telegram này đã liên kết với một tài khoản website khác.")

        old_id = int(account["wallet_user_id"])
        if old_id != telegram_id:
            conn.execute("INSERT OR IGNORE INTO users(id) VALUES(?)", (telegram_id,))
            old_user = conn.execute(
                "SELECT * FROM users WHERE id=?", (old_id,)
            ).fetchone()
            tg_user = conn.execute(
                "SELECT * FROM users WHERE id=?", (telegram_id,)
            ).fetchone()
            if (
                old_user["next_discount_percent"]
                and tg_user["next_discount_percent"]
                and old_user["next_discount_percent"] != tg_user["next_discount_percent"]
            ):
                raise ShopError("Hãy dùng ưu đãi đơn kế tiếp trước khi gộp hai tài khoản.")
            if (
                old_user["referrer_id"] is not None
                and tg_user["referrer_id"] is not None
                and old_user["referrer_id"] != tg_user["referrer_id"]
            ):
                raise ShopError("Hai tài khoản có nguồn giới thiệu khác nhau; cần admin hỗ trợ liên kết.")

            conn.execute("UPDATE orders SET user_id=? WHERE user_id=?", (telegram_id, old_id))
            conn.execute("UPDATE topups SET user_id=? WHERE user_id=?", (telegram_id, old_id))
            conn.execute(
                "UPDATE coupon_requests SET user_id=? WHERE user_id=?",
                (telegram_id, old_id),
            )
            conn.execute(
                "UPDATE referral_rewards SET referrer_id=? WHERE referrer_id=?",
                (telegram_id, old_id),
            )
            conn.execute(
                "UPDATE referral_rewards SET referred_id=? WHERE referred_id=?",
                (telegram_id, old_id),
            )
            conn.execute(
                "UPDATE users SET referrer_id=? WHERE referrer_id=?",
                (telegram_id, old_id),
            )
            old_settings = conn.execute(
                "SELECT * FROM notification_settings WHERE user_id=?", (old_id,)
            ).fetchone()
            tg_settings = conn.execute(
                "SELECT * FROM notification_settings WHERE user_id=?", (telegram_id,)
            ).fetchone()
            if old_settings:
                if tg_settings:
                    keys = (
                        "restock",
                        "new_product",
                        "buyer",
                        "review",
                        "giveaway",
                        "coupon",
                        "flash_sale",
                    )
                    conn.execute(
                        "UPDATE notification_settings SET "
                        + ",".join(f"{key}=MAX({key},?)" for key in keys)
                        + " WHERE user_id=?",
                        tuple(old_settings[key] for key in keys) + (telegram_id,),
                    )
                    conn.execute(
                        "DELETE FROM notification_settings WHERE user_id=?", (old_id,)
                    )
                else:
                    conn.execute(
                        "UPDATE notification_settings SET user_id=? WHERE user_id=?",
                        (telegram_id, old_id),
                    )
            conn.execute(
                "UPDATE users SET balance=balance+?,"
                "next_discount_percent=CASE WHEN next_discount_percent=0 "
                "THEN ? ELSE next_discount_percent END,"
                "referrer_id=COALESCE(referrer_id,?) WHERE id=?",
                (
                    old_user["balance"],
                    old_user["next_discount_percent"],
                    old_user["referrer_id"],
                    telegram_id,
                ),
            )
            conn.execute("DELETE FROM users WHERE id=?", (old_id,))
        conn.execute(
            "UPDATE web_accounts SET telegram_id=?,wallet_user_id=? WHERE id=?",
            (telegram_id, telegram_id, account_id),
        )


def normalize_email(email: str) -> str:
    return email.strip().lower()


def valid_email(email: str) -> bool:
    return len(email) <= 254 and bool(
        re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)
    )


def password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000)
    import base64

    return "pbkdf2_sha256$310000$" + base64.urlsafe_b64encode(salt + digest).decode()


def verify_password(password: str, stored: str) -> bool:
    import base64

    try:
        algorithm, rounds, encoded = stored.split("$", 2)
        if algorithm != "pbkdf2_sha256" or int(rounds) != 310_000:
            return False
        raw = base64.urlsafe_b64decode(encoded.encode())
        salt, expected = raw[:16], raw[16:]
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(rounds))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def fetch_products(app: Flask) -> list[dict]:
    body = qcst_request(app, "GET", "/v1/products")
    data = body.get("data")
    if not isinstance(data, list):
        raise ShopError("QCST trả về danh sách sản phẩm không hợp lệ")
    return data


def fetch_product(app: Flask, product_id: str) -> dict:
    body = qcst_request(app, "GET", f"/v1/products/{quote(product_id, safe='')}")
    data = body.get("data")
    if not isinstance(data, dict):
        raise ShopError("QCST trả về thông tin sản phẩm không hợp lệ")
    return data


def qcst_request(app: Flask, method: str, path: str, **kwargs) -> dict:
    headers = dict(kwargs.pop("headers", {}))
    headers["X-API-Key"] = app.config["QCST_API_KEY"]
    response = httpx.request(
        method,
        app.config["QCST_BASE_URL"] + path,
        headers=headers,
        timeout=30,
        **kwargs,
    )
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code >= 400:
        raise QCSTRejected(
            str(body.get("error_code", "")),
            str(body.get("detail", response.text[:200])),
            bool(body.get("retryable", response.status_code in (429, 500, 503))),
        )
    if not isinstance(body, dict):
        raise ShopError("QCST trả về dữ liệu không hợp lệ")
    return body


class QCSTRejected(ShopError):
    def __init__(self, code: str, detail: str, retryable: bool):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.retryable = retryable


def stock_quantity(product: dict) -> int | None:
    for key in (
        "stock_quantity",
        "available_quantity",
        "available_stock",
        "quantity_in_stock",
        "inventory_quantity",
        "stock",
    ):
        value = product.get(key)
        if value is None or isinstance(value, dict):
            continue
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            continue
    stock = product.get("stock")
    if isinstance(stock, dict):
        for key in ("quantity", "available", "available_quantity"):
            if stock.get(key) is not None:
                try:
                    return max(0, int(stock[key]))
                except (TypeError, ValueError):
                    continue
    return None


def is_available(product: dict) -> bool:
    quantity = stock_quantity(product)
    if quantity is not None and quantity < int(product.get("min_quantity") or 1):
        return False
    availability = product.get("availability")
    if isinstance(availability, dict):
        availability = " ".join(str(value) for value in availability.values())
    elif isinstance(availability, (list, tuple)):
        availability = " ".join(map(str, availability))
    return not any(
        word in str(availability or "").lower()
        for word in ("out", "sold", "unavail", "empty", "het", "hết")
    )


def is_test_product(product: dict) -> bool:
    from flask import current_app

    if str(product.get("id", "")) in {
        item.strip()
        for item in str(current_app.config.get("TEST_PRODUCT_IDS", "")).split(",")
        if item.strip()
    }:
        return True
    labels = []
    for key in (
        "name",
        "slug",
        "code",
        "type",
        "product_type",
        "category",
        "description",
    ):
        value = product.get(key)
        if isinstance(value, str):
            labels.append(value.lower())
    tags = product.get("tags")
    if isinstance(tags, list):
        labels.extend(str(tag).lower() for tag in tags)
    markers = ("test api", "api test", "test_api", "api thử nghiệm", "test product")
    return bool(product.get("is_test") or product.get("test_mode") or any(
        marker in label for marker in markers for label in labels
    ))


def visible_product(product: dict, account, admins: set[int]) -> bool:
    return not is_test_product(product) or bool(
        account
        and account["telegram_id"] is not None
        and int(account["telegram_id"]) in admins
    )


def sell_price(app: Flask, product: dict) -> int:
    try:
        raw = Decimal(str(product["price"]))
    except (KeyError, InvalidOperation, TypeError) as exc:
        raise ShopError("QCST trả về giá sản phẩm không hợp lệ") from exc
    return int(
        (raw * (Decimal("100") + app.config["MARKUP_PERCENT"]) / Decimal("100"))
        .quantize(Decimal("1"), rounding=ROUND_CEILING)
    )


def money(value: int) -> str:
    return f"{int(value):,}".replace(",", ".") + "đ"


def parse_vnd(value: str) -> int | None:
    normalized = value.strip().replace(".", "").replace(",", "")
    if not normalized.isdigit():
        return None
    amount = int(normalized)
    return amount if amount > 0 else None


def valid_topup_amount(app: Flask, amount: int) -> bool:
    return app.config["MIN_TOPUP"] <= amount <= app.config["MAX_TOPUP"]


def generate_topup_code(conn: sqlite3.Connection, wallet_id: int) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    for _ in range(20):
        suffix = "".join(secrets.choice(alphabet) for _ in range(5))
        code = f"NAP{wallet_id}{suffix}"
        if not conn.execute("SELECT 1 FROM topups WHERE code=?", (code,)).fetchone():
            return code
    raise ShopError("Không tạo được mã nạp duy nhất.")


def create_topup(
    path: str,
    wallet_id: int,
    amount: int,
    method: str,
    usdt_amount: str | None = None,
) -> tuple[int, str]:
    if method not in {"bank", "usdt"}:
        raise ShopError("Phương thức nạp không hợp lệ.")
    with sqlite_connection(path) as conn:
        with transaction(conn):
            code = generate_topup_code(conn, wallet_id)
            cur = conn.execute(
                "INSERT INTO topups(user_id,amount,code,status,created_at,method,usdt_amount) "
                "VALUES(?,?,?,'pending',?,?,?)",
                (wallet_id, amount, code, int(time.time()), method, usdt_amount),
            )
            return int(cur.lastrowid), code


def create_card_topup(
    path: str,
    wallet_id: int,
    card_type: str,
    denomination: int,
    pin: str,
    serial: str,
    encryptor: Fernet,
) -> tuple[int, str]:
    with sqlite_connection(path) as conn:
        with transaction(conn):
            code = generate_topup_code(conn, wallet_id)
            cur = conn.execute(
                "INSERT INTO topups(user_id,amount,code,status,created_at,method) "
                "VALUES(?,0,?,'submitting',?,'card')",
                (wallet_id, code, int(time.time())),
            )
            topup_id = int(cur.lastrowid)
            encrypted_data = encryptor.encrypt(
                json.dumps(
                    {"card_type": card_type, "card_pin": pin, "serial": serial},
                    separators=(",", ":"),
                ).encode()
            ).decode()
            conn.execute(
                "INSERT INTO card_topup_details("
                "topup_id,encrypted_data,card_type,denomination,provider,created_at"
                ") VALUES(?,?,?,?,?,?)",
                (topup_id, encrypted_data, card_type, denomination, "ppay", int(time.time())),
            )
            return topup_id, code


def ppay_fields(
    app: Flask, card_type: str, pin: str, serial: str, denomination: int, topup_id: int
) -> dict[str, str | int]:
    partner_key = app.config["PPAY_PARTNER_KEY"]
    signature = hashlib.md5(f"{partner_key}{pin}{serial}".encode()).hexdigest()
    return {
        "telco": card_type.upper(),
        "code": pin,
        "serial": serial,
        "amount": denomination,
        "request_id": topup_id,
        "partner_id": app.config["PPAY_PARTNER_ID"],
        "sign": signature,
        "command": "charging",
    }


def parse_ppay_result(result) -> tuple[str | None, int | None]:
    if isinstance(result, dict):
        status = result.get("status")
        raw_amount = result.get("amount")
    else:
        status, raw_amount = result, None
    status_text = str(status).strip() if status is not None else None
    amount = None
    if isinstance(raw_amount, int):
        amount = raw_amount
    elif isinstance(raw_amount, str):
        digits = re.sub(r"\D", "", raw_amount)
        amount = int(digits) if digits else None
    return status_text, amount


def store_card_verification(
    path: str, topup_id: int, received: int | None, denomination: int
) -> None:
    with sqlite_connection(path) as conn:
        with transaction(conn):
            if received is not None and 0 < received <= denomination:
                conn.execute(
                    "UPDATE topups SET status='verified',amount=? "
                    "WHERE id=? AND method='card' AND status IN ('submitting','processing')",
                    (received, topup_id),
                )
            else:
                conn.execute(
                    "UPDATE topups SET status='awaiting_amount',amount=0 "
                    "WHERE id=? AND method='card' AND status IN ('submitting','processing')",
                    (topup_id,),
                )
            conn.execute(
                "UPDATE card_topup_details SET encrypted_data='' WHERE topup_id=?",
                (topup_id,),
            )


def set_topup_status(path: str, topup_id: int, status: str) -> None:
    with sqlite_connection(path) as conn:
        conn.execute(
            "UPDATE topups SET status=? WHERE id=? AND status='submitting'",
            (status, topup_id),
        )
        conn.commit()


def reject_card(path: str, topup_id: int) -> None:
    with sqlite_connection(path) as conn:
        with transaction(conn):
            conn.execute(
                "UPDATE topups SET status='rejected' WHERE id=? "
                "AND method='card' AND status IN ('submitting','processing')",
                (topup_id,),
            )
            conn.execute(
                "UPDATE card_topup_details SET encrypted_data='' WHERE topup_id=?",
                (topup_id,),
            )


def valid_txid(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{64}", value.strip()))


def alert_admins(app: Flask, text: str, topup_id: int | None = None) -> bool:
    keyboard = None
    if topup_id is not None:
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "Đã nhận tiền", "callback_data": f"tc:{topup_id}"},
                    {"text": "Từ chối", "callback_data": f"tx:{topup_id}"},
                ]
            ]
        }
    all_sent = True
    for admin_id in app.config["ADMIN_IDS"]:
        payload = {"chat_id": admin_id, "text": text}
        if keyboard:
            payload["reply_markup"] = keyboard
        try:
            response = httpx.post(
                f"https://api.telegram.org/bot{app.config['BOT_TOKEN']}/sendMessage",
                json=payload,
                timeout=15,
            )
            response.raise_for_status()
            if not response.json().get("ok"):
                raise ShopError("Telegram Bot API từ chối gửi thông báo admin.")
        except (httpx.HTTPError, ValueError, ShopError):
            log.exception("Không gửi được thông báo cho admin Telegram %s", admin_id)
            all_sent = False
    return all_sent


def approve_or_reject_topup(path: str, topup_id: int, approve: bool) -> None:
    with sqlite_connection(path) as conn:
        with transaction(conn):
            row = conn.execute(
                "SELECT * FROM topups WHERE id=?", (topup_id,)
            ).fetchone()
            if row is None:
                raise ShopError("Không tìm thấy yêu cầu nạp.")
            if approve:
                allowed = ("verified",) if row["method"] == "card" else ("notified",)
                if row["status"] not in allowed or int(row["amount"]) <= 0:
                    raise ShopError("Yêu cầu chưa đủ điều kiện duyệt.")
                changed = conn.execute(
                    "UPDATE topups SET status='done' WHERE id=? AND status=?",
                    (topup_id, row["status"]),
                ).rowcount
                if changed != 1:
                    raise ShopError("Yêu cầu đã được xử lý ở nơi khác.")
                conn.execute("INSERT OR IGNORE INTO users(id) VALUES(?)", (row["user_id"],))
                conn.execute(
                    "UPDATE users SET balance=balance+? WHERE id=?",
                    (row["amount"], row["user_id"]),
                )
            else:
                allowed = (
                    ("submitting", "processing", "awaiting_amount", "verified")
                    if row["method"] == "card"
                    else ("pending", "notified")
                )
                placeholders = ",".join("?" for _ in allowed)
                changed = conn.execute(
                    f"UPDATE topups SET status='rejected' WHERE id=? "
                    f"AND status IN ({placeholders})",
                    (topup_id, *allowed),
                ).rowcount
                if changed != 1:
                    raise ShopError("Yêu cầu đã được xử lý ở nơi khác.")
                conn.execute(
                    "UPDATE card_topup_details SET encrypted_data='' WHERE topup_id=?",
                    (topup_id,),
                )


def notify_topup_user(app: Flask, topup_id: int, approved: bool | None) -> None:
    with sqlite_connection(app.config["DB_PATH"]) as conn:
        row = conn.execute(
            "SELECT t.user_id,t.amount,t.status,a.telegram_id FROM topups t "
            "LEFT JOIN web_accounts a ON a.wallet_user_id=t.user_id WHERE t.id=?",
            (topup_id,),
        ).fetchone()
        if row is None:
            return
        if row["telegram_id"] is None:
            return
        if approved is True:
            text = f"✅ Đã duyệt nạp tiền #{topup_id}: {money(row['amount'])}."
        elif approved is False:
            text = f"❌ Yêu cầu nạp tiền #{topup_id} bị từ chối."
        else:
            text = f"✅ Đã lưu số tiền thực nhận cho thẻ #{topup_id}; đang chờ admin duyệt."
    try:
        response = httpx.post(
            f"https://api.telegram.org/bot{app.config['BOT_TOKEN']}/sendMessage",
            json={"chat_id": row["telegram_id"], "text": text},
            timeout=15,
        )
        response.raise_for_status()
        if not response.json().get("ok"):
            raise ShopError("Telegram Bot API không gửi được thông báo.")
    except (httpx.HTTPError, ValueError, ShopError):
        log.exception("Không gửi được thông báo nạp tiền tới Telegram user %s", row["telegram_id"])


def refund_order(path: str, order_id: int, terminal_status: str) -> None:
    with sqlite_connection(path) as conn:
        with transaction(conn):
            row = conn.execute(
                "SELECT user_id,total,discount_percent FROM orders WHERE id=?",
                (order_id,),
            ).fetchone()
            if row is None:
                return
            changed = conn.execute(
                "UPDATE orders SET status=? WHERE id=? AND status='creating'",
                (terminal_status, order_id),
            ).rowcount
            if changed != 1:
                return
            conn.execute(
                "UPDATE users SET balance=balance+?,"
                "next_discount_percent=CASE WHEN next_discount_percent=0 "
                "THEN ? ELSE next_discount_percent END WHERE id=?",
                (row["total"], row["discount_percent"], row["user_id"]),
            )


def mark_order_review(path: str, order_id: int) -> None:
    with sqlite_connection(path) as conn:
        conn.execute(
            "UPDATE orders SET status='review' WHERE id=? AND status='creating'",
            (order_id,),
        )
        conn.commit()


def start_order_worker(path: str, app_config: dict) -> None:
    resolved_path = str(Path(path).resolve())
    with _worker_lock:
        if resolved_path in _worker_started_paths:
            return
        _worker_started_paths.add(resolved_path)

    def run():
        while True:
            try:
                track_web_orders(path, app_config)
            except Exception:
                log.exception("Lỗi theo dõi đơn web")
            time.sleep(5)

    thread = threading.Thread(target=run, name="zenlig-order-tracker", daemon=True)
    thread.start()


def track_web_orders(path: str, config: dict) -> None:
    with sqlite_connection(path) as conn:
        rows = conn.execute(
            "SELECT id,qcst_order_id FROM orders "
            "WHERE source='web' AND status='processing' "
            "AND qcst_order_id IS NOT NULL AND qcst_order_id<>'' LIMIT 20"
        ).fetchall()
    for row in rows:
        try:
            response = httpx.get(
                config["QCST_BASE_URL"].rstrip("/")
                + f"/v1/orders/{quote(str(row['qcst_order_id']), safe='')}",
                headers={"X-API-Key": config["QCST_API_KEY"]},
                timeout=20,
            )
            response.raise_for_status()
            data = response.json().get("data", {})
        except (httpx.HTTPError, ValueError):
            log.exception("Không thể theo dõi QCST order cho đơn web #%s", row["id"])
            continue
        if data.get("delivery_available") and data.get("delivery") is not None:
            complete_order(path, int(row["id"]), data["delivery"])
            continue
        status = str(data.get("status") or "").lower()
        payment = str(data.get("payment_status") or "").lower()
        if status in FAIL_STATUSES or payment == "refunded":
            refund_processing_order(path, int(row["id"]))


def complete_order(path: str, order_id: int, delivery) -> None:
    encoded = json.dumps(delivery, ensure_ascii=False)
    with sqlite_connection(path) as conn:
        with transaction(conn):
            conn.execute(
                "UPDATE orders SET status='done',delivery=? "
                "WHERE id=? AND status='processing'",
                (encoded, order_id),
            )
        settle_referral_reward(path, order_id, success=True)


def refund_processing_order(path: str, order_id: int) -> None:
    with sqlite_connection(path) as conn:
        with transaction(conn):
            row = conn.execute(
                "SELECT user_id,total,discount_percent FROM orders WHERE id=?",
                (order_id,),
            ).fetchone()
            if row is None:
                return
            changed = conn.execute(
                "UPDATE orders SET status='refunded' WHERE id=? AND status='processing'",
                (order_id,),
            ).rowcount
            if changed != 1:
                return
            conn.execute(
                "UPDATE users SET balance=balance+?,"
                "next_discount_percent=CASE WHEN next_discount_percent=0 "
                "THEN ? ELSE next_discount_percent END WHERE id=?",
                (row["total"], row["discount_percent"], row["user_id"]),
            )
    settle_referral_reward(path, order_id, success=False)


def stage_referral_reward(
    path: str, order_id: int, total: int, usdt_vnd_rate: Decimal = Decimal("22000")
) -> None:
    with sqlite_connection(path) as conn:
        order = conn.execute(
            "SELECT user_id FROM orders WHERE id=? AND status='processing'",
            (order_id,),
        ).fetchone()
        if not order:
            return
        referred_id = order["user_id"]
        with transaction(conn):
            user = conn.execute(
                "SELECT referrer_id FROM users WHERE id=?", (referred_id,)
            ).fetchone()
            if not user or user["referrer_id"] is None:
                return
            referrer_id = int(user["referrer_id"])
            commission = int(
                (Decimal(total) * Decimal(2) / Decimal(100)).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                )
            )
            first_bonus = int(
                (Decimal(usdt_vnd_rate) * Decimal("0.10")).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                )
            )
            prior_bonus = conn.execute(
                "SELECT 1 FROM referral_rewards WHERE referred_id=? "
                "AND referred_bonus>0 AND status IN ('pending','approved') LIMIT 1",
                (referred_id,),
            ).fetchone()
            bonus = 0 if prior_bonus else first_bonus
            conn.execute(
                "INSERT OR IGNORE INTO referral_rewards("
                "order_id,referrer_id,referred_id,commission_amount,"
                "referrer_bonus,referred_bonus,status,created_at"
                ") VALUES(?,?,?,?,?,?,'pending',?)",
                (
                    order_id,
                    referrer_id,
                    referred_id,
                    commission,
                    bonus,
                    bonus,
                    int(time.time()),
                ),
            )


def settle_referral_reward(path: str, order_id: int, success: bool) -> None:
    with sqlite_connection(path) as conn:
        with transaction(conn):
            reward = conn.execute(
                "SELECT * FROM referral_rewards WHERE order_id=? AND status='pending'",
                (order_id,),
            ).fetchone()
            if not reward:
                return
            new_status = "approved" if success else "rejected"
            changed = conn.execute(
                "UPDATE referral_rewards SET status=?,approved_at=? "
                "WHERE order_id=? AND status='pending'",
                (new_status, int(time.time()) if success else None, order_id),
            ).rowcount
            if success and changed:
                conn.execute(
                    "INSERT OR IGNORE INTO users(id) VALUES(?)",
                    (reward["referrer_id"],),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO users(id) VALUES(?)",
                    (reward["referred_id"],),
                )
                conn.execute(
                    "UPDATE users SET balance=balance+? WHERE id=?",
                    (
                        reward["commission_amount"] + reward["referrer_bonus"],
                        reward["referrer_id"],
                    ),
                )
                if reward["referred_bonus"]:
                    conn.execute(
                        "UPDATE users SET balance=balance+? WHERE id=?",
                        (reward["referred_bonus"], reward["referred_id"]),
                    )
            elif changed and (reward["referrer_bonus"] or reward["referred_bonus"]):
                next_reward = conn.execute(
                    "SELECT order_id,referrer_id FROM referral_rewards "
                    "WHERE referred_id=? AND status='approved' "
                    "AND referred_bonus=0 ORDER BY order_id LIMIT 1",
                    (reward["referred_id"],),
                ).fetchone()
                if next_reward:
                    conn.execute(
                        "UPDATE referral_rewards SET referrer_bonus=?,referred_bonus=? "
                        "WHERE order_id=?",
                        (
                            reward["referrer_bonus"],
                            reward["referred_bonus"],
                            next_reward["order_id"],
                        ),
                    )
                    conn.execute(
                        "UPDATE users SET balance=balance+? WHERE id=?",
                        (reward["referrer_bonus"], next_reward["referrer_id"]),
                    )
                    conn.execute(
                        "UPDATE users SET balance=balance+? WHERE id=?",
                        (reward["referred_bonus"], reward["referred_id"]),
                    )
                else:
                    next_reward = conn.execute(
                        "SELECT order_id FROM referral_rewards "
                        "WHERE referred_id=? AND status='pending' "
                        "AND referred_bonus=0 ORDER BY order_id LIMIT 1",
                        (reward["referred_id"],),
                    ).fetchone()
                    if next_reward:
                        conn.execute(
                            "UPDATE referral_rewards SET referrer_bonus=?,referred_bonus=? "
                            "WHERE order_id=?",
                            (
                                reward["referrer_bonus"],
                                reward["referred_bonus"],
                                next_reward["order_id"],
                            ),
                        )


def topup_method_label(method: str) -> str:
    return {
        "bank": "Chuyển khoản ngân hàng",
        "usdt": "USDT / TRC20",
        "card": "Thẻ cào / PPay",
    }.get(method, method)


# Create app instance for WSGI servers
app = create_app()

if __name__ == "__main__":
    app = create_app()
    app.run(host="127.0.0.1", port=8080, threaded=True)
