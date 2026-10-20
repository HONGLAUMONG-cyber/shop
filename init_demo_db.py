#!/usr/bin/env python3
"""Create minimal bot database schema for demo"""
import sqlite3
from pathlib import Path

db_path = Path(__file__).parent / 'demo_shop.db'
print(f"Creating demo database at: {db_path}")

conn = sqlite3.connect(str(db_path))

# Create core bot tables (minimal schema from bot)
conn.executescript("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    balance INTEGER NOT NULL DEFAULT 0,
    first_name TEXT,
    username TEXT,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    product_id TEXT NOT NULL,
    product_name TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    total INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'processing',
    created_at INTEGER NOT NULL,
    completed_at INTEGER
);

CREATE TABLE IF NOT EXISTS topups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    method TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at INTEGER NOT NULL,
    txid TEXT
);

CREATE TABLE IF NOT EXISTS card_topup_details (
    topup_id INTEGER PRIMARY KEY,
    telco TEXT NOT NULL,
    serial_encrypted BLOB NOT NULL,
    pin_encrypted BLOB NOT NULL,
    card_value INTEGER NOT NULL,
    FOREIGN KEY(topup_id) REFERENCES topups(id)
);
""")

conn.commit()
conn.close()

print("✓ Base schema created")

# Now run init_schema to add web-specific tables
from app import init_schema
init_schema(str(db_path))

print("✓ Demo database fully initialized")
