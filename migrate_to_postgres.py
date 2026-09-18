r"""TREX MES SQLite verisini PostgreSQL/Neon'a tek seferlik taşır.

Kullanım (PowerShell):
  $env:DATABASE_URL='Neon panelindeki bağlantı adresi'
  .\venv\Scripts\python.exe .\migrate_to_postgres.py

Bu betik kaynak mes.db dosyasını değiştirmez.
"""

import os
import re
import sqlite3
import sys

import psycopg


SQLITE_DB = "mes.db"


def postgres_schema(sql):
    """SQLite CREATE TABLE tanımını PostgreSQL uyumlu temel biçime çevirir."""
    sql = re.sub(r"\bINTEGER\s+PRIMARY\s+KEY\b", "BIGSERIAL PRIMARY KEY", sql, flags=re.I)
    sql = re.sub(r"\bAUTOINCREMENT\b", "", sql, flags=re.I)
    return sql


def main():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        sys.exit("DATABASE_URL tanımlı değil. Neon bağlantı adresini ortam değişkeni olarak girin.")
    if not os.path.exists(SQLITE_DB):
        sys.exit(f"Kaynak veritabanı bulunamadı: {SQLITE_DB}")

    sqlite = sqlite3.connect(SQLITE_DB)
    sqlite.row_factory = sqlite3.Row
    tables = sqlite.execute("""
        SELECT name, sql FROM sqlite_master
        WHERE type='table' AND name NOT LIKE 'sqlite_%' AND sql IS NOT NULL
        ORDER BY name
    """).fetchall()

    with psycopg.connect(database_url) as postgres:
        with postgres.cursor() as cursor:
            for table in tables:
                table_name, create_sql = table["name"], postgres_schema(table["sql"])
                cursor.execute(f'DROP TABLE IF EXISTS "{table_name}" CASCADE')
                cursor.execute(create_sql)

                rows = sqlite.execute(f'SELECT * FROM "{table_name}"').fetchall()
                if rows:
                    columns = rows[0].keys()
                    quoted_columns = ", ".join(f'"{column}"' for column in columns)
                    placeholders = ", ".join(["%s"] * len(columns))
                    cursor.executemany(
                        f'INSERT INTO "{table_name}" ({quoted_columns}) VALUES ({placeholders})',
                        [tuple(row[column] for column in columns) for row in rows],
                    )
                print(f"{table_name}: {len(rows)} kayıt taşındı")

                id_column = sqlite.execute(f'PRAGMA table_info("{table_name}")').fetchall()
                if any(column["name"] == "id" for column in id_column):
                    cursor.execute(
                        f"SELECT setval(pg_get_serial_sequence(%s, 'id'), "
                        f"COALESCE((SELECT MAX(id) FROM \"{table_name}\"), 1), true)",
                        (table_name,),
                    )
        postgres.commit()

    sqlite.close()
    print("Taşıma tamamlandı. app.py bağlantısını PostgreSQL'e geçirmeden önce Neon tablosunu doğrulayın.")


if __name__ == "__main__":
    main()
