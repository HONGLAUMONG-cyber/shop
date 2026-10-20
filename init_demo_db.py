#!/usr/bin/env python3
"""Initialize demo database for local testing"""
import sqlite3
from pathlib import Path

# Create the database file
db_path = Path(__file__).parent / 'demo_shop.db'
print(f"Creating demo database at: {db_path}")

conn = sqlite3.connect(str(db_path))
conn.row_factory = sqlite3.Row

# Import and run schema init from app
# We need to bypass create_app validation, so directly call init_schema
from app import init_schema
init_schema(str(db_path))

print("✓ Demo database initialized successfully")
conn.close()
