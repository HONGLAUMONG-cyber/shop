from __future__ import annotations

from cryptography.fernet import Fernet
from flask import g
import hashlib
import hmac
import re
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import app as shop


class ShopTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "shop.db")
        self.create_bot_database(self.db_path)
        self.fernet_key = Fernet.generate_key().decode()
        self.app = shop.create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-secret-key-long-enough-for-flask-sessions",
                "DB_PATH": self.db_path,
                "BOT_TOKEN": "123456:test-token",
                "QCST_API_KEY": "test-qcst-key",
                "TELEGRAM_BOT_USERNAME": "zenlig_test_bot",
                "ADMIN_IDS": "12345",
                "BANK_CODE": "TEST",
                "BANK_ACCOUNT": "123456789",
                "ACCOUNT_NAME": "Test Account",
                "USDT_TRC20_ADDRESS": "TTestWallet",
                "PPAY_PARTNER_ID": "partner",
                "PPAY_PARTNER_KEY": "partner-key",
                "DOITHE_CARD_ENCRYPTION_KEY": self.fernet_key,
                "ENABLE_ORDER_WORKER": False,
                "SESSION_COOKIE_SECURE": False,
            }
        )
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def create_bot_database(path: str) -> None:
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE users(
                id INTEGER PRIMARY KEY,
                balance INTEGER NOT NULL DEFAULT 0,
                language TEXT NOT NULL DEFAULT 'vi',
                next_discount_percent INTEGER NOT NULL DEFAULT 0,
                referrer_id INTEGER
            );
            CREATE TABLE orders(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                product_id TEXT NOT NULL,
                product_name TEXT NOT NULL,
                qty INTEGER NOT NULL,
                unit_price INTEGER NOT NULL,
                total INTEGER NOT NULL,
                client_order_id TEXT,
                qcst_order_id TEXT,
                status TEXT NOT NULL,
                discount_percent INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE topups(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                code TEXT,
                status TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                method TEXT NOT NULL DEFAULT 'bank',
                usdt_amount TEXT,
                txid TEXT
            );
            CREATE TABLE card_topup_details(
                topup_id INTEGER PRIMARY KEY,
                encrypted_data TEXT NOT NULL,
                card_type TEXT NOT NULL DEFAULT '',
                denomination INTEGER NOT NULL,
                provider TEXT NOT NULL DEFAULT 'doithe',
                created_at INTEGER NOT NULL
            );
            CREATE TABLE coupon_requests(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                code TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at INTEGER NOT NULL
            );
            CREATE TABLE notification_settings(
                user_id INTEGER PRIMARY KEY,
                restock INTEGER NOT NULL DEFAULT 1,
                new_product INTEGER NOT NULL DEFAULT 1,
                buyer INTEGER NOT NULL DEFAULT 1,
                review INTEGER NOT NULL DEFAULT 0,
                giveaway INTEGER NOT NULL DEFAULT 1,
                coupon INTEGER NOT NULL DEFAULT 1,
                flash_sale INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE referral_rewards(
                order_id INTEGER PRIMARY KEY,
                referrer_id INTEGER NOT NULL,
                referred_id INTEGER NOT NULL,
                commission_amount INTEGER NOT NULL DEFAULT 0,
                referrer_bonus INTEGER NOT NULL DEFAULT 0,
                referred_bonus INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                approved_at INTEGER
            );
            """
        )
        conn.commit()
        conn.close()

    def csrf(self, path: str) -> str:
        response = self.client.get(path)
        match = re.search(
            rb'name="csrf_token" value="([^"]+)"', response.data
        )
        self.assertIsNotNone(match)
        return match.group(1).decode()

    def register(self, email="buyer@example.com"):
        return self.client.post(
            "/register",
            data={
                "csrf_token": self.csrf("/register"),
                "email": email,
                "password": "correct-horse-battery",
            },
            follow_redirects=True,
        )

    def test_registration_creates_separate_wallet_and_session(self):
        response = self.register()
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"buyer@example.com", response.data)
        with shop.sqlite_connection(self.db_path) as conn:
            account = conn.execute(
                "SELECT * FROM web_accounts WHERE email='buyer@example.com'"
            ).fetchone()
            user = conn.execute(
                "SELECT balance FROM users WHERE id=?", (account["wallet_user_id"],)
            ).fetchone()
        self.assertLess(account["wallet_user_id"], 0)
        self.assertEqual(user["balance"], 0)

    def test_post_without_csrf_is_rejected(self):
        response = self.client.post(
            "/register",
            data={"email": "buyer@example.com", "password": "correct-horse-battery"},
        )
        self.assertEqual(response.status_code, 400)

    def test_admin_topup_approval_credits_once(self):
        with shop.sqlite_connection(self.db_path) as conn:
            conn.execute("INSERT INTO users(id,balance) VALUES(12345,1000)")
            conn.execute(
                "INSERT INTO web_accounts(email,telegram_id,wallet_user_id,created_at) "
                "VALUES('admin@example.com',12345,12345,?)",
                (int(time.time()),),
            )
            cur = conn.execute(
                "INSERT INTO topups(user_id,amount,code,status,created_at,method) "
                "VALUES(12345,50000,'TOPUPTEST','notified',?,'bank')",
                (int(time.time()),),
            )
            topup_id = cur.lastrowid
            conn.commit()

        shop.approve_or_reject_topup(self.db_path, topup_id, approve=True)
        with shop.sqlite_connection(self.db_path) as conn:
            balance = conn.execute(
                "SELECT balance FROM users WHERE id=12345"
            ).fetchone()["balance"]
        self.assertEqual(balance, 51000)
        with self.assertRaises(shop.ShopError):
            shop.approve_or_reject_topup(self.db_path, topup_id, approve=True)
        with shop.sqlite_connection(self.db_path) as conn:
            self.assertEqual(
                conn.execute("SELECT balance FROM users WHERE id=12345").fetchone()[
                    "balance"
                ],
                51000,
            )

    def test_web_delivery_is_persisted_and_refund_is_atomic(self):
        with shop.sqlite_connection(self.db_path) as conn:
            conn.execute("INSERT INTO users(id,balance) VALUES(-1,100)")
            conn.execute(
                "INSERT INTO orders(user_id,product_id,product_name,qty,unit_price,total,"
                "status,discount_percent,created_at,source) "
                "VALUES(-1,'p1','Test',1,20,20,'processing',0,?,'web')",
                (int(time.time()),),
            )
            delivered_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO orders(user_id,product_id,product_name,qty,unit_price,total,"
                "status,discount_percent,created_at,source) "
                "VALUES(-1,'p2','Failed',1,30,30,'processing',10,?,'web')",
                (int(time.time()),),
            )
            refund_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.commit()

        shop.complete_order(self.db_path, delivered_id, {"account": "delivered"})
        shop.refund_processing_order(self.db_path, refund_id)
        with shop.sqlite_connection(self.db_path) as conn:
            orders = conn.execute(
                "SELECT status,delivery FROM orders ORDER BY id"
            ).fetchall()
            user = conn.execute("SELECT * FROM users WHERE id=-1").fetchone()
        self.assertEqual(orders[0]["status"], "done")
        self.assertEqual(orders[0]["delivery"], '{"account": "delivered"}')
        self.assertEqual(orders[1]["status"], "refunded")
        self.assertEqual(user["balance"], 130)
        self.assertEqual(user["next_discount_percent"], 10)

    def test_link_merges_wallet_and_transaction_history(self):
        self.register()
        with shop.sqlite_connection(self.db_path) as conn:
            account_id = conn.execute(
                "SELECT id FROM web_accounts WHERE email='buyer@example.com'"
            ).fetchone()["id"]
            wallet_id = -account_id
            conn.execute("UPDATE users SET balance=25000 WHERE id=?", (wallet_id,))
            conn.execute("INSERT INTO users(id,balance) VALUES(7654321,3000)")
            conn.execute(
                "INSERT INTO orders(user_id,product_id,product_name,qty,unit_price,total,"
                "status,created_at) VALUES(?, 'p1','Test',1,10,10,'done',?)",
                (wallet_id, int(time.time())),
            )
            conn.commit()

        with self.app.test_request_context("/"):
            shop.link_telegram_account(self.app, account_id, 7654321)
        with shop.sqlite_connection(self.db_path) as conn:
            account = conn.execute(
                "SELECT * FROM web_accounts WHERE id=?", (account_id,)
            ).fetchone()
            user = conn.execute(
                "SELECT balance FROM users WHERE id=7654321"
            ).fetchone()
            order = conn.execute("SELECT user_id FROM orders").fetchone()
            old_user = conn.execute(
                "SELECT 1 FROM users WHERE id=?", (wallet_id,)
            ).fetchone()
        self.assertEqual(account["telegram_id"], 7654321)
        self.assertEqual(account["wallet_user_id"], 7654321)
        self.assertEqual(user["balance"], 28000)
        self.assertEqual(order["user_id"], 7654321)
        self.assertIsNone(old_user)

    def test_telegram_hmac_authentication(self):
        bot_token = "123456:test-token"
        values = {"id": "7654321", "auth_date": str(int(time.time()))}
        data = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
        signature = hmac.new(
            hashlib.sha256(bot_token.encode()).digest(),
            data.encode(),
            hashlib.sha256,
        ).hexdigest()
        values["hash"] = signature
        self.assertTrue(shop.validate_telegram_auth(bot_token, values))
        values["id"] = "7654322"
        self.assertFalse(shop.validate_telegram_auth(bot_token, values))

    def test_card_credentials_are_encrypted_at_rest(self):
        encryptor = Fernet(self.fernet_key.encode())
        topup_id, _ = shop.create_card_topup(
            self.db_path,
            -1,
            "viettel",
            50000,
            "123456789012",
            "987654321098",
            encryptor,
        )
        with shop.sqlite_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT encrypted_data FROM card_topup_details WHERE topup_id=?",
                (topup_id,),
            ).fetchone()
        self.assertNotIn("123456789012", row["encrypted_data"])
        decrypted = encryptor.decrypt(row["encrypted_data"].encode()).decode()
        self.assertIn("123456789012", decrypted)

    def test_ppay_optional_mode(self):
        app_no_ppay = shop.create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-secret-key-long-enough-for-flask-sessions",
                "DB_PATH": self.db_path,
                "BOT_TOKEN": "123456:test-token",
                "QCST_API_KEY": "test-qcst-key",
                "TELEGRAM_BOT_USERNAME": "zenlig_test_bot",
                "ADMIN_IDS": "12345",
                "BANK_CODE": "TEST",
                "BANK_ACCOUNT": "123456789",
                "ACCOUNT_NAME": "Test Account",
                "USDT_TRC20_ADDRESS": "TTestWallet",
                "PPAY_PARTNER_ID": "",
                "PPAY_PARTNER_KEY": "",
                "DOITHE_CARD_ENCRYPTION_KEY": "",
                "ENABLE_ORDER_WORKER": False,
                "SESSION_COOKIE_SECURE": False,
            }
        )
        self.assertIsNone(app_no_ppay.extensions.get("card_encryptor"))
        client = app_no_ppay.test_client()
        csrf = client.get("/register").data.split(b'name="csrf_token" value="')[1].split(b'"')[0].decode()
        response = client.post(
            "/register",
            data={"csrf_token": csrf, "email": "ppaytest@example.com", "password": "testpass123"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        response = client.get("/topups")
        self.assertEqual(response.status_code, 200)

    def test_language_switch(self):
        response = self.register()
        self.assertEqual(response.status_code, 200)
        with shop.sqlite_connection(self.db_path) as conn:
            account = conn.execute(
                "SELECT * FROM web_accounts WHERE email='buyer@example.com'"
            ).fetchone()
            lang_before = conn.execute(
                "SELECT language FROM users WHERE id=?", (account["wallet_user_id"],)
            ).fetchone()["language"]
        self.assertEqual(lang_before, "vi")
        response = self.client.post(
            "/account/language",
            data={
                "csrf_token": self.csrf("/account"),
                "language": "en",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        with shop.sqlite_connection(self.db_path) as conn:
            lang_after = conn.execute(
                "SELECT language FROM users WHERE id=?", (account["wallet_user_id"],)
            ).fetchone()["language"]
        self.assertEqual(lang_after, "en")

    def test_product_category_logic(self):
        gemini_product = {
            "id": "test1",
            "name": "Gemini Advanced API Key",
            "metadata": {"category": "Gemini"},
        }
        self.assertEqual(shop.product_category(gemini_product), "gemini")
        gpt_product = {
            "id": "test2",
            "name": "ChatGPT Plus Account",
            "category": "AI",
        }
        self.assertEqual(shop.product_category(gpt_product), "chatgpt")
        other_product = {
            "id": "test3",
            "name": "Unknown Service Account",
        }
        self.assertEqual(shop.product_category(other_product), "other")

    def make_account(self, wallet_id, email=None, telegram_id=None):
        with shop.sqlite_connection(self.db_path) as conn:
            conn.execute("INSERT OR IGNORE INTO users(id,balance) VALUES(?,0)", (wallet_id,))
            cur = conn.execute(
                "INSERT INTO web_accounts(email,telegram_id,wallet_user_id,created_at) "
                "VALUES(?,?,?,?)",
                (email, telegram_id, wallet_id, int(time.time())),
            )
            conn.commit()
            return cur.lastrowid

    def login_as(self, account_id):
        with self.client.session_transaction() as sess:
            sess["account_id"] = account_id
            sess["csrf_token"] = "test-csrf-token"

    def test_catalog_search_and_category_filter(self):
        products = [
            {
                "id": "p1",
                "name": "Gemini Advanced",
                "description": "AI key",
                "price": 10000,
                "status": "available",
                "metadata": {"category": "Gemini"},
            },
            {
                "id": "p2",
                "name": "Canva Pro",
                "description": "design tool",
                "price": 20000,
                "status": "available",
                "metadata": {"category": "Canva"},
            },
        ]
        with mock.patch.object(shop, "fetch_products", return_value=products):
            response = self.client.get("/")
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"Gemini Advanced", response.data)
            self.assertIn(b"Canva Pro", response.data)

            response = self.client.get("/?q=canva")
            self.assertIn(b"Canva Pro", response.data)
            self.assertNotIn(b"Gemini Advanced", response.data)

            response = self.client.get("/?category=gemini")
            self.assertIn(b"Gemini Advanced", response.data)
            self.assertNotIn(b"Canva Pro", response.data)
            self.assertIn(b'value="canva"', response.data)

    def test_coupon_submission_and_admin_approval_credits_once(self):
        buyer = self.make_account(-5, email="buyer5@example.com")
        admin = self.make_account(12345, email="admin@example.com", telegram_id=12345)
        self.login_as(buyer)
        response = self.client.post(
            "/coupons",
            data={"csrf_token": "test-csrf-token", "code": "zenlig2024"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        with shop.sqlite_connection(self.db_path) as conn:
            coupon = conn.execute(
                "SELECT * FROM coupon_requests WHERE user_id=-5"
            ).fetchone()
        self.assertEqual(coupon["code"], "ZENLIG2024")
        self.assertEqual(coupon["status"], "pending")

        self.login_as(admin)
        response = self.client.post(
            f"/admin/coupons/{coupon['id']}/approve",
            data={
                "csrf_token": "test-csrf-token",
                "action": "credit",
                "amount": "50000",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        with shop.sqlite_connection(self.db_path) as conn:
            balance = conn.execute(
                "SELECT balance FROM users WHERE id=-5"
            ).fetchone()["balance"]
            status = conn.execute(
                "SELECT status FROM coupon_requests WHERE id=?", (coupon["id"],)
            ).fetchone()["status"]
        self.assertEqual(balance, 50000)
        self.assertEqual(status, "approved")

        # Second approval must not credit twice.
        self.client.post(
            f"/admin/coupons/{coupon['id']}/approve",
            data={
                "csrf_token": "test-csrf-token",
                "action": "credit",
                "amount": "50000",
            },
            follow_redirects=True,
        )
        with shop.sqlite_connection(self.db_path) as conn:
            balance = conn.execute(
                "SELECT balance FROM users WHERE id=-5"
            ).fetchone()["balance"]
        self.assertEqual(balance, 50000)

    def test_coupon_discount_sets_next_order_discount(self):
        buyer = self.make_account(-6, email="buyer6@example.com")
        admin = self.make_account(12345, email="admin@example.com", telegram_id=12345)
        with shop.sqlite_connection(self.db_path) as conn:
            cur = conn.execute(
                "INSERT INTO coupon_requests(user_id,code,status,created_at) "
                "VALUES(-6,'DISCOUNT10','pending',?)",
                (int(time.time()),),
            )
            coupon_id = cur.lastrowid
            conn.commit()
        self.login_as(admin)
        self.client.post(
            f"/admin/coupons/{coupon_id}/approve",
            data={
                "csrf_token": "test-csrf-token",
                "action": "discount",
                "discount": "10",
            },
            follow_redirects=True,
        )
        with shop.sqlite_connection(self.db_path) as conn:
            discount = conn.execute(
                "SELECT next_discount_percent FROM users WHERE id=-6"
            ).fetchone()["next_discount_percent"]
        self.assertEqual(discount, 10)

    def test_referral_staging_and_settlement_is_idempotent(self):
        with shop.sqlite_connection(self.db_path) as conn:
            conn.execute("INSERT INTO users(id,balance) VALUES(9001,0)")
            conn.execute("INSERT INTO users(id,balance,referrer_id) VALUES(-7,0,9001)")
            cur = conn.execute(
                "INSERT INTO orders(user_id,product_id,product_name,qty,unit_price,total,"
                "status,discount_percent,created_at,source) "
                "VALUES(-7,'p1','Test',1,100000,100000,'processing',0,?,'web')",
                (int(time.time()),),
            )
            order_id = cur.lastrowid
            conn.commit()

        shop.stage_referral_reward(self.db_path, order_id, 100000)
        shop.stage_referral_reward(self.db_path, order_id, 100000)
        with shop.sqlite_connection(self.db_path) as conn:
            rewards = conn.execute("SELECT * FROM referral_rewards").fetchall()
        self.assertEqual(len(rewards), 1)
        self.assertEqual(rewards[0]["commission_amount"], 2000)
        self.assertEqual(rewards[0]["referrer_bonus"], 2200)
        self.assertEqual(rewards[0]["referred_bonus"], 2200)

        shop.settle_referral_reward(self.db_path, order_id, success=True)
        shop.settle_referral_reward(self.db_path, order_id, success=True)
        with shop.sqlite_connection(self.db_path) as conn:
            referrer_balance = conn.execute(
                "SELECT balance FROM users WHERE id=9001"
            ).fetchone()["balance"]
            referred_balance = conn.execute(
                "SELECT balance FROM users WHERE id=-7"
            ).fetchone()["balance"]
            status = conn.execute(
                "SELECT status FROM referral_rewards WHERE order_id=?", (order_id,)
            ).fetchone()["status"]
        self.assertEqual(referrer_balance, 4200)
        self.assertEqual(referred_balance, 2200)
        self.assertEqual(status, "approved")

    def test_completed_order_settles_referral(self):
        with shop.sqlite_connection(self.db_path) as conn:
            conn.execute("INSERT INTO users(id,balance) VALUES(9002,0)")
            conn.execute("INSERT INTO users(id,balance,referrer_id) VALUES(-8,0,9002)")
            cur = conn.execute(
                "INSERT INTO orders(user_id,product_id,product_name,qty,unit_price,total,"
                "status,discount_percent,created_at,source) "
                "VALUES(-8,'p1','Test',1,50000,50000,'processing',0,?,'web')",
                (int(time.time()),),
            )
            order_id = cur.lastrowid
            conn.commit()
        shop.stage_referral_reward(self.db_path, order_id, 50000)
        shop.complete_order(self.db_path, order_id, {"account": "delivered"})
        with shop.sqlite_connection(self.db_path) as conn:
            status = conn.execute(
                "SELECT status FROM referral_rewards WHERE order_id=?", (order_id,)
            ).fetchone()["status"]
            balance = conn.execute(
                "SELECT balance FROM users WHERE id=9002"
            ).fetchone()["balance"]
        self.assertEqual(status, "approved")
        self.assertEqual(balance, 1000 + 2200)

    def test_refunded_order_rejects_referral_reward(self):
        with shop.sqlite_connection(self.db_path) as conn:
            conn.execute("INSERT INTO users(id,balance) VALUES(9003,0)")
            conn.execute("INSERT INTO users(id,balance,referrer_id) VALUES(-9,0,9003)")
            cur = conn.execute(
                "INSERT INTO orders(user_id,product_id,product_name,qty,unit_price,total,"
                "status,discount_percent,created_at,source) "
                "VALUES(-9,'p1','Test',1,50000,50000,'processing',0,?,'web')",
                (int(time.time()),),
            )
            order_id = cur.lastrowid
            conn.commit()
        shop.stage_referral_reward(self.db_path, order_id, 50000)
        shop.refund_processing_order(self.db_path, order_id)
        with shop.sqlite_connection(self.db_path) as conn:
            status = conn.execute(
                "SELECT status FROM referral_rewards WHERE order_id=?", (order_id,)
            ).fetchone()["status"]
            balance = conn.execute(
                "SELECT balance FROM users WHERE id=9003"
            ).fetchone()["balance"]
        self.assertEqual(status, "rejected")
        self.assertEqual(balance, 0)

    def test_notification_preferences_roundtrip(self):
        account_id = self.make_account(-10, email="notify@example.com")
        self.login_as(account_id)
        response = self.client.get("/notifications")
        self.assertEqual(response.status_code, 200)
        response = self.client.post(
            "/notifications",
            data={
                "csrf_token": "test-csrf-token",
                "restock": "on",
                "buyer": "on",
                "flash_sale": "on",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        with shop.sqlite_connection(self.db_path) as conn:
            settings = conn.execute(
                "SELECT * FROM notification_settings WHERE user_id=-10"
            ).fetchone()
        self.assertEqual(settings["restock"], 1)
        self.assertEqual(settings["buyer"], 1)
        self.assertEqual(settings["flash_sale"], 1)
        self.assertEqual(settings["new_product"], 0)
        self.assertEqual(settings["giveaway"], 0)

    def test_referral_landing_sets_referrer_once(self):
        with shop.sqlite_connection(self.db_path) as conn:
            conn.execute("INSERT INTO users(id,balance) VALUES(777,0)")
            conn.commit()
        account_id = self.make_account(-11, email="referred@example.com")
        self.login_as(account_id)
        response = self.client.get("/ref/777", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        with shop.sqlite_connection(self.db_path) as conn:
            referrer = conn.execute(
                "SELECT referrer_id FROM users WHERE id=-11"
            ).fetchone()["referrer_id"]
        self.assertEqual(referrer, 777)

        # A second, different referrer must not overwrite the first.
        with shop.sqlite_connection(self.db_path) as conn:
            conn.execute("INSERT INTO users(id,balance) VALUES(888,0)")
            conn.commit()
        self.client.get("/ref/888", follow_redirects=True)
        with shop.sqlite_connection(self.db_path) as conn:
            referrer = conn.execute(
                "SELECT referrer_id FROM users WHERE id=-11"
            ).fetchone()["referrer_id"]
        self.assertEqual(referrer, 777)

    def test_referral_landing_blocks_self_referral(self):
        account_id = self.make_account(999, email="self@example.com", telegram_id=999)
        self.login_as(account_id)
        self.client.get("/ref/999", follow_redirects=True)
        with shop.sqlite_connection(self.db_path) as conn:
            referrer = conn.execute(
                "SELECT referrer_id FROM users WHERE id=999"
            ).fetchone()["referrer_id"]
        self.assertIsNone(referrer)

    def test_giveaway_and_support_pages_render(self):
        response = self.client.get("/giveaway")
        self.assertEqual(response.status_code, 200)
        self.assertIn("chưa có giveaway".encode(), response.data.lower())
        response = self.client.get("/support")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"honghnt2747", response.data)

    def test_admin_coupons_requires_admin(self):
        buyer = self.make_account(-12, email="notadmin@example.com")
        self.login_as(buyer)
        response = self.client.get("/admin/coupons")
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
