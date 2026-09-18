"""Streamlit Secrets içindeki Neon bağlantısını gizli değeri yazdırmadan sınar."""
import os
import tomllib
from pathlib import Path

import psycopg


secret_file = Path(__file__).parent / ".streamlit" / "secrets.toml"
secrets = tomllib.loads(secret_file.read_text(encoding="utf-8")) if secret_file.exists() else {}
database_url = os.environ.get("DATABASE_URL", "").strip() or str(secrets.get("DATABASE_URL", "")).strip()
if not database_url:
    raise SystemExit("DATABASE_URL henüz girilmedi.")

with psycopg.connect(database_url, connect_timeout=10) as connection:
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_database(), current_user")
        database_name, database_user = cursor.fetchone()
        cursor.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'")
        table_count = cursor.fetchone()[0]

print(f"Neon bağlantısı başarılı · Veritabanı: {database_name} · Kullanıcı: {database_user} · Tablo: {table_count}")
