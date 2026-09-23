import io
import os
import base64
import sqlite3
import time
import hashlib
import hmac
import html
import numbers
import unicodedata
import socket
import uuid
from urllib.parse import quote
from datetime import date, datetime, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None
    dict_row = None

INTEGRITY_ERRORS = (sqlite3.IntegrityError,) + ((psycopg.IntegrityError,) if psycopg else ())

DB = "mes.db"


def runtime_setting(name, default=""):
    """Önce ortam değişkenini, sonra Streamlit Secrets değerini okur."""
    environment_value = os.environ.get(name, "").strip()
    if environment_value:
        return environment_value
    try:
        return str(st.secrets.get(name, default)).strip()
    except Exception:
        return default


DATABASE_URL = runtime_setting("DATABASE_URL")
USING_POSTGRES = bool(DATABASE_URL)
POSTGRES_CONNECTION_CACHE_VERSION = "autocommit-v2"
API_URL = "http://127.0.0.1:8000"
PUBLIC_APP_URL = runtime_setting(
    "PUBLIC_APP_URL",
    "https://mild-testimonials-fighting-prot.trycloudflare.com"
).strip().rstrip("/")
LOGO_PATH = "logo.png"
LOGIN_LOGO_PATH = "login_logo.png"
LOGIN_BACKGROUND_PATH = os.path.join("static", "login-factory-bg.png")
TREX_SIGNATURE_PATH = os.path.join("static", "trex-signature.png")


def image_data_uri(path):
    """Yerel giriş görsellerini tarayıcıya doğrudan ilet."""
    if not os.path.exists(path):
        return ""
    with open(path, "rb") as image_file:
        encoded = base64.b64encode(image_file.read()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


# =========================================================
# DATABASE
# =========================================================

def _postgres_sql(sql):
    """SQLite yer tutucularını PostgreSQL biçimine çevirip LIKE yüzdelerini korur."""
    # psycopg parametreli sorgularda % işaretini kendi biçimlendirme sözdizimi
    # olarak yorumlar. Önce SQL'deki gerçek yüzde işaretlerini kaçırır, sonra
    # yalnızca uygulamanın ? yer tutucularını %s yaparız.
    return "%s".join(part.replace("%", "%%") for part in sql.split("?"))


@st.cache_resource(show_spinner=False)
def cached_postgres_connection(connection_url, cache_version, session_id):
    """Neon TLS bağlantısını ekran yenilemeleri arasında tekrar kullanır."""
    if psycopg is None:
        raise RuntimeError("PostgreSQL için psycopg kurulu değil. requirements.txt dosyasını yükleyin.")
    # Streamlit ekranları çok sayıda kısa okuma sorgusu çalıştırır. Autocommit,
    # bu sorguların açık bir PostgreSQL işleminde kalmasını engeller.
    return psycopg.connect(connection_url, row_factory=dict_row, autocommit=True, connect_timeout=10)


class PostgresConnection:
    """SQLite'a benzer küçük bir arayüzle psycopg bağlantısını sarmalar."""

    def __init__(self):
        if psycopg is None:
            raise RuntimeError("PostgreSQL için psycopg kurulu değil. requirements.txt dosyasını yükleyin.")
        self._session_id = st.session_state.setdefault("db_session_id", uuid.uuid4().hex)
        self._connection = cached_postgres_connection(DATABASE_URL, POSTGRES_CONNECTION_CACHE_VERSION, self._session_id)

    def _refresh_connection(self):
        cached_postgres_connection.clear(DATABASE_URL, POSTGRES_CONNECTION_CACHE_VERSION, self._session_id)
        self._connection = cached_postgres_connection(DATABASE_URL, POSTGRES_CONNECTION_CACHE_VERSION, self._session_id)

    def _active_connection(self):
        """Neon boşta kaldığında veya eski oturum kapandığında yeniden bağlanır."""
        if self._connection.closed:
            self._refresh_connection()
        return self._connection

    def execute(self, sql, params=()):
        statement = _postgres_sql(sql) if params else sql
        try:
            if params:
                return self._active_connection().execute(statement, params)
            return self._active_connection().execute(statement)
        except psycopg.OperationalError:
            # Neon kapalı/eskimiş bir bağlantıyı sonlandırmış olabilir.
            self._refresh_connection()
            if not sql.lstrip().upper().startswith("SELECT"):
                raise
            if params:
                return self._connection.execute(statement, params)
            return self._connection.execute(statement)

    def executemany(self, sql, params_seq):
        with self._active_connection().cursor() as cursor:
            cursor.executemany(_postgres_sql(sql), params_seq)

    def commit(self):
        self._active_connection().commit()
        invalidate_data_caches()

    def close(self):
        # Bu sarmalayıcı kapatılsa da önbellekteki Neon bağlantısı açık kalır.
        # Böylece her sorguda TLS bağlantısı açılmadığı için ekranlar hızlanır.
        pass


def invalidate_data_caches():
    cached_query = globals().get("_cached_read")
    if cached_query is not None:
        cached_query.clear()
    for name in ("make_excel_report", "make_pdf_report", "default_password_accounts"):
        report_builder = globals().get(name)
        if report_builder is not None and hasattr(report_builder, "clear"):
            report_builder.clear()


class LocalConnection(sqlite3.Connection):
    def commit(self):
        super().commit()
        invalidate_data_caches()


def conn():
    if USING_POSTGRES:
        return PostgresConnection()
    c = sqlite3.connect(DB, check_same_thread=False, timeout=10, factory=LocalConnection)
    c.row_factory = sqlite3.Row
    return c


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


SHIFT_SCHEDULE = {
    "Sabah": (8, 16),
    "Akşam": (16, 24),
    "Gece": (0, 8),
}


def active_shift_name(reference_time=None):
    """Yerel saate göre aktif üretim vardiyasını döndürür."""
    hour = (reference_time or datetime.now()).hour
    if 8 <= hour < 16:
        return "Sabah"
    if 16 <= hour < 24:
        return "Akşam"
    return "Gece"


def password_hash(password):
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120000)
    return f"{salt.hex()}${digest.hex()}"


def _read_query(sql, params=()):
    c = conn()
    try:
        if USING_POSTGRES:
            cur = c.execute(sql, params)
            rows = cur.fetchall()
            columns = [column.name for column in cur.description]
            return pd.DataFrame(rows, columns=columns)
        return pd.read_sql_query(sql, c, params=params)
    finally:
        c.close()


@st.cache_data(ttl=10, max_entries=256, show_spinner=False)
def _cached_read(sql, params, database_identity):
    return _read_query(sql, params)


def q(sql, params=()):
    # Kimlik ve yetki kontrolleri her zaman güncel veriyi okur.
    if "users" in sql.lower() or "user_permissions" in sql.lower():
        return _read_query(sql, params)
    identity = hashlib.sha256(DATABASE_URL.encode()).hexdigest() if USING_POSTGRES else os.path.abspath(DB)
    return _cached_read(sql, tuple(params), identity)


def execute(sql, params=()):
    c = conn()
    cur = c.execute(sql, params)
    c.commit()
    last_id = getattr(cur, "lastrowid", None)
    c.close()
    return last_id


def audit_event(action, entity, details=""):
    """Kullanıcı işlemlerinin temel denetim kaydını tutar."""
    execute(
        "INSERT INTO audit_log(username,action,entity,details,timestamp) VALUES(?,?,?,?,?)",
        (st.session_state.get("username", "sistem"), action, entity, details, now())
    )


UNDO_WINDOW_SECONDS = 45


def register_undo(label, operations):
    """Son güvenli durum değişikliğini kısa süreliğine geri alınabilir yapar."""
    st.session_state["pending_undo"] = {
        "label": str(label),
        "operations": [(sql, tuple(params)) for sql, params in operations],
        "created_at": time.time(),
    }


def pending_undo_action():
    action = st.session_state.get("pending_undo")
    if not action:
        return None
    if time.time() - float(action.get("created_at", 0)) > UNDO_WINDOW_SECONDS:
        st.session_state.pop("pending_undo", None)
        return None
    return action


def perform_pending_undo():
    action = pending_undo_action()
    if not action:
        return False
    for sql, params in action["operations"]:
        execute(sql, params)
    audit_event("İşlemi geri aldı", "Kullanıcı işlemi", action["label"])
    st.session_state.pop("pending_undo", None)
    return True


DEFAULT_SENSOR_THRESHOLDS = {
    "temperature_warning": 75.0,
    "temperature_critical": 85.0,
    "vibration_warning": 4.0,
    "vibration_critical": 5.0,
    "pressure_warning": 4.5,
    "pressure_critical": 3.5,
}


@st.cache_resource(show_spinner=False)
def ensure_notification_schema(database_identity, schema_version="notifications-v2-thresholds"):
    """Bildirim ve işletme ayarı tablolarını SQLite/PostgreSQL üzerinde oluşturur."""
    connection = conn()
    connection.execute("""
        CREATE TABLE IF NOT EXISTS notifications(
            id INTEGER PRIMARY KEY,
            recipient_username TEXT,
            recipient_role TEXT,
            notification_type TEXT,
            priority TEXT,
            title TEXT,
            message TEXT,
            machine_code TEXT,
            target_module TEXT,
            entity_type TEXT,
            entity_id INTEGER,
            is_read INTEGER DEFAULT 0,
            is_completed INTEGER DEFAULT 0,
            created_at TEXT,
            read_at TEXT,
            completed_at TEXT
        )
    """)
    connection.execute("CREATE INDEX IF NOT EXISTS idx_notifications_recipient ON notifications(recipient_username,recipient_role,is_read)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_notifications_entity ON notifications(entity_type,entity_id)")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS system_settings(
            setting_key TEXT PRIMARY KEY,
            setting_value REAL NOT NULL,
            updated_at TEXT,
            updated_by TEXT
        )
    """)
    for setting_key, setting_value in DEFAULT_SENSOR_THRESHOLDS.items():
        connection.execute("""
            INSERT INTO system_settings(setting_key,setting_value,updated_at,updated_by)
            VALUES(?,?,?,?) ON CONFLICT(setting_key) DO NOTHING
        """, (setting_key, setting_value, now(), "sistem"))
    connection.commit()
    connection.close()
    return True


@st.cache_resource(show_spinner=False)
def ensure_spc_schema(database_identity, schema_version="spc-v1"):
    """SPC ölçüm tablosunu her iki veritabanında kurar ve ilk demoyu hazırlar."""
    connection = conn()
    connection.execute("""
        CREATE TABLE IF NOT EXISTS spc_measurements(
            id INTEGER PRIMARY KEY,
            machine_id INTEGER,
            machine_code TEXT NOT NULL,
            product TEXT NOT NULL,
            measurement_name TEXT NOT NULL,
            measurement_value REAL NOT NULL,
            unit TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            spec_low REAL,
            spec_high REAL
        )
    """)
    connection.execute("CREATE INDEX IF NOT EXISTS idx_spc_machine_time ON spc_measurements(machine_code,timestamp)")
    existing = connection.execute("SELECT COUNT(*) AS total FROM spc_measurements").fetchone()
    existing_total = int(existing["total"] if hasattr(existing, "keys") else existing[0])
    if existing_total == 0:
        machines = connection.execute("SELECT id,machine_code,product FROM machines ORDER BY machine_code").fetchall()
        for machine_index, machine in enumerate(machines):
            for sample_index in range(24):
                drift = max(sample_index - 17, 0) * .012 if machine_index == 1 else 0
                value = round(25 + random_float(-.035, .035) + drift, 3)
                measured_at = (datetime.now() - timedelta(minutes=(23 - sample_index) * 20)).strftime("%Y-%m-%d %H:%M:%S")
                connection.execute("""
                    INSERT INTO spc_measurements(
                        id,machine_id,machine_code,product,measurement_name,
                        measurement_value,unit,timestamp,spec_low,spec_high
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """, (int(uuid.uuid4().int % 2_000_000_000) or 1, machine["id"], machine["machine_code"],
                      machine["product"] or "Tanımsız Ürün", "Çap", value, "mm", measured_at, 24.90, 25.10))
    connection.commit()
    connection.close()
    return True


@st.cache_resource(show_spinner=False)
def ensure_maturity_schema(database_identity, schema_version="digital-maturity-v1"):
    """Olgunluk değerlendirmesi, kategori ayrıntısı ve aksiyon geçmişini kurar."""
    connection = conn()
    connection.execute("""
        CREATE TABLE IF NOT EXISTS digital_maturity_scores(
            id INTEGER PRIMARY KEY,
            assessment_date TEXT NOT NULL,
            overall_score REAL NOT NULL,
            maturity_level TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS digital_maturity_categories(
            id INTEGER PRIMARY KEY,
            assessment_id INTEGER NOT NULL,
            category_name TEXT NOT NULL,
            score REAL NOT NULL,
            weight REAL NOT NULL,
            details TEXT
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS digital_maturity_actions(
            id INTEGER PRIMARY KEY,
            category_name TEXT NOT NULL,
            action TEXT NOT NULL,
            priority TEXT NOT NULL,
            status TEXT NOT NULL,
            expected_score_gain REAL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)
    connection.execute("CREATE INDEX IF NOT EXISTS idx_maturity_score_date ON digital_maturity_scores(assessment_date)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_maturity_category_assessment ON digital_maturity_categories(assessment_id)")
    connection.commit()
    connection.close()
    return True


@st.cache_resource(show_spinner=False)
def ensure_five_why_schema(database_identity, schema_version="five-why-v1"):
    """5 Why analizlerini ve neden zincirini kalıcı olarak saklar."""
    connection = conn()
    connection.execute("""
        CREATE TABLE IF NOT EXISTS five_why_analyses(
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            event_type TEXT NOT NULL,
            source_type TEXT,
            source_id INTEGER,
            machine_code TEXT,
            event_description TEXT,
            priority TEXT NOT NULL,
            status TEXT NOT NULL,
            owner TEXT,
            due_date TEXT,
            root_cause TEXT,
            containment_action TEXT,
            corrective_action TEXT,
            verification_method TEXT,
            created_by TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS five_why_steps(
            id INTEGER PRIMARY KEY,
            analysis_id INTEGER NOT NULL,
            step_no INTEGER NOT NULL,
            question TEXT,
            answer TEXT NOT NULL
        )
    """)
    connection.execute("CREATE INDEX IF NOT EXISTS idx_five_why_status_due ON five_why_analyses(status,due_date)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_five_why_machine ON five_why_analyses(machine_code)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_five_why_steps_analysis ON five_why_steps(analysis_id,step_no)")
    connection.commit()
    connection.close()
    return True


def get_sensor_thresholds():
    values = DEFAULT_SENSOR_THRESHOLDS.copy()
    try:
        rows = q("SELECT setting_key,setting_value FROM system_settings")
        for _, row in rows.iterrows():
            if row["setting_key"] in values:
                values[row["setting_key"]] = float(row["setting_value"])
    except Exception:
        # İlk kurulum sırasında varsayılanlarla güvenli biçimde devam edilir.
        pass
    return values


def save_sensor_thresholds(values):
    for setting_key in DEFAULT_SENSOR_THRESHOLDS:
        execute("""
            INSERT INTO system_settings(setting_key,setting_value,updated_at,updated_by)
            VALUES(?,?,?,?)
            ON CONFLICT(setting_key) DO UPDATE SET
                setting_value=excluded.setting_value,
                updated_at=excluded.updated_at,
                updated_by=excluded.updated_by
        """, (setting_key, float(values[setting_key]), now(), st.session_state.get("username", "sistem")))


def create_notification(*, recipient_username="", recipient_role="", notification_type="Görev",
                        priority="Bilgi", title, message, machine_code="", target_module="🏠 Ana Sayfa",
                        entity_type="", entity_id=0):
    """Aynı görev ve alıcı için yinelenmeyen kalıcı bildirim oluşturur."""
    recipient_username = str(recipient_username or "").strip()
    recipient_role = str(recipient_role or "").strip()
    existing = q("""
        SELECT id FROM notifications
        WHERE entity_type=? AND entity_id=?
          AND COALESCE(recipient_username,'')=? AND COALESCE(recipient_role,'')=?
        LIMIT 1
    """, (entity_type, int(entity_id or 0), recipient_username, recipient_role))
    if not existing.empty:
        return int(existing.iloc[0]["id"])

    connection = conn()
    # SQLite ve PostgreSQL'de ortak çalışan, eşzamanlı oturumlarda çakışma
    # olasılığı çok düşük bir sayısal kimlik kullanılır.
    next_id = int(uuid.uuid4().int % 2_000_000_000) or 1
    connection.execute("""
        INSERT INTO notifications(
            id,recipient_username,recipient_role,notification_type,priority,title,message,
            machine_code,target_module,entity_type,entity_id,is_read,is_completed,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        next_id, recipient_username, recipient_role, notification_type, priority, title, message,
        machine_code, target_module, entity_type, int(entity_id or 0), 0, 0, now()
    ))
    connection.commit()
    connection.close()
    return next_id


def sync_current_user_notifications():
    """Mevcut görevlerden giriş yapan kullanıcıya uygun bildirimleri üretir."""
    username = st.session_state.get("username", "")
    role = st.session_state.get("role", "")
    full_name = st.session_state.get("full_name", "")
    if not username or not role:
        return

    if role == "operator":
        assigned_orders = q("""
            SELECT w.id,w.order_no,w.machine_code,w.product,w.priority,w.status,m.operator
            FROM work_orders w JOIN machines m ON m.machine_code=w.machine_code
            WHERE w.status NOT IN ('Tamamlandı','İptal')
            ORDER BY w.id DESC LIMIT 12
        """)
        for _, item in assigned_orders.iterrows():
            create_notification(
                recipient_role="operator", notification_type="İş Emri",
                priority="Kritik" if str(item["priority"]) == "Kritik" else "Görev",
                title=f'{item["order_no"]} iş emri atandı',
                message=f'{item["product"]} üretimi · Operatör: {item["operator"]} · Durum: {item["status"]}',
                machine_code=item["machine_code"], target_module="📋 İş Emirleri",
                entity_type="work_order", entity_id=item["id"]
            )

    if role == "maintenance":
        assigned_requests = q("""
            SELECT id,machine_code,request_type,description,priority,status,assigned_to
            FROM maintenance_requests
            WHERE status NOT IN ('Tamamlandı','İptal')
            ORDER BY id DESC LIMIT 12
        """)
        for _, item in assigned_requests.iterrows():
            create_notification(
                recipient_role="maintenance", notification_type="Bakım",
                priority="Kritik" if str(item["priority"]) == "Kritik" else "Görev",
                title=f'{item["machine_code"]} bakım görevi',
                message=f'{item["request_type"]} · {item["description"]} · Atanan: {item["assigned_to"] or "Bakım ekibi"}',
                machine_code=item["machine_code"], target_module="🧰 Bakım Talebi",
                entity_type="maintenance_request", entity_id=item["id"]
            )

        assigned_alarms = q("""
            SELECT a.id,a.machine_code,a.alarm,a.level,x.assignee,x.status
            FROM alarms a JOIN alarm_actions x ON x.alarm_id=a.id
            WHERE a.acknowledged=0 AND x.assignee=? AND x.status NOT IN ('Çözüldü','Onaylandı')
            ORDER BY a.id DESC LIMIT 10
        """, (full_name,))
        for _, item in assigned_alarms.iterrows():
            create_notification(
                recipient_username=username, notification_type="Alarm Ataması",
                priority="Kritik" if str(item["level"]) == "Kritik" else "Uyarı",
                title=f'{item["machine_code"]} alarmı size atandı',
                message=f'{item["alarm"]} · Müdahale bekleniyor',
                machine_code=item["machine_code"], target_module="🚨 Alarmlar",
                entity_type="assigned_alarm", entity_id=item["id"]
            )

    if role in ("maintenance", "admin"):
        critical_alarms = q("""
            SELECT id,machine_code,alarm,level,time FROM alarms
            WHERE acknowledged=0 AND level='Kritik' ORDER BY id DESC LIMIT 10
        """)
        for _, item in critical_alarms.iterrows():
            create_notification(
                recipient_role=role, notification_type="Acil Alarm", priority="Kritik",
                title=f'ACİL · {item["machine_code"]}',
                message=f'{item["alarm"]} · Makineye müdahale gerekiyor',
                machine_code=item["machine_code"], target_module="🚨 Alarmlar",
                entity_type="critical_alarm", entity_id=item["id"]
            )

    if role == "quality":
        quality_tasks = q("""
            SELECT id,machine_code,product,defective,defect_reason,timestamp FROM quality
            WHERE defective>0 ORDER BY id DESC LIMIT 10
        """)
        for _, item in quality_tasks.iterrows():
            create_notification(
                recipient_role="quality", notification_type="Kalite Kontrol", priority="Uyarı",
                title=f'{item["machine_code"]} kalite kontrolü',
                message=f'{item["product"]} · {int(item["defective"])} hatalı · {item["defect_reason"]}',
                machine_code=item["machine_code"], target_module="✅ Kalite",
                entity_type="quality_record", entity_id=item["id"]
            )


def current_user_notifications(limit=12):
    username = st.session_state.get("username", "")
    role = st.session_state.get("role", "")
    return q("""
        SELECT * FROM notifications
        WHERE (recipient_username=? OR recipient_role=?) AND is_completed=0
        ORDER BY CASE priority WHEN 'Kritik' THEN 1 WHEN 'Uyarı' THEN 2 ELSE 3 END,
                 created_at DESC, id DESC LIMIT ?
    """, (username, role, int(limit)))


def init_db():
    # Neon'a taşınmış şema ve veriler zaten hazırdır. Yerel SQLite ilk
    # kurulum ve yükseltme işlemleri yalnızca yerel modda çalışır.
    if USING_POSTGRES:
        return
    c = conn()

    c.executescript("""
    CREATE TABLE IF NOT EXISTS machines(
        id INTEGER PRIMARY KEY,
        machine_code TEXT UNIQUE,
        status TEXT,
        production INTEGER,
        target INTEGER,
        planned_time REAL,
        downtime REAL,
        ideal_cycle REAL,
        defective INTEGER,
        product TEXT,
        operator TEXT,
        shift TEXT,
        last_maintenance TEXT,
        next_maintenance TEXT
    );

    CREATE TABLE IF NOT EXISTS production_history(
        id INTEGER PRIMARY KEY,
        machine_id INTEGER,
        machine_code TEXT,
        quantity INTEGER,
        timestamp TEXT
    );

    CREATE TABLE IF NOT EXISTS downtime(
        id INTEGER PRIMARY KEY,
        machine_id INTEGER,
        machine_code TEXT,
        reason TEXT,
        duration REAL,
        start_time TEXT,
        end_time TEXT,
        notes TEXT
    );

    CREATE TABLE IF NOT EXISTS alarms(
        id INTEGER PRIMARY KEY,
        machine_id INTEGER,
        machine_code TEXT,
        alarm TEXT,
        level TEXT,
        time TEXT,
        acknowledged INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS work_orders(
        id INTEGER PRIMARY KEY,
        order_no TEXT UNIQUE,
        machine_id INTEGER,
        machine_code TEXT,
        product TEXT,
        target INTEGER,
        produced INTEGER DEFAULT 0,
        priority TEXT,
        status TEXT,
        due_date TEXT
    );

    CREATE TABLE IF NOT EXISTS sensors(
        id INTEGER PRIMARY KEY,
        machine_id INTEGER UNIQUE,
        machine_code TEXT UNIQUE,
        temperature REAL,
        vibration REAL,
        pressure REAL,
        rpm INTEGER,
        timestamp TEXT
    );

    CREATE TABLE IF NOT EXISTS sensor_history(
        id INTEGER PRIMARY KEY,
        machine_id INTEGER,
        machine_code TEXT,
        temperature REAL,
        vibration REAL,
        pressure REAL,
        rpm INTEGER,
        timestamp TEXT
    );

    CREATE TABLE IF NOT EXISTS quality(
        id INTEGER PRIMARY KEY,
        machine_id INTEGER,
        machine_code TEXT,
        product TEXT,
        produced INTEGER,
        defective INTEGER,
        defect_reason TEXT,
        timestamp TEXT
    );

    CREATE TABLE IF NOT EXISTS maintenance(
        id INTEGER PRIMARY KEY,
        machine_id INTEGER,
        machine_code TEXT,
        maintenance_type TEXT,
        description TEXT,
        maintenance_date TEXT,
        next_date TEXT,
        technician TEXT,
        status TEXT
    );

    CREATE TABLE IF NOT EXISTS operators(
        id INTEGER PRIMARY KEY,
        name TEXT UNIQUE,
        department TEXT,
        shift TEXT,
        active INTEGER
    );

    CREATE TABLE IF NOT EXISTS shifts(
        id INTEGER PRIMARY KEY,
        shift_name TEXT,
        start_time TEXT,
        end_time TEXT,
        supervisor TEXT,
        status TEXT
    );

    CREATE TABLE IF NOT EXISTS shift_machine_assignments(
        id INTEGER PRIMARY KEY,
        shift_name TEXT NOT NULL,
        machine_code TEXT NOT NULL,
        operator_name TEXT NOT NULL,
        active INTEGER DEFAULT 1,
        UNIQUE(shift_name, machine_code)
    );

    CREATE TABLE IF NOT EXISTS products(
        id INTEGER PRIMARY KEY,
        product_code TEXT UNIQUE,
        product_name TEXT,
        unit TEXT,
        ideal_cycle REAL,
        stock INTEGER,
        min_stock INTEGER
    );

    CREATE TABLE IF NOT EXISTS stock(
        id INTEGER PRIMARY KEY,
        product_id INTEGER,
        product_code TEXT,
        movement_type TEXT,
        quantity INTEGER,
        reason TEXT,
        timestamp TEXT
    );

    CREATE TABLE IF NOT EXISTS alarm_actions(
        alarm_id INTEGER PRIMARY KEY,
        assignee TEXT,
        note TEXT,
        assigned_at TEXT,
        resolved_at TEXT,
        status TEXT DEFAULT 'Atandı'
    );

    CREATE TABLE IF NOT EXISTS energy_readings(
        id INTEGER PRIMARY KEY,
        machine_id INTEGER,
        machine_code TEXT,
        power_kw REAL,
        energy_kwh REAL,
        timestamp TEXT
    );

    CREATE TABLE IF NOT EXISTS maintenance_requests(
        id INTEGER PRIMARY KEY,
        machine_code TEXT,
        request_type TEXT,
        description TEXT,
        priority TEXT,
        requested_by TEXT,
        requested_at TEXT,
        assigned_to TEXT,
        status TEXT DEFAULT 'Açık',
        completed_at TEXT
    );

    CREATE TABLE IF NOT EXISTS audit_log(
        id INTEGER PRIMARY KEY,
        username TEXT,
        action TEXT,
        entity TEXT,
        details TEXT,
        timestamp TEXT
    );

    CREATE TABLE IF NOT EXISTS notifications(
        id INTEGER PRIMARY KEY,
        recipient_username TEXT,
        recipient_role TEXT,
        notification_type TEXT,
        priority TEXT,
        title TEXT,
        message TEXT,
        machine_code TEXT,
        target_module TEXT,
        entity_type TEXT,
        entity_id INTEGER,
        is_read INTEGER DEFAULT 0,
        is_completed INTEGER DEFAULT 0,
        created_at TEXT,
        read_at TEXT,
        completed_at TEXT
    );

    CREATE TABLE IF NOT EXISTS user_permissions(
        username TEXT,
        module TEXT,
        PRIMARY KEY(username, module)
    );
    """)

    # İş emrinin makinedeki başlangıç sayacını saklayarak, her emri kendi
    # miktarı üzerinden takip ederiz. Mevcut veritabanları güvenle yükseltilir.
    work_order_columns = [row["name"] for row in c.execute("PRAGMA table_info(work_orders)")]
    if "start_production" not in work_order_columns:
        c.execute("ALTER TABLE work_orders ADD COLUMN start_production INTEGER DEFAULT 0")
    downtime_columns = [row["name"] for row in c.execute("PRAGMA table_info(downtime)")]
    if "event_at" not in downtime_columns:
        c.execute("ALTER TABLE downtime ADD COLUMN event_at TEXT")
    for history_table in ("production_history", "sensor_history", "energy_readings"):
        history_columns = [row["name"] for row in c.execute(f"PRAGMA table_info({history_table})")]
        if "shift" not in history_columns:
            c.execute(f"ALTER TABLE {history_table} ADD COLUMN shift TEXT")
    # Eski kayıtlar için gösterilen gerçekleşen miktarı iş emri miktarıyla sınırla.
    c.execute("UPDATE work_orders SET produced=target WHERE produced > target")

    # Zaman serisi ekranları ve günlük analizler için temel performans indeksleri.
    c.executescript("""
        CREATE INDEX IF NOT EXISTS idx_production_history_time ON production_history(timestamp);
        CREATE INDEX IF NOT EXISTS idx_sensor_history_time ON sensor_history(timestamp);
        CREATE INDEX IF NOT EXISTS idx_energy_readings_time ON energy_readings(timestamp);
        CREATE INDEX IF NOT EXISTS idx_downtime_machine_event ON downtime(machine_code,event_at);
        CREATE INDEX IF NOT EXISTS idx_alarms_machine_time ON alarms(machine_code,time);
        CREATE INDEX IF NOT EXISTS idx_assignments_shift_machine ON shift_machine_assignments(shift_name,machine_code);
        CREATE INDEX IF NOT EXISTS idx_production_history_shift_time ON production_history(shift,timestamp);
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin','operator')),
            full_name TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TEXT
        )
    """)

    if c.execute("SELECT COUNT(*) FROM machines").fetchone()[0] == 0:
        machines = [
            (
                "CNC-01", "Çalışıyor", 850, 1000, 480, 30, 0.50, 20,
                "Mil Parçası", "Ahmet Yılmaz", "Sabah",
                "2026-09-10", "2026-09-20"
            ),
            (
                "CNC-02", "Çalışıyor", 650, 900, 480, 60, 0.58, 15,
                "Flanş Parçası", "Mehmet Demir", "Sabah",
                "2026-09-08", "2026-09-18"
            ),
            (
                "CNC-03", "Beklemede", 500, 800, 480, 120, 0.60, 30,
                "Gövde Parçası", "Ali Kaya", "Sabah",
                "2026-09-05", "2026-09-16"
            ),
        ]

        c.executemany("""
            INSERT INTO machines(
                machine_code,status,production,target,planned_time,downtime,
                ideal_cycle,defective,product,operator,shift,
                last_maintenance,next_maintenance
            )
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, machines)

    if c.execute("SELECT COUNT(*) FROM operators").fetchone()[0] == 0:
        c.executemany("""
            INSERT INTO operators(name,department,shift,active)
            VALUES(?,?,?,?)
        """, [
            ("Ahmet Yılmaz", "Üretim", "Sabah", 1),
            ("Mehmet Demir", "Üretim", "Sabah", 1),
            ("Ali Kaya", "Üretim", "Sabah", 1),
            ("Zeynep Çelik", "Kalite", "Akşam", 1),
        ])

    # Üç vardiyada her CNC için bir operatör bulunacak örnek üretim kadrosu.
    c.executemany("INSERT OR IGNORE INTO operators(name,department,shift,active) VALUES(?,?,?,?)", [
        ("Emre Arslan", "Üretim", "Akşam", 1),
        ("Seda Yıldız", "Üretim", "Akşam", 1),
        ("Burak Şahin", "Üretim", "Akşam", 1),
        ("Elif Aydın", "Üretim", "Gece", 1),
        ("Kaan Öztürk", "Üretim", "Gece", 1),
        ("Deniz Koç", "Üretim", "Gece", 1),
    ])

    if c.execute("SELECT COUNT(*) FROM shifts").fetchone()[0] == 0:
        c.executemany("""
            INSERT INTO shifts(shift_name,start_time,end_time,supervisor,status)
            VALUES(?,?,?,?,?)
        """, [
            ("Sabah", "08:00", "16:00", "Hasan Kaya", "Aktif"),
            ("Akşam", "16:00", "00:00", "Murat Demir", "Planlı"),
            ("Gece", "00:00", "08:00", "Ayşe Yıldız", "Planlı"),
        ])

    # Vardiya-makine-operatör atamaları; Admin ekranından daha sonra değiştirilebilir.
    if c.execute("SELECT COUNT(*) FROM shift_machine_assignments").fetchone()[0] == 0:
        c.executemany("""
            INSERT INTO shift_machine_assignments(shift_name,machine_code,operator_name,active)
            VALUES(?,?,?,?)
        """, [
            ("Sabah", "CNC-01", "Ahmet Yılmaz", 1), ("Sabah", "CNC-02", "Mehmet Demir", 1), ("Sabah", "CNC-03", "Ali Kaya", 1),
            ("Akşam", "CNC-01", "Emre Arslan", 1), ("Akşam", "CNC-02", "Seda Yıldız", 1), ("Akşam", "CNC-03", "Burak Şahin", 1),
            ("Gece", "CNC-01", "Elif Aydın", 1), ("Gece", "CNC-02", "Kaan Öztürk", 1), ("Gece", "CNC-03", "Deniz Koç", 1),
        ])

    # Makine ekranı ve simülasyon, aktif vardiyanın atanan çalışanını gösterir.
    active_shift = active_shift_name()
    c.execute("""
        UPDATE machines
        SET shift=?, operator=COALESCE((
            SELECT operator_name FROM shift_machine_assignments a
            WHERE a.shift_name=? AND a.machine_code=machines.machine_code AND a.active=1
        ), operator)
    """, (active_shift, active_shift))

    if c.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0:
        c.executemany("""
            INSERT INTO products(
                product_code,product_name,unit,ideal_cycle,stock,min_stock
            )
            VALUES(?,?,?,?,?,?)
        """, [
            ("UR-001", "Mil Parçası", "Adet", 0.50, 1250, 500),
            ("UR-002", "Flanş Parçası", "Adet", 0.58, 680, 300),
            ("UR-003", "Gövde Parçası", "Adet", 0.60, 320, 200),
            ("HM-001", "Çelik Mil", "Adet", 0, 1800, 500),
            ("HM-002", "Kesici Takım", "Adet", 0, 42, 20),
        ])

    if c.execute("SELECT COUNT(*) FROM sensors").fetchone()[0] == 0:
        for r in c.execute(
            "SELECT id,machine_code,status FROM machines"
        ).fetchall():
            c.execute("""
                INSERT INTO sensors(
                    machine_id,machine_code,temperature,vibration,
                    pressure,rpm,timestamp
                )
                VALUES(?,?,?,?,?,?,?)
            """, (
                r["id"],
                r["machine_code"],
                55 if r["status"] == "Çalışıyor" else 30,
                2.2,
                6,
                1200 if r["status"] == "Çalışıyor" else 0,
                now()
            ))

    if c.execute("SELECT COUNT(*) FROM downtime").fetchone()[0] == 0:
        ids = {
            r["machine_code"]: r["id"]
            for r in c.execute("SELECT id,machine_code FROM machines")
        }

        rows = [
            (ids["CNC-01"], "CNC-01", "Bakım", 15, "10:00", "10:15", "Periyodik bakım"),
            (ids["CNC-01"], "CNC-01", "Malzeme Bekleme", 10, "11:20", "11:30", ""),
            (ids["CNC-01"], "CNC-01", "Arıza", 5, "13:00", "13:05", ""),
            (ids["CNC-02"], "CNC-02", "Malzeme Bekleme", 40, "09:30", "10:10", ""),
            (ids["CNC-02"], "CNC-02", "Operatör Bekleme", 20, "12:00", "12:20", ""),
            (ids["CNC-03"], "CNC-03", "Arıza", 80, "08:30", "09:50", ""),
            (ids["CNC-03"], "CNC-03", "Bakım", 40, "10:00", "10:40", ""),
        ]

        c.executemany("""
            INSERT INTO downtime(
                machine_id,machine_code,reason,duration,start_time,end_time,notes
            )
            VALUES(?,?,?,?,?,?,?)
        """, rows)

    if c.execute("SELECT COUNT(*) FROM work_orders").fetchone()[0] == 0:
        for i, r in enumerate(
            c.execute("SELECT * FROM machines").fetchall(), 1
        ):
            c.execute("""
                INSERT INTO work_orders(
                    order_no,machine_id,machine_code,product,target,
                    produced,priority,status,due_date
                )
                VALUES(?,?,?,?,?,?,?,?,?)
            """, (
                f"WO-{i:03d}",
                r["id"],
                r["machine_code"],
                r["product"],
                r["target"],
                r["production"],
                "Yüksek" if i == 1 else "Normal",
                "Devam Ediyor" if r["status"] == "Çalışıyor" else "Bekliyor",
                "2026-09-16"
            ))

    if c.execute("SELECT COUNT(*) FROM quality").fetchone()[0] == 0:
        for r in c.execute("SELECT * FROM machines").fetchall():
            c.execute("""
                INSERT INTO quality(
                    machine_id,machine_code,product,produced,
                    defective,defect_reason,timestamp
                )
                VALUES(?,?,?,?,?,?,?)
            """, (
                r["id"], r["machine_code"], r["product"],
                r["production"], r["defective"], "Ölçü dışı", now()
            ))

    if c.execute("SELECT COUNT(*) FROM maintenance").fetchone()[0] == 0:
        for r in c.execute("SELECT * FROM machines").fetchall():
            c.execute("""
                INSERT INTO maintenance(
                    machine_id,machine_code,maintenance_type,description,
                    maintenance_date,next_date,technician,status
                )
                VALUES(?,?,?,?,?,?,?,?)
            """, (
                r["id"], r["machine_code"], "Periyodik Bakım",
                "Genel CNC kontrolü", r["last_maintenance"],
                r["next_maintenance"], "Teknik Servis", "Planlandı"
            ))

    # Eski iki rollü şemayı, bakım ve kalite rollerini destekleyecek biçimde yükselt.
    user_schema = c.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone()[0]
    if "maintenance" not in user_schema.lower():
        c.execute("ALTER TABLE users RENAME TO users_legacy")
        c.execute("""
            CREATE TABLE users(
                id INTEGER PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('admin','operator','maintenance','quality')),
                full_name TEXT,
                is_active INTEGER DEFAULT 1,
                created_at TEXT
            )
        """)
        c.execute("""
            INSERT INTO users(id,username,password_hash,role,full_name,is_active,created_at)
            SELECT id,username,password_hash,role,full_name,is_active,created_at FROM users_legacy
        """)
        c.execute("DROP TABLE users_legacy")

    def _initial_hash(password):
        salt = os.urandom(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120000)
        return f"{salt.hex()}${digest.hex()}"

    for username, password, role, full_name in [
        ("admin", "Admin123!", "admin", "trex Admin"),
        ("operator", "Operator123!", "operator", "trex Operatör"),
        ("maintenance", "Maintenance123!", "maintenance", "trex Bakım"),
        ("quality", "Quality123!", "quality", "trex Kalite"),
    ]:
        if not c.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            c.execute("""
                INSERT INTO users(username,password_hash,role,full_name,is_active,created_at)
                VALUES(?,?,?,?,?,?)
            """, (username, _initial_hash(password), role, full_name, 1, now()))

    c.commit()
    c.close()


def verify_password(password, stored_hash):
    try:
        salt_hex, digest_hex = stored_hash.split("$", 1)
        salt = bytes.fromhex(salt_hex)
        calculated = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, 120000
        ).hex()
        return hmac.compare_digest(calculated, digest_hex)
    except (ValueError, TypeError):
        return False


@st.cache_data(ttl=300, show_spinner=False)
def default_password_accounts():
    defaults = {
        "admin": "Admin123!",
        "operator": "Operator123!",
        "maintenance": "Maintenance123!",
        "quality": "Quality123!",
    }
    users = _read_query("SELECT username,password_hash FROM users WHERE is_active=1")
    return [
        str(row["username"])
        for _, row in users.iterrows()
        if row["username"] in defaults and verify_password(defaults[row["username"]], row["password_hash"])
    ]


def authenticate(username, password):
    user = q("""
        SELECT username, password_hash, role, full_name
        FROM users
        WHERE username=? AND is_active=1
    """, (username.strip(),))
    if user.empty or not verify_password(password, user.iloc[0]["password_hash"]):
        return None
    return {
        "username": user.iloc[0]["username"],
        "role": user.iloc[0]["role"],
        "full_name": user.iloc[0]["full_name"],
    }


def is_admin():
    return st.session_state.get("role") == "admin"


ROLE_LABELS = {
    "admin": "ADMIN",
    "operator": "OPERATÖR",
    "maintenance": "BAKIM",
    "quality": "KALİTE",
}


def has_role(*roles):
    return st.session_state.get("role") in roles


ROLE_MODULES = {
    "admin": None,
    "operator": {"🏠 Ana Sayfa", "🏭 Makine", "⚡ Enerji Takibi", "📈 Üretim", "🧮 Manuel OEE", "👥 OLE", "📋 İş Emirleri", "📡 Sensörler", "⏱️ Duruşlar", "👷 Vardiya", "🧰 Bakım Talebi", "🔎 Detay", "📺 Andon Ekranı", "🔳 QR Makine", "🌐 Dijital Olgunluk", "🧠 Akıllı Analiz"},
    "maintenance": {"🏠 Ana Sayfa", "🏭 Makine", "⚡ Enerji Takibi", "🚨 Alarmlar", "📋 İş Emirleri", "📡 Sensörler", "⏱️ Duruşlar", "👷 Vardiya", "👥 OLE", "🧰 Bakım Talebi", "🔧 Bakım", "🔎 Detay", "📺 Andon Ekranı", "🔳 QR Makine", "🌐 Dijital Olgunluk", "🧠 Akıllı Analiz"},
    "quality": {"🏠 Ana Sayfa", "🏭 Makine", "⚡ Enerji Takibi", "📈 Üretim", "📋 İş Emirleri", "📡 Sensörler", "👷 Vardiya", "🧰 Bakım Talebi", "✅ Kalite", "🔎 Detay", "📺 Andon Ekranı", "🔳 QR Makine", "🌐 Dijital Olgunluk", "🧠 Akıllı Analiz"},
}

NAV_LABELS = {
    "🏠 Ana Sayfa": "Genel Bakış", "🏭 Makine": "Makineler", "📈 Üretim": "Üretim",
    "🧮 Manuel OEE": "OEE Hesaplama", "🚨 Alarmlar": "Alarm Yönetimi", "📋 İş Emirleri": "İş Emirleri",
    "📡 Sensörler": "Sensörler", "⏱️ Duruşlar": "Duruş Yönetimi", "👷 Vardiya": "Vardiyalar",
    "🧰 Bakım Talebi": "Bakım Talepleri", "✅ Kalite": "Kalite", "🔧 Bakım": "Bakım Yönetimi",
    "📦 Stok": "Stok", "🔎 Detay": "Makine Detayı", "📄 Raporlar": "Raporlar",
    "📺 Andon Ekranı": "Andon Panosu", "🔳 QR Makine": "QR Makine", "📜 Denetim Kaydı": "Denetim ve Yedekleme",
    "👥 Kullanıcı Yönetimi": "Kullanıcı ve Yetki", "📊 Veritabanı": "Veri ve Raporlama",
    "🧠 Akıllı Analiz": "Akıllı Analiz", "👥 OLE": "İşgücü OLE", "🌐 Dijital Olgunluk": "Dijital Olgunluk",
}


_permission_cache = {}


def can_access_module(module):
    """Rol varsayılanlarını veya kullanıcıya özel modül yetkilerini uygular."""
    if module == "🎯 Yönetici Merkezi":
        return False
    username = st.session_state.get("username")
    role = st.session_state.get("role")
    if role == "admin":
        return True
    if username:
        if username not in _permission_cache:
            _permission_cache[username] = q("SELECT module FROM user_permissions WHERE username=?", (username,))
        custom_permissions = _permission_cache[username]
        custom_modules = custom_permissions["module"].tolist() if not custom_permissions.empty else []
        if "__CUSTOM__" in custom_modules:
            return module in custom_modules
    allowed = ROLE_MODULES.get(role, set())
    return module in allowed


def target_forecast(machine_frame):
    """Son üretim hızına göre makine hedefinin vardiya içi görünümünü hesaplar."""
    history = q("""
        SELECT machine_code, quantity, timestamp FROM production_history
        ORDER BY machine_code, id DESC LIMIT 180
    """)
    rows = []
    for _, machine in machine_frame.iterrows():
        code = machine["machine_code"]
        series = history[history["machine_code"] == code].copy().head(12)
        speed = 0.0
        if len(series) >= 2:
            series["timestamp"] = pd.to_datetime(series["timestamp"], errors="coerce")
            oldest, newest = series.iloc[-1], series.iloc[0]
            elapsed = (newest["timestamp"] - oldest["timestamp"]).total_seconds() / 3600
            if elapsed > 0:
                speed = max((float(newest["quantity"]) - float(oldest["quantity"])) / elapsed, 0)
        if speed == 0 and float(machine["planned_time"]) > 0:
            speed = float(machine["production"]) / (float(machine["planned_time"]) / 60)
        remaining = max(int(machine["target"]) - int(machine["production"]), 0)
        hours = (remaining / speed) if speed > 0 else None
        status = "Tamamlandı" if remaining == 0 else ("Hedefte" if hours is not None and hours <= 8 else "Riskte")
        rows.append({
            "Makine": code, "Kalan": remaining,
            "Hız (adet/saat)": round(speed, 1),
            "Tahmini süre": "Tamamlandı" if remaining == 0 else (f"{hours:.1f} saat" if hours is not None else "Veri yetersiz"),
            "Durum": status
        })
    return pd.DataFrame(rows)


@st.cache_data(ttl=15, show_spinner=False)
def make_excel_report():
    output = io.BytesIO()
    tables = {
        "Makine": q('SELECT machine_code AS "Makine", status AS "Durum", production AS "Uretim", target AS "Hedef", product AS "Urun", operator AS "Operator", shift AS "Vardiya" FROM machines ORDER BY machine_code'),
        "Alarmlar": q('SELECT machine_code AS "Makine", alarm AS "Alarm", level AS "Seviye", time AS "Zaman", acknowledged AS "Onaylandi" FROM alarms ORDER BY id DESC'),
        "Is Emirleri": q('SELECT order_no AS "Is_Emri", machine_code AS "Makine", product AS "Urun", target AS "Hedef", produced AS "Uretilen", priority AS "Oncelik", status AS "Durum", due_date AS "Termin" FROM work_orders ORDER BY id DESC'),
        "Bakim": q('SELECT machine_code AS "Makine", maintenance_type AS "Bakim_Tipi", maintenance_date AS "Tarih", next_date AS "Sonraki_Bakim", technician AS "Teknisyen", status AS "Durum" FROM maintenance ORDER BY next_date'),
        "Kalite": q('SELECT machine_code AS "Makine", product AS "Urun", produced AS "Uretilen", defective AS "Hatali", defect_reason AS "Hata_Nedeni", timestamp AS "Zaman" FROM quality ORDER BY id DESC'),
        "Vardiya Tahmini": target_forecast(q("SELECT * FROM machines ORDER BY machine_code")),
    }
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for sheet, table in tables.items():
            table.to_excel(writer, sheet_name=sheet[:31], index=False)
    return output.getvalue()


@st.cache_data(ttl=15, show_spinner=False)
def make_pdf_report():
    machines = q("SELECT machine_code, production, target, status FROM machines ORDER BY machine_code")
    lines = ["trex MES - Operasyon Ozeti", f"Rapor tarihi: {datetime.now():%Y-%m-%d %H:%M}", ""]
    for _, item in machines.iterrows():
        lines.append(f"{item['machine_code']}: {int(item['production'])}/{int(item['target'])} adet - {item['status']}")
    lines += ["", f"Acik alarm: {int(q('SELECT COUNT(*) AS n FROM alarms WHERE acknowledged=0').iloc[0]['n'])}"]
    safe_lines = [unicodedata.normalize("NFKD", line).encode("ascii", "ignore").decode("ascii") for line in lines]
    content = ["BT", "/F1 16 Tf", "50 790 Td"]
    for index, line in enumerate(safe_lines):
        if index:
            content.append("0 -22 Td")
        content.append("(" + line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ") Tj")
    content.append("ET")
    stream = "\n".join(content).encode("latin-1")
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>", b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>", b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(pdf)); pdf.extend(f"{number} 0 obj\n".encode()); pdf.extend(obj); pdf.extend(b"\nendobj\n")
    xref = len(pdf); pdf.extend(f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]: pdf.extend(f"{offset:010d} 00000 n \n".encode())
    pdf.extend(f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode())
    return bytes(pdf)


def machine_qr_url(machine_code):
    """QR için herkese açık veya yerel makine detay bağlantısını döndürür."""
    if PUBLIC_APP_URL:
        return f"{PUBLIC_APP_URL}/?machine={quote(machine_code)}"
    try:
        host = socket.gethostbyname(socket.gethostname())
        if host.startswith("127."):
            host = "localhost"
    except OSError:
        host = "localhost"
    return f"http://{host}:8501/?machine={quote(machine_code)}"


def generate_qr_image(target_url):
    """QR görselini yerel olarak üretir; dış QR servisine ihtiyaç duymaz."""
    try:
        import qrcode
        image = qrcode.make(target_url)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
    except ImportError:
        return None


def status_badge(status):
    return {
        "Çalışıyor": "🟢 Çalışıyor",
        "Beklemede": "🟡 Beklemede",
        "Arızalı": "🔴 Arızalı",
    }.get(status, f"⚪ {status}")


def require_admin():
    if not is_admin():
        st.error("Bu işlem için Admin yetkisi gereklidir.")
        return False
    return True


# =========================================================
# OEE
# =========================================================

def oee(row):
    planned = float(row["planned_time"])
    downtime = float(row["downtime"])
    production = int(row["production"])
    defective = int(row["defective"])
    ideal_cycle = float(row["ideal_cycle"])

    operating_time = max(planned - downtime, 0)

    availability = (
        operating_time / planned
        if planned > 0
        else 0
    )

    performance = (
        ideal_cycle * production / operating_time
        if operating_time > 0
        else 0
    )

    performance = min(max(performance, 0), 1)

    quality = (
        (production - defective) / production
        if production > 0
        else 0
    )

    quality = min(max(quality, 0), 1)

    return availability, performance, quality, availability * performance * quality


# =========================================================
# API
# =========================================================

def api_get(path):
    try:
        response = requests.get(
            API_URL + path,
            timeout=2
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException:
        return None


def api_post(path):
    try:
        response = requests.post(
            API_URL + path,
            timeout=3
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException:
        return None


@st.cache_data(ttl=15, show_spinner=False)
def api_is_alive():
    """Sekme geçişlerini bekletmemek için API durumunu kısa süre önbellekte tutar."""
    try:
        response = requests.get(API_URL + "/", timeout=.35)
        return response.ok
    except requests.RequestException:
        return False


def dispatch_queued_work_orders(c):
    """Boşalan uygun makinelere öncelik sırasıyla bekleyen işleri atar."""
    assigned = []
    available = c.execute("""
        SELECT id, machine_code, production, target
        FROM machines
        WHERE status != 'Arızalı' AND production >= target
        ORDER BY machine_code
    """).fetchall()
    for machine in available:
        active = c.execute("""
            SELECT 1 FROM work_orders
            WHERE machine_id=? AND status != 'Tamamlandı'
            LIMIT 1
        """, (machine["id"],)).fetchone()
        if active:
            continue
        queued = c.execute("""
            SELECT id, order_no, product, target
            FROM work_orders
            WHERE status='Sırada' AND machine_id IS NULL
            ORDER BY CASE priority
                WHEN 'Kritik' THEN 1 WHEN 'Yüksek' THEN 2
                WHEN 'Normal' THEN 3 ELSE 4 END,
                due_date, id
            LIMIT 1
        """).fetchone()
        if not queued:
            break
        start_production = int(machine["production"])
        c.execute("""
            UPDATE work_orders
            SET machine_id=?, machine_code=?, start_production=?, status='Üretimde'
            WHERE id=?
        """, (machine["id"], machine["machine_code"], start_production, queued["id"]))
        c.execute("""
            UPDATE machines SET target=?, product=?, status='Çalışıyor'
            WHERE id=?
        """, (start_production + int(queued["target"]), queued["product"], machine["id"]))
        assigned.append((queued["order_no"], machine["machine_code"]))
    return assigned


def sync_from_api():
    data = api_get("/machines")

    if not data:
        return False

    c = conn()

    for item in data:
        c.execute("""
            UPDATE machines
            SET status=?,
                production=?,
                target=?,
                planned_time=?,
                downtime=?,
                ideal_cycle=?,
                defective=?,
                product=?,
                operator=?,
                shift=?,
                last_maintenance=?,
                next_maintenance=?
            WHERE machine_code=?
        """, (
            item.get("status"),
            item.get("production"),
            item.get("target"),
            item.get("planned_time"),
            item.get("downtime"),
            item.get("ideal_cycle"),
            item.get("defective"),
            item.get("product"),
            item.get("operator"),
            item.get("shift"),
            item.get("last_maintenance"),
            item.get("next_maintenance"),
            item.get("machine_code"),
        ))

    dispatch_queued_work_orders(c)
    c.commit()
    c.close()
    return True


def pull_api_simulation():
    result = api_post("/simulate")

    if not result:
        return False

    return sync_from_api()


# =========================================================
# LOCAL FALLBACK SIMULATION
# =========================================================

def local_simulate():
    c = conn()
    timestamp = now()
    today = date.today()
    thresholds = get_sensor_thresholds()
    summary = {"production": 0, "alarms": 0, "status_changes": 0, "queued_dispatches": 0, "spc_measurements": 0}

    for r in c.execute("SELECT * FROM machines ORDER BY machine_code").fetchall():
        previous_production = int(r["production"])
        production = previous_production
        target = int(r["target"])
        status = r["status"]
        new_status = status
        downtime = float(r["downtime"])
        planned = min(float(r["planned_time"]) + random_float(.5, 1.5), 1440)
        defective = int(r["defective"])

        # Bakım tarihi yaklaşan makinelerde geçici duruş veya arıza riski artar.
        try:
            maintenance_due = date.fromisoformat(r["next_maintenance"]) <= today
        except (TypeError, ValueError):
            maintenance_due = False
        risk_multiplier = 1.8 if maintenance_due else 1.0

        if status == "Çalışıyor":
            increment = random_int(5, 18)
            production = min(production + increment, target)
            summary["production"] += production - previous_production

            transition_chance = random_float()
            if production >= target:
                new_status = "Beklemede"
            elif transition_chance < .025 * risk_multiplier:
                new_status = "Arızalı"
                downtime += random_float(2, 6)
            elif transition_chance < .075 * risk_multiplier:
                new_status = "Beklemede"
                downtime += random_float(1, 3)

        elif status == "Beklemede":
            # Plan tamamlandıysa yeni iş emri beklenir; tamamlanmadıysa yeniden devreye alınabilir.
            if production < target and random_float() < .35:
                new_status = "Çalışıyor"
            else:
                downtime += random_float(.1, .5)

        else:  # Arızalı
            downtime += random_float(1, 2.5)
            if random_float() < (.12 if maintenance_due else .22):
                new_status = "Beklemede"

        sensor = c.execute(
            "SELECT * FROM sensors WHERE machine_id=?", (r["id"],)
        ).fetchone()
        alarm_list = []

        if sensor:
            temperature = float(sensor["temperature"])
            vibration = float(sensor["vibration"])
            pressure = float(sensor["pressure"])
            rpm = int(sensor["rpm"])

            if new_status == "Çalışıyor":
                temperature += random_float(-.7, 1.8) + (.4 if maintenance_due else 0)
                vibration += random_float(-.12, .28) + (.08 if maintenance_due else 0)
                pressure += random_float(-.12, .10)
                rpm = max(950, min(1600, rpm + random_int(-35, 35)))
            elif new_status == "Beklemede":
                temperature += (40 - temperature) * .12
                vibration += (0.2 - vibration) * .18
                pressure += (5.8 - pressure) * .10
                rpm = 0
            else:
                temperature += random_float(.8, 2.8)
                vibration += random_float(.25, .75)
                pressure += random_float(-.30, .05)
                rpm = 0

            temperature = round(max(20, min(temperature, 105)), 1)
            vibration = round(max(0, min(vibration, 12)), 2)
            pressure = round(max(0, min(pressure, 8)), 2)

            if (temperature >= thresholds["temperature_critical"] + 10
                    or vibration >= thresholds["vibration_critical"] + 3
                    or pressure < thresholds["pressure_critical"]):
                new_status = "Arızalı"
            if temperature >= thresholds["temperature_critical"]:
                alarm_list.append(("Motor sıcaklığı kritik seviyede", "Kritik"))
            elif temperature >= thresholds["temperature_warning"]:
                alarm_list.append(("Motor sıcaklığı yükseldi", "Uyarı"))
            if vibration >= thresholds["vibration_critical"]:
                alarm_list.append(("Titreşim seviyesi kritik", "Kritik"))
            elif vibration >= thresholds["vibration_warning"]:
                alarm_list.append(("Titreşim seviyesi yükseldi", "Uyarı"))
            if pressure <= thresholds["pressure_critical"]:
                alarm_list.append(("Basınç seviyesi düşük", "Kritik"))
            elif pressure <= thresholds["pressure_warning"]:
                alarm_list.append(("Basınç seviyesi düşüyor", "Uyarı"))
            if maintenance_due:
                alarm_list.append(("Planlı bakım tarihi geldi", "Uyarı"))

            c.execute("""
                UPDATE sensors
                SET temperature=?, vibration=?, pressure=?, rpm=?, timestamp=?
                WHERE machine_id=?
            """, (temperature, vibration, pressure, rpm, timestamp, r["id"]))
            c.execute("""
                INSERT INTO sensor_history(machine_id,machine_code,temperature,vibration,pressure,rpm,timestamp,shift)
                VALUES(?,?,?,?,?,?,?,?)
            """, (r["id"], r["machine_code"], temperature, vibration, pressure, rpm, timestamp, r["shift"]))

        if production > previous_production and random_float() < .18:
            defective = min(defective + 1, production)
            c.execute("""
                INSERT INTO quality(machine_id,machine_code,product,produced,defective,defect_reason,timestamp)
                VALUES(?,?,?,?,?,?,?)
            """, (r["id"], r["machine_code"], r["product"], production, 1, "Simülasyon kalite kontrolü", timestamp))

        if production > previous_production and new_status == "Çalışıyor":
            recent_spc = c.execute("""
                SELECT measurement_value FROM spc_measurements
                WHERE machine_code=? AND measurement_name='Çap'
                ORDER BY timestamp DESC LIMIT 20
            """, (r["machine_code"],)).fetchall()
            recent_values = [float(item["measurement_value"]) for item in reversed(recent_spc)]
            process_drift = .018 if r["machine_code"] == "CNC-02" and random_float() < .35 else 0
            spc_value = round(25 + random_float(-.035, .035) + process_drift, 3)
            c.execute("""
                INSERT INTO spc_measurements(
                    id,machine_id,machine_code,product,measurement_name,
                    measurement_value,unit,timestamp,spec_low,spec_high
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """, (int(uuid.uuid4().int % 2_000_000_000) or 1, r["id"], r["machine_code"],
                  r["product"] or "Tanımsız Ürün", "Çap", spc_value, "mm", timestamp, 24.90, 25.10))
            summary["spc_measurements"] += 1
            if len(recent_values) >= 5:
                series = pd.Series(recent_values, dtype=float)
                center = float(series.mean())
                deviation = float(series.std(ddof=1))
                outside_control = deviation > 0 and (spc_value > center + 3 * deviation or spc_value < center - 3 * deviation)
                outside_spec = spc_value > 25.10 or spc_value < 24.90
                if outside_control or outside_spec:
                    alarm_text = f"SPC: Çap ölçümü kontrol limitini aştı ({spc_value:.3f} mm)"
                    recent_alarm = c.execute("SELECT 1 FROM alarms WHERE machine_id=? AND alarm LIKE 'SPC:%' AND acknowledged=0 LIMIT 1", (r["id"],)).fetchone()
                    if not recent_alarm:
                        alarm_list.append((alarm_text, "Kritik" if outside_spec else "Uyarı"))

        if new_status != status:
            summary["status_changes"] += 1
            if new_status in ("Beklemede", "Arızalı"):
                reason = "Plan hedefi tamamlandı" if production >= target else (
                    "Simülasyon arızası" if new_status == "Arızalı" else "Kısa operasyon duruşu"
                )
                c.execute("""
                    INSERT INTO downtime(machine_id,machine_code,reason,duration,start_time,end_time,notes,event_at)
                    VALUES(?,?,?,?,?,?,?,?)
                """, (r["id"], r["machine_code"], reason, round(max(downtime - float(r["downtime"]), 0), 1), timestamp, timestamp, "Yerel simülasyon olayı", timestamp))

        for alarm_text, level in alarm_list:
            recent = c.execute("""
                SELECT 1 FROM alarms
                WHERE machine_id=? AND alarm=? AND acknowledged=0
                LIMIT 1
            """, (r["id"], alarm_text)).fetchone()
            if not recent:
                c.execute("""
                    INSERT INTO alarms(machine_id,machine_code,alarm,level,time)
                    VALUES(?,?,?,?,?)
                """, (r["id"], r["machine_code"], alarm_text, level, timestamp))
                summary["alarms"] += 1

        c.execute("""
            UPDATE machines
            SET status=?, production=?, planned_time=?, downtime=?, defective=?
            WHERE id=?
        """, (new_status, production, planned, downtime, defective, r["id"]))
        c.execute("""
            INSERT INTO production_history(machine_id,machine_code,quantity,timestamp,shift)
            VALUES(?,?,?,?,?)
        """, (r["id"], r["machine_code"], production, timestamp, r["shift"]))
        power_kw = round(
            random_float(10, 18) if new_status == "Çalışıyor"
            else (random_float(1.2, 3.2) if new_status == "Beklemede" else random_float(.4, 1.1)), 2
        )
        energy_kwh = round(power_kw / 60, 3)
        c.execute("""
            INSERT INTO energy_readings(machine_id,machine_code,power_kw,energy_kwh,timestamp,shift)
            VALUES(?,?,?,?,?,?)
        """, (r["id"], r["machine_code"], power_kw, energy_kwh, timestamp, r["shift"]))
        active_order = c.execute("""
            SELECT id, target, start_production FROM work_orders
            WHERE machine_id=? AND status != 'Tamamlandı'
            ORDER BY id DESC LIMIT 1
        """, (r["id"],)).fetchone()
        if active_order:
            order_produced = max(0, production - int(active_order["start_production"] or 0))
            order_status = "Tamamlandı" if order_produced >= int(active_order["target"]) else (
                "Üretimde" if new_status == "Çalışıyor" else "Bekliyor"
            )
            c.execute(
                "UPDATE work_orders SET produced=?, status=? WHERE id=?",
                (min(order_produced, int(active_order["target"])), order_status, active_order["id"])
            )

    summary["queued_dispatches"] = len(dispatch_queued_work_orders(c))
    c.commit()
    c.close()
    return summary


def random_int(a, b):
    import random
    return random.randint(a, b)


def random_float(a=0, b=1):
    import random
    return random.uniform(a, b)


# =========================================================
# STREAMLIT
# =========================================================

st.set_page_config(
    page_title="trex MES",
    page_icon="🏭",
    layout="wide"
)

@st.cache_resource(show_spinner=False)
def initialize_local_database(database_path, schema_version):
    init_db()
    return True


if not USING_POSTGRES:
    initialize_local_database(os.path.abspath(DB), "stability-v1")

notification_database_identity = (
    hashlib.sha256(DATABASE_URL.encode()).hexdigest() if USING_POSTGRES else os.path.abspath(DB)
)
ensure_notification_schema(notification_database_identity)
ensure_spc_schema(notification_database_identity)
ensure_maturity_schema(notification_database_identity)
ensure_five_why_schema(notification_database_identity)


# =========================================================
# TREX YEŞİL - BEYAZ KURUMSAL TEMA
# =========================================================

st.markdown("""
<style>
/* Ana sayfa */
.stApp {
    background:
        radial-gradient(circle at 92% 4%, rgba(55, 180, 82, 0.16) 0, rgba(55, 180, 82, 0) 22%),
        radial-gradient(circle at 6% 96%, rgba(32, 140, 65, 0.13) 0, rgba(32, 140, 65, 0) 24%),
        linear-gradient(135deg, #f8fffa 0%, #ffffff 48%, #eefaf1 100%);
}

/* Üst bölümde yumuşak yeşil dekorasyon */
.main .block-container {
    padding-top: .75rem;
    padding-bottom: 3rem;
    max-width: 1500px;
}

/* Sol menü */
section[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #063b24 0%, #07552d 48%, #0a6a35 100%);
    border-right: 1px solid rgba(255,255,255,0.08);
}

section[data-testid="stSidebar"] > div {
    background: transparent;
}

section[data-testid="stSidebar"] * {
    color: #ffffff;
}

/* Menü daraltma düğmesi kullanılmadığı için gizlenir; logo zarif bir filigran görünümündedir. */
button[data-testid="stSidebarCollapseButton"] { display: none !important; }
section[data-testid="stSidebar"] img { opacity: .68; filter: saturate(.84); }

/* Sidebar butonları */
section[data-testid="stSidebar"] .stButton > button {
    background: rgba(255,255,255,0.08);
    color: #ffffff;
    border: 1px solid rgba(255,255,255,0.10);
    border-radius: 10px;
    font-weight: 600;
    transition: all 0.2s ease;
}

section[data-testid="stSidebar"] .stButton > button:hover {
    background: rgba(82, 210, 103, 0.25);
    border-color: rgba(120, 230, 135, 0.55);
    transform: translateY(-1px);
}

/* Sidebar seçim alanlarında beyaz metin–açık zemin çakışmasını önle. */
section[data-testid="stSidebar"] div[data-baseweb="select"] > div {
    background: rgba(2, 42, 25, .72) !important;
    border-color: rgba(143, 237, 169, .42) !important;
}
section[data-testid="stSidebar"] div[data-baseweb="select"] span,
section[data-testid="stSidebar"] div[data-baseweb="select"] input {
    color: #f5fff7 !important;
    -webkit-text-fill-color: #f5fff7 !important;
}

/* Vardiya bağlamı için bileşene özel kontrast: Streamlit sürümleri arasında
   seçili değerin beyaz kalmasını önlemek için anahtar sınıfı hedeflenir. */
section[data-testid="stSidebar"] .st-key-shift_context [data-baseweb="select"] > div {
    background: #eaf8ee !important;
    border: 1px solid #8dd3a4 !important;
}
section[data-testid="stSidebar"] .st-key-shift_context [data-baseweb="select"] input,
section[data-testid="stSidebar"] .st-key-shift_context [data-baseweb="select"] div,
section[data-testid="stSidebar"] .st-key-shift_context [data-baseweb="select"] span {
    color: #087a38 !important;
    -webkit-text-fill-color: #087a38 !important;
}
section[data-testid="stSidebar"] .st-key-shift_context [data-baseweb="select"] svg {
    fill: #087a38 !important;
    color: #087a38 !important;
}

/* Sağ üstteki vardiya seçicisi: açık zemin üzerinde koyu yeşil, okunaklı metin. */
.st-key-shift_context [data-baseweb="select"] > div {
    background: #eaf8ee !important;
    border: 1px solid #8dd3a4 !important;
}
.st-key-shift_context [data-baseweb="select"] input,
.st-key-shift_context [data-baseweb="select"] span,
.st-key-shift_context [data-baseweb="select"] div {
    color: #087a38 !important;
    -webkit-text-fill-color: #087a38 !important;
}
.st-key-shift_context [data-baseweb="select"] svg {
    fill: #087a38 !important;
    color: #087a38 !important;
}

/* Başlık */
h1 {
    color: #063b24 !important;
    font-weight: 800 !important;
    letter-spacing: -0.8px;
}

h2, h3 {
    color: #0a4d2d !important;
}

/* KPI kartları */
div[data-testid="stMetric"] {
    background: rgba(255,255,255,0.90);
    border: 1px solid #d9eee0;
    border-radius: 16px;
    padding: 18px 20px;
    box-shadow: 0 5px 20px rgba(7, 84, 45, 0.08);
}

div[data-testid="stMetricLabel"] {
    color: #507261 !important;
    font-weight: 600;
}

div[data-testid="stMetricValue"] {
    color: #063b24 !important;
    font-weight: 800;
}

/* Genel kutular / expander */
div[data-testid="stExpander"] {
    background: rgba(255,255,255,0.88);
    border: 1px solid #d9eee0;
    border-radius: 14px;
}

/* Sekmeler */
button[data-baseweb="tab"] {
    color: #35624a !important;
    font-weight: 600;
}

button[data-baseweb="tab"][aria-selected="true"] {
    color: #087a38 !important;
}

div[data-baseweb="tab-highlight"] {
    background-color: #20a64a !important;
}

/* Veri tabloları */
div[data-testid="stDataFrame"] {
    --gdg-bg-cell: rgba(255, 255, 255, 0.62) !important;
    --gdg-bg-cell-medium: rgba(246, 253, 248, 0.72) !important;
    --gdg-bg-header: rgba(229, 246, 234, 0.86) !important;
    --gdg-bg-header-has-focus: rgba(210, 239, 219, 0.95) !important;
    --gdg-text-dark: #183c2b !important;
    --gdg-text-medium: #527060 !important;
    --gdg-border-color: #d2e9d9 !important;
    --gdg-accent-color: #159447 !important;
    border: 1px solid rgba(186, 222, 197, 0.85);
    border-radius: 12px;
    overflow: hidden;
    background: rgba(255,255,255,0.42);
    box-shadow: 0 4px 15px rgba(7, 84, 45, 0.04);
}

/* Grafik çevresi */
div[data-testid="stPlotlyChart"] {
    background: rgba(255,255,255,0.82);
    border: 1px solid #d9eee0;
    border-radius: 14px;
    padding: 8px;
    box-shadow: 0 4px 15px rgba(7, 84, 45, 0.05);
}

/* Streamlit butonları */
.stButton > button {
    border-radius: 10px;
    border: 1px solid #bfe4ca;
    font-weight: 600;
}

.stButton > button:hover {
    border-color: #20a64a;
    color: #087a38;
}

/* Ana sayfadaki tıklanabilir KPI kartları */
.main button[kind="secondary"][data-testid="stBaseButton-secondary"] {
    min-height: 50px;
    background: rgba(255,255,255,.92);
    border-color: #bfe4ca;
    color: #123c28;
    font-size: .95rem;
    font-weight: 650;
}

/* Başarı / uyarı kutuları */
div[data-testid="stAlert"] {
    border-radius: 12px;
}

/* Selectbox ve inputlar */
div[data-baseweb="select"] > div,
div[data-baseweb="input"] > div {
    border-radius: 10px;
    border-color: #cce6d4;
}

/* Ayırıcı */
hr {
    border-color: #d9eee0;
}

/* Alt bilgi */
.trex-footer {
    margin-top: 35px;
    padding: 18px 22px;
    border-radius: 16px;
    background: linear-gradient(135deg, #eaf8ee, #ffffff);
    border: 1px solid #cfead7;
    color: #35624a;
    text-align: center;
    font-size: 0.9rem;
}

/* =========================================================
   ANA DASHBOARD - REFERANS TASARIM
   ========================================================= */

.trex-topbar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 4px 8px 18px 8px;
}

/* Tüm modüllerde başlık satırını kompakt tut; sağ üst bilgilerin oluşturduğu
   gereksiz dikey boşluğu kaldır. */
.trex-compact-topbar {
    min-height: 44px;
    padding: 0 4px 8px !important;
    gap: 18px;
}
.trex-compact-topbar .trex-hero-title { font-size: 1.08rem; }
.trex-compact-topbar .trex-kicker { margin-top: 0 !important; font-size: .75rem; }
.trex-compact-meta {
    margin-left: auto;
    display: flex;
    align-items: center;
    gap: 14px;
    color: #527463;
    font-size: .68rem;
    white-space: nowrap;
}
.trex-compact-meta b { color: #15563a; }
.st-key-notification_center button {
    min-height: 38px !important;
    border-radius: 10px !important;
    border-color: #bfe5cf !important;
    background: linear-gradient(135deg,#ffffff,#eaf8ef) !important;
    color: #075337 !important;
    font-weight: 850 !important;
}
.notification-card {
    border: 1px solid #dbece3;
    border-left: 4px solid var(--notification-color);
    border-radius: 9px;
    background: #fff;
    padding: 9px 10px;
    margin: 7px 0 4px;
}
.notification-card.unread { background: #f3fbf6; }
.notification-card small { color:#71877b;font-size:.66rem; }
.notification-card b { display:block;color:#123f2d;font-size:.78rem;margin:2px 0; }
.notification-card span { color:#4d6a5c;font-size:.69rem;line-height:1.35;display:block; }
@media (max-width: 900px) { .trex-compact-meta { display: none; } }

.trex-kicker {
    color: #5c7668;
    font-size: 0.92rem;
    margin-top: -10px;
}

.trex-section {
    background: rgba(255,255,255,0.92);
    border: 1px solid #dcefe2;
    border-radius: 16px;
    padding: 13px;
    box-shadow: 0 5px 22px rgba(7, 84, 45, 0.07);
    margin-bottom: 12px;
}

.trex-section-title {
    color: #0a4d2d;
    font-size: 1.02rem;
    font-weight: 800;
    margin-bottom: 8px;
}

.trex-small {
    color: #668073;
    font-size: 0.82rem;
}

.trex-status {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    font-weight: 600;
}

.trex-dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    display: inline-block;
}

.trex-green { background: #20a64a; }
.trex-yellow { background: #f2b233; }
.trex-red { background: #e34d4d; }

.trex-hero {
    background:
        radial-gradient(circle at 95% 0%, rgba(90,205,110,0.20), transparent 27%),
        linear-gradient(135deg, #ffffff 0%, #effaf2 100%);
    border: 1px solid #d7eddd;
    border-radius: 20px;
    padding: 16px 20px;
    margin-bottom: 14px;
    box-shadow: 0 7px 25px rgba(7,84,45,0.07);
}

.trex-hero-title {
    color: #063b24;
    font-size: 1.55rem;
    font-weight: 850;
}

.trex-hero-sub {
    color: #607a6c;
    margin-top: 3px;
}

.trex-info-card {
    background: rgba(255,255,255,0.92);
    border: 1px solid #dcefe2;
    border-radius: 14px;
    padding: 12px;
    box-shadow: 0 4px 16px rgba(7,84,45,0.05);
}

.trex-info-title {
    color: #123f2a;
    font-weight: 750;
    font-size: 0.92rem;
}

.trex-info-value {
    color: #063b24;
    font-size: 1.45rem;
    font-weight: 850;
    margin-top: 3px;
}

.trex-positive {
    color: #14933e;
    font-size: 0.82rem;
    font-weight: 700;
}

.trex-muted {
    color: #71877b;
    font-size: 0.78rem;
}

.trex-brand-card {
    background:
        radial-gradient(circle at 90% 20%, rgba(50,180,80,0.16), transparent 35%),
        linear-gradient(135deg, #eefaf1, #ffffff);
    border: 1px solid #d5ebda;
    border-radius: 16px;
    padding: 22px;
    min-height: 185px;
}

.trex-brand-card strong {
    color: #07552d;
    font-size: 1.08rem;
}


/* KPI / METRIC YAZILARI - OKUNABİLİR RENK */
div[data-testid="stMetric"] {
    background: #ffffff !important;
    border: 1px solid #d5eadb !important;
    border-radius: 16px !important;
    padding: 18px 20px !important;
    box-shadow: 0 5px 20px rgba(7, 84, 45, 0.08) !important;
}

div[data-testid="stMetric"] label,
div[data-testid="stMetric"] label *,
div[data-testid="stMetricLabel"],
div[data-testid="stMetricLabel"] * {
    color: #315847 !important;
    opacity: 1 !important;
}

div[data-testid="stMetricValue"],
div[data-testid="stMetricValue"] *,
div[data-testid="stMetric"] [data-testid="stMetricValue"] {
    color: #063b24 !important;
    opacity: 1 !important;
    font-weight: 800 !important;
}

div[data-testid="stMetricDelta"],
div[data-testid="stMetricDelta"] *,
div[data-testid="stMetric"] [data-testid="stMetricDelta"] {
    opacity: 1 !important;
}

div[data-testid="stMetricDelta"] svg {
    opacity: 1 !important;
}

/* KPI kartlarının içindeki normal yazılar */
div[data-testid="stMetric"] p,
div[data-testid="stMetric"] span {
    color: #315847 !important;
    opacity: 1 !important;
}


/* Dikey MES menüsü */
section[data-testid="stSidebar"] .stRadio > div {
    gap: 2px !important;
}
section[data-testid="stSidebar"] .stRadio label {
    border-radius: 7px !important;
    padding: 8px 9px !important;
    margin: 1px 0 !important;
    color: #ffffff !important;
    font-weight: 600 !important;
    transition: background .15s ease, transform .15s ease;
}
section[data-testid="stSidebar"] .stRadio label:hover {
    background: rgba(40, 190, 80, 0.20) !important;
}
section[data-testid="stSidebar"] .stRadio label p {
    color: #ffffff !important;
    font-size: 0.94rem !important;
}
section[data-testid="stSidebar"] .stRadio [data-testid="stMarkdownContainer"] {
    color: #ffffff !important;
}

.nav-group-title {
    color: #aee8bd;
    font-size: .64rem;
    font-weight: 800;
    letter-spacing: .12em;
    margin: 16px 0 7px;
    padding-left: 4px;
}

section[data-testid="stSidebar"] div[data-testid="stButton"] button {
    justify-content: flex-start;
    padding: 8px 11px !important;
    min-height: 38px;
    border: 1px solid rgba(255,255,255,0) !important;
    border-radius: 9px !important;
    background: rgba(255,255,255,.025) !important;
    color: #ffffff !important;
    box-shadow: none !important;
    font-size: .86rem !important;
    font-weight: 650 !important;
    letter-spacing: .01em;
}

section[data-testid="stSidebar"] div[data-testid="stButton"] button:hover {
    background: rgba(74, 196, 103, .16) !important;
    border-color: rgba(190, 244, 205, .18) !important;
    transform: translateX(2px);
}

section[data-testid="stSidebar"] div[data-testid="stButton"] button[kind="primary"] {
    background: linear-gradient(90deg, rgba(42, 190, 82, .46), rgba(23, 135, 61, .30)) !important;
    border-color: rgba(190, 244, 205, .28) !important;
    box-shadow: inset 3px 0 0 #a7f0b7 !important;
}

.sidebar-nav-caption {
    margin: 16px 2px 4px;
    padding: 10px 12px;
    border: 1px solid rgba(198, 244, 208, .13);
    border-radius: 10px;
    background: rgba(0,0,0,.10);
    color: #cdeed5;
    font-size: .71rem;
    line-height: 1.45;
}
.sidebar-nav-caption strong {
    color: #ffffff;
    font-size: .72rem;
    letter-spacing: .04em;
}

.trex-priority {
    border-radius: 12px;
    padding: 12px 14px;
    min-height: 92px;
    border: 1px solid #dcefe2;
    background: #fff;
}
.trex-priority-alert { border-left: 4px solid #e34d4d; }
.trex-priority-ok { border-left: 4px solid #20a64a; }
.trex-priority-title { color: #315847; font-size: .76rem; font-weight: 800; }
.trex-priority-value { color: #063b24; font-size: 1.3rem; font-weight: 850; margin: 4px 0; }
.trex-priority-note { color: #71877b; font-size: .76rem; }

/* REFERANS GORSELE YAKIN ANA DASHBOARD */
.dashboard-ring-card {background:rgba(255,255,255,.96);border:1px solid #d8e5dc;border-radius:14px;padding:14px 12px;box-shadow:0 5px 15px rgba(0,45,25,.12);min-height:112px;display:flex;align-items:center;gap:14px;}
.dashboard-ring {--value:80;--ring:#16b8c6;width:68px;height:68px;border-radius:50%;background:conic-gradient(var(--ring) calc(var(--value)*1%),#e8efeb 0);position:relative;flex:0 0 68px;}
.dashboard-ring:after {content:"";position:absolute;inset:8px;background:#fff;border-radius:50%;}
.dashboard-ring-text {position:relative;z-index:2;text-align:left;}.dashboard-ring-title{font-size:.83rem;font-weight:800;color:#1d2d25}.dashboard-ring-value{font-size:1.35rem;font-weight:900;color:#07150e;line-height:1.15}.dashboard-ring-note{font-size:.70rem;color:#52675b;margin-top:3px}
.machine-card {border-radius:12px;padding:10px 10px 8px;background:#fff;border:1px solid #dbe7df;box-shadow:0 4px 12px rgba(0,40,22,.10);min-height:150px}.machine-card.running{background:linear-gradient(145deg,#dff8e8,#fff)}.machine-card.waiting{background:linear-gradient(145deg,#fff3c5,#fff)}.machine-card.faulty{background:linear-gradient(145deg,#ffe0e0,#fff)}.machine-code{font-size:1rem;font-weight:900;color:#17251d}.machine-status{font-size:.68rem;font-weight:800;float:right}.machine-icon{font-size:2.7rem;text-align:center;margin:10px 0 7px}.machine-stats{display:flex;justify-content:space-between;font-size:.72rem;color:#52675b}.machine-stats b{display:block;color:#122018;font-size:.98rem}.trex-priority{box-shadow:0 5px 14px rgba(0,40,22,.12);min-height:105px}.trex-priority-alert{background:linear-gradient(145deg,#fff,#ffe9e9)}.trex-priority-ok{background:linear-gradient(145deg,#fff,#eafaf0)}

@media (max-width: 900px) {
    .trex-hero-title { font-size: 1.25rem; }
    .trex-topbar { padding-bottom: 10px; }
    div[data-testid="stMetric"] { padding: 13px 14px !important; }
}

/* =========================================================
   KURUMSAL LOGIN EKRANI
   ========================================================= */
body:has(.login-page-intro) .stApp {
    background: url("/app/static/login-factory-bg.png") center center / cover no-repeat fixed !important;
    overflow: hidden;
}

body:has(.login-page-intro) .stApp::before {
    display: none;
}

body:has(.login-page-intro) .main {
    position: relative;
    z-index: 1;
}

body:has(.login-page-intro) .main .block-container {
    max-width: 760px !important;
    min-height: 100vh;
    padding-top: 5vh !important;
    padding-bottom: 3vh !important;
}

body:has(.login-page-intro) section[data-testid="stSidebar"] {
    display: none !important;
}

.login-page-intro {
    text-align: center;
    margin: 0 auto 18px auto;
}

.login-brand-signature {
    display: block;
    width: min(100%, 390px);
    height: auto;
    margin: 0 auto 16px auto;
    filter: drop-shadow(0 8px 22px rgba(0, 0, 0, .48));
}

.login-security {
    display: inline-block;
    padding: 6px 12px;
    border: 1px solid rgba(192, 245, 204, .42);
    border-radius: 999px;
    background: rgba(2, 47, 27, .48);
    color: #d9ffe2;
    font-size: .68rem;
    font-weight: 800;
    letter-spacing: 1.4px;
}

.login-title {
    margin-top: 12px;
    color: #ffffff;
    font-size: 2rem;
    font-weight: 850;
    letter-spacing: -.7px;
}

.login-subtitle {
    margin-top: 3px;
    color: #d0ead8;
    font-size: .94rem;
}

.login-card-head {
    text-align: center;
    padding: 20px 8px 8px 8px;
}

.login-card-title {
    color: #ffffff;
    font-size: 1.16rem;
    font-weight: 800;
}

.login-card-caption {
    color: #c6dfce;
    font-size: .82rem;
    margin-top: 5px;
}

/* Login form alanı */
body:has(.login-page-intro) div[data-testid="stForm"] {
    background: rgba(3, 47, 28, .76) !important;
    border: 1px solid rgba(202, 247, 212, .27) !important;
    border-radius: 20px !important;
    padding: 26px 28px 24px 28px !important;
    box-shadow: 0 24px 70px rgba(0, 14, 7, .42) !important;
    backdrop-filter: blur(15px);
}

body:has(.login-page-intro) div[data-testid="stForm"] label {
    color: #dff8e6 !important;
    font-weight: 700 !important;
    font-size: .84rem !important;
}

body:has(.login-page-intro) div[data-testid="stForm"] input {
    border: 1px solid rgba(208, 239, 215, .76) !important;
    border-radius: 11px !important;
    background: rgba(255,255,255,.95) !important;
    color: #183c2b !important;
    min-height: 46px !important;
}

body:has(.login-page-intro) div[data-testid="stForm"] input:focus {
    border-color: #20a64a !important;
    box-shadow: 0 0 0 3px rgba(32,166,74,.10) !important;
}

body:has(.login-page-intro) div[data-testid="stForm"] button {
    margin-top: 10px !important;
    min-height: 48px !important;
    border: none !important;
    border-radius: 11px !important;
    background: linear-gradient(135deg, #07552d, #18a34a) !important;
    color: #ffffff !important;
    font-size: .88rem !important;
    font-weight: 800 !important;
    letter-spacing: .35px;
    box-shadow: 0 8px 22px rgba(7,85,45,.20) !important;
}

body:has(.login-page-intro) div[data-testid="stForm"] button:hover {
    background: linear-gradient(135deg, #063b24, #14933e) !important;
    transform: translateY(-1px);
}

.login-role-info {
    text-align: center;
    margin: 15px auto 0 auto;
    color: #72877c;
    font-size: .75rem;
}

.login-role-info span {
    color: #20a64a;
    font-size: .9rem;
    margin-right: 5px;
}

.login-footer {
    text-align: center;
    margin: 24px auto 0 auto;
    color: #315847;
    font-size: .78rem;
}

.login-footer span {
    display: block;
    margin-top: 5px;
    color: #8a9c93;
    font-size: .68rem;
}


/* TUM MODULLER - ANA SAYFA ILE AYNI KART DILI */
div[data-testid="stForm"] {background:rgba(255,255,255,.94);border:1px solid #d8e8dc;border-radius:16px;padding:16px;box-shadow:0 5px 18px rgba(0,45,25,.07)}
div[data-testid="stVerticalBlockBorderWrapper"] {border-color:#d8e8dc !important;border-radius:16px !important;background:rgba(255,255,255,.82)}
.mes-record-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:12px;margin:8px 0 16px}.mes-record-card{background:rgba(255,255,255,.96);border:1px solid #d7e7db;border-radius:14px;padding:13px 14px;box-shadow:0 4px 14px rgba(0,45,25,.07);position:relative;overflow:hidden}.mes-record-card:before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:linear-gradient(#19a94b,#62d47e)}.mes-record-title{font-size:.94rem;font-weight:850;color:#073d24;margin-bottom:9px;padding-left:3px}.mes-record-fields{display:grid;grid-template-columns:1fr 1fr;gap:7px 12px}.mes-record-field{min-width:0}.mes-record-label{font-size:.65rem;text-transform:uppercase;letter-spacing:.045em;color:#7a8d82;font-weight:750}.mes-record-value{font-size:.82rem;color:#183b2a;font-weight:650;white-space:normal;word-break:break-word}.mes-record-empty{background:#fff;border:1px dashed #cfe1d4;border-radius:14px;padding:20px;text-align:center;color:#6f8478}.mes-record-note{font-size:.72rem;color:#71877b;margin:-5px 0 12px}
/* Plotly kartlari dashboard ile ayni gorunsun */
div[data-testid="stPlotlyChart"]{background:#fff !important;border-radius:14px !important;border:1px solid #d8e5dc !important;box-shadow:0 5px 15px rgba(0,45,25,.08) !important;padding:8px !important}

</style>
""", unsafe_allow_html=True)

# =========================================================
# KART TABANLI KAYIT GORUNUMU
# Excel benzeri st.dataframe gorunumunu tum modullerde kartlara cevirir.
# =========================================================
_original_dataframe = st.dataframe

def _mes_card_dataframe(data=None, *args, **kwargs):
    if data is None:
        st.markdown('<div class="mes-record-empty">Gösterilecek kayıt yok.</div>', unsafe_allow_html=True)
        return None
    try:
        frame = data.data if hasattr(data, "data") and isinstance(data.data, pd.DataFrame) else data
        if isinstance(frame, pd.Series):
            frame = frame.to_frame().T
        if not isinstance(frame, pd.DataFrame):
            frame = pd.DataFrame(frame)
    except Exception:
        return _original_dataframe(data, *args, **kwargs)

    if frame.empty:
        st.markdown('<div class="mes-record-empty">Gösterilecek kayıt bulunamadı.</div>', unsafe_allow_html=True)
        return None

    clean = frame.copy()
    clean.columns = [str(c) for c in clean.columns]
    max_rows = 60
    shown = clean.head(max_rows)
    cards = ['<div class="mes-record-grid">']
    for idx, (_, row) in enumerate(shown.iterrows(), 1):
        vals = row.to_dict()
        first_col = clean.columns[0] if len(clean.columns) else "Kayıt"
        first_val = vals.get(first_col, idx)
        title = html.escape(str(first_val)) if pd.notna(first_val) else f"Kayıt {idx}"
        cards.append('<div class="mes-record-card">')
        cards.append(f'<div class="mes-record-title">{title}</div><div class="mes-record-fields">')
        for col in clean.columns[1:]:
            val = vals.get(col, "")
            if pd.isna(val): val = "—"
            elif isinstance(val, numbers.Real) and not isinstance(val, (bool, int)):
                val = f"{float(val):.2f}".rstrip("0").rstrip(".")
            cards.append(
                f'<div class="mes-record-field"><div class="mes-record-label">{html.escape(str(col))}</div>'
                f'<div class="mes-record-value">{html.escape(str(val))}</div></div>'
            )
        cards.append('</div></div>')
    cards.append('</div>')
    st.markdown(''.join(cards), unsafe_allow_html=True)
    if len(clean) > max_rows:
        st.markdown(f'<div class="mes-record-note">Toplam {len(clean)} kaydın ilk {max_rows} kaydı gösteriliyor.</div>', unsafe_allow_html=True)
    return None

# Operasyon ekranlarında kayıtlar satır/sütun biçiminde okunmalıdır.
# Kart dönüştürücüsü yalnızca eski tasarım denemesiydi; Streamlit'in gerçek
# tablo bileşenini koruyoruz.
st.dataframe = _original_dataframe


def render_management_table(frame, title, record_label="kayıt"):
    """Yönetim ekranları için satır/sütun yapısı belirgin, kurumsal HTML tablo."""
    if frame is None or frame.empty:
        st.info("Gösterilecek kayıt bulunamadı.")
        return

    def cell_value(column, value):
        if pd.isna(value):
            value = "—"
        elif isinstance(value, numbers.Real) and not isinstance(value, (bool, int)):
            value = f"{float(value):.2f}".rstrip("0").rstrip(".")
        text = html.escape(str(value))
        if column == "Bakım Durumu":
            badge_class = "late" if "Gecikmiş" in text else ("soon" if "Yaklaşıyor" in text else "planned")
            text = text.replace("🔴 ", "").replace("🟡 ", "").replace("🟢 ", "")
            return f'<span class="mes-status-badge {badge_class}">{text}</span>'
        return text

    headers = "".join(f"<th>{html.escape(str(column))}</th>" for column in frame.columns)
    rows = []
    for _, row in frame.iterrows():
        cells = "".join(
            f"<td>{cell_value(column, row[column])}</td>" for column in frame.columns
        )
        rows.append(f"<tr>{cells}</tr>")
    st.markdown(f"""
    <style>
    .mes-management-wrap{{background:#fff;border:1px solid #dfe7e2;border-radius:9px;box-shadow:0 3px 12px rgba(17,57,36,.08);overflow:hidden;margin:8px 0 18px}}
    .mes-management-head{{display:flex;align-items:center;justify-content:space-between;padding:12px 15px;border-bottom:1px solid #e6ece8;background:#fbfdfb}}
    .mes-management-head b{{font-size:1rem;color:#173f2b}} .mes-management-head span{{font-size:.76rem;color:#658071;background:#eef5f0;border-radius:5px;padding:4px 7px}}
    .mes-management-scroll{{overflow-x:auto}} .mes-management-table{{width:100%;border-collapse:collapse;min-width:980px;font-size:.78rem}}
    .mes-management-table th{{background:#f3f7f4;color:#536b5e;font-size:.66rem;letter-spacing:.035em;text-transform:uppercase;text-align:left;padding:10px 11px;white-space:nowrap;border-bottom:1px solid #dce6df}}
    .mes-management-table td{{color:#173f2b;padding:11px;border-bottom:1px solid #edf1ee;vertical-align:middle;line-height:1.35}}
    .mes-management-table tr:last-child td{{border-bottom:0}} .mes-management-table tr:hover td{{background:#f8fcf9}}
    .mes-status-badge{{display:inline-block;padding:3px 8px;border-radius:5px;font-size:.72rem;font-weight:750;white-space:nowrap}}
    .mes-status-badge.planned{{color:#16733d;background:#dff4e5;border:1px solid #a8dfb8}} .mes-status-badge.soon{{color:#8b6511;background:#fff0c7;border:1px solid #efd17d}} .mes-status-badge.late{{color:#a43232;background:#fde4e4;border:1px solid #efb4b4}}
    .mes-management-foot{{padding:9px 14px;color:#718479;font-size:.72rem;border-top:1px solid #e6ece8;background:#fbfdfb}}
    </style>
    <div class="mes-management-wrap"><div class="mes-management-head"><b>{html.escape(title)}</b><span>Toplam {len(frame)} {html.escape(record_label)}</span></div>
      <div class="mes-management-scroll"><table class="mes-management-table"><thead><tr>{headers}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>
      <div class="mes-management-foot">Gösterilen kayıtlar: 1–{len(frame)} / {len(frame)}</div></div>
    """, unsafe_allow_html=True)
# =========================================================
# KULLANICI GİRİŞİ / OTURUM
# =========================================================
if "authenticated" not in st.session_state:
    st.session_state["authenticated"] = False

if not st.session_state["authenticated"]:
    # Kurumsal giriş ekranı
    login_background_uri = image_data_uri(LOGIN_BACKGROUND_PATH)
    trex_signature_uri = image_data_uri(TREX_SIGNATURE_PATH)
    st.markdown(f"""
    <style>
    body:has(.login-page-intro) .stApp {{
        background: url(\"{login_background_uri}\") center center / cover no-repeat fixed !important;
    }}
    </style>
    """, unsafe_allow_html=True)
    st.markdown(f"""
    <div class="login-page-intro">
        <img class="login-brand-signature" src="{trex_signature_uri}" alt="trex Digital Manufacturing ve E-TURQUALITY">
        <div class="login-security">GÜVENLİ ERİŞİM</div>
        <div class="login-title">trex MES</div>
        <div class="login-subtitle">Üretim Yönetim ve İzleme Platformu</div>
    </div>
    """, unsafe_allow_html=True)

    login_col = st.columns([1.15, 2.0, 1.15])[1]
    with login_col:
        st.markdown("""
        <div class="login-card-head">
            <div class="login-card-title">Sisteme Giriş</div>
            <div class="login-card-caption">Yetkili kullanıcı hesabınız ile devam edin.</div>
        </div>
        """, unsafe_allow_html=True)

        with st.form("login_form", clear_on_submit=False):
            username = st.text_input(
                "Kullanıcı Adı",
                placeholder="Kullanıcı adınızı girin",
                autocomplete="username"
            )
            password = st.text_input(
                "Şifre",
                type="password",
                placeholder="Şifrenizi girin",
                autocomplete="current-password"
            )
            submitted = st.form_submit_button("GİRİŞ YAP", use_container_width=True)

        if submitted:
            user = authenticate(username, password)
            if user:
                st.session_state["authenticated"] = True
                st.session_state["username"] = user["username"]
                st.session_state["role"] = user["role"]
                st.session_state["full_name"] = user["full_name"]
                st.rerun()
            else:
                st.error("Kullanıcı adı veya şifre hatalı.")

        st.markdown("""
        <div class="login-role-info">
            <span>●</span> Yetkili trex MES kullanıcıları için güvenli erişim
        </div>
        """, unsafe_allow_html=True)

    st.markdown("""
    <div class="login-footer">
        <div>🏭 trex Digital Manufacturing</div>
        <span>Endüstriyel üretim süreçleri • OEE • Kalite • Bakım • Operasyon</span>
    </div>
    """, unsafe_allow_html=True)

    st.stop()

# Menüdeki tema seçimi oturum boyunca korunur. Karanlık tema, modül bazlı
# stillerden sonra !important kurallarıyla uygulanarak işlevlere dokunmaz.
if "dark_mode_enabled" not in st.session_state:
    st.session_state["dark_mode_enabled"] = False
if "critical_focus_enabled" not in st.session_state:
    st.session_state["critical_focus_enabled"] = False
if st.session_state["dark_mode_enabled"]:
    st.markdown("""
    <style>
    :root { color-scheme: dark; }
    .stApp,
    [data-testid="stAppViewContainer"] {
        background:
            radial-gradient(circle at 92% 3%, rgba(26,140,86,.17), transparent 23%),
            radial-gradient(circle at 5% 96%, rgba(20,103,70,.15), transparent 25%),
            linear-gradient(135deg,#09130f 0%,#0d1b15 52%,#0a1711 100%) !important;
        color: #dcebe3 !important;
    }
    [data-testid="stHeader"] { background: rgba(8,19,14,.88) !important; }
    .main, .main .block-container, [data-testid="stMainBlockContainer"] { color:#dcebe3 !important; }
    .main h1, .main h2, .main h3, .main h4,
    .main p, .main label, .main li, .main .stMarkdown,
    .main [data-testid="stCaptionContainer"] { color:#dcebe3 !important; }
    .trex-hero-title, .trex-section-title, .trex-info-title,
    .sensor-v3-panel-title, .sensor-v3-machine-name,
    .mesv3-box-title, .home-v2-title { color:#ecfff4 !important; }
    .trex-kicker, .trex-hero-sub, .trex-small, .trex-muted,
    .sensor-v3-reading, .sensor-v3-reading small { color:#9bb9aa !important; }

    /* Streamlit bileşenleri */
    div[data-testid="stMetric"], div[data-testid="stExpander"],
    div[data-testid="stForm"], div[data-testid="stPopoverBody"],
    div[data-testid="stDialog"] > div,
    div[data-testid="stVerticalBlockBorderWrapper"] {
        background:#13251d !important;
        border-color:#294a3a !important;
        box-shadow:0 5px 18px rgba(0,0,0,.24) !important;
    }
    div[data-testid="stMetricLabel"], div[data-testid="stMetricLabel"] *,
    div[data-testid="stMetricValue"], div[data-testid="stMetricValue"] *,
    div[data-testid="stMetric"] p, div[data-testid="stMetric"] span {
        color:#e7f8ee !important;
    }
    div[data-baseweb="select"] > div,
    div[data-baseweb="input"] > div,
    div[data-baseweb="base-input"],
    div[data-baseweb="textarea"] > div,
    [data-testid="stDateInput"] input,
    [data-testid="stNumberInput"] input,
    [data-testid="stTextInput"] input {
        background:#14271e !important;
        border-color:#315542 !important;
        color:#e8f8ef !important;
        -webkit-text-fill-color:#e8f8ef !important;
    }
    div[data-baseweb="select"] span, div[data-baseweb="select"] input,
    div[data-baseweb="select"] svg { color:#e8f8ef !important; fill:#e8f8ef !important; }
    [data-baseweb="popover"], [role="listbox"], [role="option"] {
        background:#14271e !important; color:#e8f8ef !important;
    }
    button[data-baseweb="tab"] { color:#a9c8b8 !important; }
    button[data-baseweb="tab"][aria-selected="true"] { color:#62d594 !important; }
    .main .stButton > button, .main button[kind="secondary"] {
        background:#172b21 !important; border-color:#345843 !important; color:#e7f8ee !important;
    }
    .main .stButton > button:hover { background:#1d392b !important; border-color:#55b77e !important; color:#fff !important; }
    hr { border-color:#294a3a !important; }

    /* Tablolar ve grafik alanları */
    div[data-testid="stDataFrame"] {
        --gdg-bg-cell:#13251d !important;
        --gdg-bg-cell-medium:#172b21 !important;
        --gdg-bg-header:#1b3327 !important;
        --gdg-bg-header-has-focus:#244837 !important;
        --gdg-text-dark:#e4f4eb !important;
        --gdg-text-medium:#a5c0b2 !important;
        --gdg-border-color:#2e4b3d !important;
        background:#13251d !important; border-color:#2e4b3d !important;
    }
    div[data-testid="stPlotlyChart"] { background:#12231b !important; border-color:#294a3a !important; }
    .mes-management-wrap, .mes-management-head, .mes-management-foot,
    .mes-management-scroll, .mes-management-table, .mes-management-table th,
    .mes-management-table td { background:#13251d !important; color:#dcebe3 !important; border-color:#294a3a !important; }
    .mes-management-table tr:hover td { background:#1a3126 !important; }

    /* Uygulamanın özel kart ve panelleri */
    .trex-section, .trex-hero, .trex-info-card, .trex-brand-card,
    .dashboard-ring-card, .machine-card, .trex-priority,
    .sensor-v3-stat, .loss-action, .notification-card, .alarm-compact,
    .mesv3-box, .mesv3-kpi, .home-v2-card, .home-v2-machine,
    .home-v2-panel, .mini-speed-card, .quality-card, .stock-card {
        background:#13251d !important;
        border-color:#2d4d3d !important;
        color:#dcebe3 !important;
        box-shadow:0 5px 17px rgba(0,0,0,.22) !important;
    }
    .notification-card.unread { background:#183426 !important; }
    .data-freshness { background:#183426 !important; color:#cce9da !important; }
    .critical-focus-strip { background:linear-gradient(90deg,#32191b,#18231d) !important; border-color:#704044 !important; color:#f2c9cb !important; }
    .critical-focus-strip span { background:#261b1c !important; border-color:#704044 !important; color:#f2c9cb !important; }
    .notification-card b, .notification-card span,
    .dashboard-ring-title, .dashboard-ring-value, .dashboard-ring-note,
    .machine-code, .machine-stats, .machine-stats b,
    .trex-priority-title, .trex-priority-value, .trex-priority-note,
    .sensor-v3-stat label, .sensor-v3-stat strong, .sensor-v3-stat small,
    .loss-action strong, .loss-action p, .loss-rank b, .loss-rank small {
        color:#dcebe3 !important;
    }
    .dashboard-ring:after { background:#13251d !important; }
    .loss-rank { border-color:#294a3a !important; }
    .trex-footer { background:#13251d !important; border-color:#294a3a !important; color:#a9c8b8 !important; }
    </style>
    """, unsafe_allow_html=True)

# QR bağlantısından gelindiğinde doğrudan ilgili makinenin detay ekranını aç.
qr_machine_from_url = st.query_params.get("machine")
if qr_machine_from_url:
    st.session_state["selected_module"] = "🔎 Detay"
    st.session_state["detail_machine"] = qr_machine_from_url

# =========================================================
# SAYFA BAŞLIĞI
# =========================================================

module_page_info = {
    "🏠 Ana Sayfa": ("Ana Sayfa", "Fabrikanın anlık görünümü ve operasyon öncelikleri"),
    "🏭 Makine": ("Makine", "Makine durumu, üretim ve OEE performansı"),
    "📈 Üretim": ("Üretim", "Üretim kayıtları ve performans takibi"),
    "🧮 Manuel OEE": ("Manuel OEE", "OEE değerlerini manuel olarak hesaplayın"),
    "🚨 Alarmlar": ("Alarmlar", "Alarm kayıtları ve müdahale takibi"),
    "📋 İş Emirleri": ("İş Emirleri", "Aktif ve planlı iş emirleri"),
    "📡 Sensörler": ("Sensörler", "Canlı sensör verileri ve geçmiş değerler"),
    "⏱️ Duruşlar": ("Duruşlar", "Duruş süreleri ve neden analizi"),
    "👷 Vardiya": ("Vardiya", "Vardiya ve operatör bilgileri"),
    "✅ Kalite": ("Kalite", "Ürün kalitesi ve hata oranları"),
    "🔧 Bakım": ("Bakım", "Planlı bakım ve bakım geçmişi"),
    "📦 Stok": ("Stok", "Malzeme ve stok hareketleri"),
    "🔎 Detay": ("Makine Detayı", "Makine bazlı ayrıntılı analiz"),
    "📊 Veritabanı": ("Veritabanı", "SQLite tabloları ve dışa aktarma"),
    "📄 Raporlar": ("Raporlar", "Operasyon raporlarını Excel ve PDF olarak indirin"),
    "⚡ Enerji Takibi": ("Enerji Takibi", "Makine bazında güç, tüketim ve enerji verimliliği"),
    "📺 Andon Ekranı": ("Andon Ekranı", "Fabrika için büyük ekran canlı durum panosu"),
    "🔳 QR Makine": ("QR Makine", "Makine detaylarına QR kod ile hızlı erişim"),
    "🧰 Bakım Talebi": ("Bakım Talebi", "Operatör bakım talepleri ve atama süreci"),
    "📜 Denetim Kaydı": ("Denetim Kaydı", "Kullanıcı işlemleri ve yedekleme kayıtları"),
    "👥 Kullanıcı Yönetimi": ("Kullanıcı Yönetimi", "Admin için rol ve kullanıcı durumu düzenleme"),
    "🧠 Akıllı Analiz": ("Akıllı Analiz", "Üretim, OEE ve bakım riskleri için karar desteği"),
    "👥 OLE": ("İşgücü OLE", "Operatör ve vardiya bazında tahmini işgücü etkinliği"),
    "🌐 Dijital Olgunluk": ("Dijital Fabrika Olgunluğu", "Dokuz kategoride açıklanabilir dijital dönüşüm skoru"),
}
active_module = st.session_state.get("selected_module", "🏠 Ana Sayfa")
if not can_access_module(active_module):
    active_module = "🏠 Ana Sayfa"
    st.session_state["selected_module"] = active_module
active_page_name, active_page_subtitle = module_page_info.get(
    active_module,
    ("MES", "Üretim Yönetim ve İzleme Platformu")
)

# Tüm modüllerde tek satırlık kompakt üst bilgi alanı kullanılır.
active_shift_header = active_shift_name()

# Son canlı veri zamanını sensör tablosundan okur; veri yoksa üretim geçmişine düşer.
freshness_rows = q("SELECT timestamp FROM sensors WHERE timestamp IS NOT NULL ORDER BY timestamp DESC LIMIT 1")
if freshness_rows.empty:
    freshness_rows = q("SELECT timestamp FROM production_history WHERE timestamp IS NOT NULL ORDER BY timestamp DESC LIMIT 1")
latest_data_at = pd.to_datetime(freshness_rows.iloc[0]["timestamp"], errors="coerce") if not freshness_rows.empty else pd.NaT
if pd.notna(latest_data_at):
    if getattr(latest_data_at, "tzinfo", None) is not None:
        latest_data_at = latest_data_at.tz_localize(None)
    freshness_seconds = max(0, int((datetime.now() - latest_data_at.to_pydatetime()).total_seconds()))
    if freshness_seconds < 60:
        freshness_text = f"{freshness_seconds} sn önce"
    elif freshness_seconds < 3600:
        freshness_text = f"{freshness_seconds // 60} dk önce"
    elif freshness_seconds < 86400:
        freshness_text = f"{freshness_seconds // 3600} sa önce"
    else:
        freshness_text = latest_data_at.strftime("%d.%m.%Y %H:%M")
    freshness_state = "live" if freshness_seconds <= 120 else ("warn" if freshness_seconds <= 900 else "stale")
else:
    freshness_text, freshness_state = "Veri bekleniyor", "stale"

critical_focus_counts = {"alarms": 0, "machines": 0, "orders": 0, "maintenance": 0}
if st.session_state.get("critical_focus_enabled", False):
    # Kritik odak kapalıyken bu pahalı sayaçlara hiç ihtiyaç yok. Açıkken de dört
    # ayrı Neon gidiş-dönüşü yerine bütün değerleri tek sorguda alıyoruz.
    critical_rows = q("""
        SELECT
            (SELECT COUNT(*) FROM alarms WHERE acknowledged=0 AND level='Kritik') AS alarms,
            (SELECT COUNT(*) FROM machines WHERE status='Arızalı') AS machines,
            (SELECT COUNT(*) FROM work_orders WHERE status='Gecikmiş' OR (status!='Tamamlandı' AND due_date<?)) AS orders,
            (SELECT COUNT(*) FROM maintenance WHERE status!='Tamamlandı' AND next_date<?) AS maintenance
    """, (str(date.today()), str(date.today())))
    if not critical_rows.empty:
        critical_focus_counts = {name: int(critical_rows.iloc[0][name] or 0) for name in critical_focus_counts}

st.markdown(f"""
<style>
.data-freshness{{display:inline-flex;align-items:center;gap:5px;padding:3px 7px;border-radius:99px;background:#edf8f1;color:#38604c;font-size:.63rem;font-weight:750}}
.data-freshness:before{{content:'';width:7px;height:7px;border-radius:50%;background:{'#22b66b' if freshness_state == 'live' else ('#e8a525' if freshness_state == 'warn' else '#e15359')};box-shadow:0 0 0 3px {'rgba(34,182,107,.13)' if freshness_state == 'live' else 'rgba(225,83,89,.12)'}}}
.critical-focus-strip{{display:flex;align-items:center;gap:10px;padding:8px 11px;margin:0 0 8px;border:1px solid #f1b8ba;border-left:4px solid #e1484f;border-radius:9px;background:linear-gradient(90deg,#fff2f2,#fff);color:#663236}}
.critical-focus-strip strong{{font-size:.78rem;color:#b72f36}}.critical-focus-strip span{{font-size:.67rem;padding:3px 7px;border-radius:99px;background:#fff;border:1px solid #f0c8ca}}
</style>
""", unsafe_allow_html=True)


@st.fragment(run_every="20s")
def notification_refresh_tick():
    """Yeni görev geldiğinde üst çubuğu kullanıcı müdahalesi olmadan yeniler."""
    # Bildirim tablosunu her Streamlit rerun'ında baştan taramak özellikle uzak
    # Neon bağlantısında sayfa geçişlerini yavaşlatıyordu. İlk çizimde mevcut
    # bildirimler doğrudan okunur; kaynak senkronizasyonu en fazla 5 dakikada bir yapılır.
    sync_key = f"notification_sync_at::{st.session_state.get('username', '')}"
    now_tick = time.time()
    last_sync = st.session_state.get(sync_key)
    if last_sync is None:
        st.session_state[sync_key] = now_tick
    elif now_tick - float(last_sync) >= 300:
        sync_current_user_notifications()
        st.session_state[sync_key] = now_tick
    latest_notifications = current_user_notifications()
    latest_unread = int((latest_notifications["is_read"] == 0).sum()) if not latest_notifications.empty else 0
    previous_unread = st.session_state.get("notification_unread_snapshot")
    st.session_state["notification_unread_snapshot"] = latest_unread
    if previous_unread is not None and previous_unread != latest_unread:
        st.rerun(scope="app")


notification_refresh_tick()
header_notifications = current_user_notifications()
unread_notification_count = int((header_notifications["is_read"] == 0).sum()) if not header_notifications.empty else 0
header_left, header_shift, header_notification = st.columns([4.15, 1.25, .58], vertical_alignment="center")
with header_left:
    st.markdown(f"""
    <div class="trex-topbar trex-compact-topbar">
        <div>
            <div class="trex-hero-title">🏭 trex MES · {active_page_name}</div>
            <div class="trex-kicker">{active_page_subtitle}</div>
        </div>
        <div class="trex-compact-meta">
            <span>📅 {datetime.now():%d %B %Y · %H:%M}</span>
            <span class="data-freshness">Veri · {html.escape(freshness_text)}</span>
            <b>👤 {st.session_state.get("full_name", "MES Kullanıcısı")}</b>
        </div>
    </div>
    """, unsafe_allow_html=True)
with header_shift:
    st.selectbox(
        "Vardiya görünümü",
        [f"Aktif vardiya · {active_shift_header}", "Tümü", "Sabah", "Akşam", "Gece"],
        key="shift_context",
        label_visibility="collapsed",
        help="Seçilen vardiyanın makine-operatör ataması tüm ekranlarda uygulanır."
    )
with header_notification:
    with st.popover(f"🔔 {unread_notification_count}", use_container_width=True):
        st.markdown("**Bildirim Merkezi**")
        st.caption("Size ve rolünüze atanmış güncel görevler")
        if header_notifications.empty:
            st.success("Yeni bildiriminiz yok.")
        else:
            current_username = st.session_state.get("username", "")
            current_role = st.session_state.get("role", "")
            if unread_notification_count and st.button("Tümünü okundu işaretle", use_container_width=True, key="notification_read_all"):
                unread_before = q("""
                    SELECT id,is_read,read_at FROM notifications
                    WHERE is_read=0 AND (recipient_username=? OR recipient_role=?)
                """, (current_username, current_role))
                execute("""
                    UPDATE notifications SET is_read=1,read_at=?
                    WHERE is_read=0 AND (recipient_username=? OR recipient_role=?)
                """, (now(), current_username, current_role))
                if not unread_before.empty:
                    register_undo(
                        f"{len(unread_before)} bildirim okundu işaretlendi",
                        [("UPDATE notifications SET is_read=?,read_at=? WHERE id=?", (int(row["is_read"] or 0), row["read_at"], int(row["id"]))) for _, row in unread_before.iterrows()]
                    )
                st.rerun()
            for _, notification in header_notifications.iterrows():
                notification_id = int(notification["id"])
                notification_priority = str(notification["priority"])
                notification_color = "#e3474f" if notification_priority == "Kritik" else ("#efa72b" if notification_priority == "Uyarı" else "#159a63")
                notification_state = "unread" if int(notification["is_read"] or 0) == 0 else ""
                st.markdown(
                    f'<div class="notification-card {notification_state}" style="--notification-color:{notification_color}">'
                    f'<small>{html.escape(str(notification["notification_type"]))} · {html.escape(str(notification["created_at"]))}</small>'
                    f'<b>{html.escape(str(notification["title"]))}</b>'
                    f'<span>{html.escape(str(notification["message"]))}</span></div>',
                    unsafe_allow_html=True,
                )
                notification_actions = st.columns([1.15, .9, .9], gap="small")
                if notification_actions[0].button("Göreve Git", key=f"notification_open_{notification_id}", use_container_width=True):
                    previous_read = int(notification["is_read"] or 0)
                    previous_read_at = notification["read_at"]
                    execute("""
                        UPDATE notifications SET is_read=1,read_at=?
                        WHERE id=? AND (recipient_username=? OR recipient_role=?)
                    """, (now(), notification_id, current_username, current_role))
                    if previous_read == 0:
                        register_undo(
                            f'{notification["title"]} bildirimi okundu',
                            [("UPDATE notifications SET is_read=?,read_at=? WHERE id=?", (previous_read, previous_read_at, notification_id))]
                        )
                    target_module = str(notification["target_module"] or "🏠 Ana Sayfa")
                    st.session_state["selected_module"] = target_module if can_access_module(target_module) else "🏠 Ana Sayfa"
                    if notification["machine_code"]:
                        st.session_state["notification_machine_filter"] = str(notification["machine_code"])
                    st.rerun()
                if notification_actions[1].button("Okundu", key=f"notification_read_{notification_id}", use_container_width=True):
                    previous_read = int(notification["is_read"] or 0)
                    previous_read_at = notification["read_at"]
                    execute("""
                        UPDATE notifications SET is_read=1,read_at=?
                        WHERE id=? AND (recipient_username=? OR recipient_role=?)
                    """, (now(), notification_id, current_username, current_role))
                    if previous_read == 0:
                        register_undo(
                            f'{notification["title"]} bildirimi okundu',
                            [("UPDATE notifications SET is_read=?,read_at=? WHERE id=?", (previous_read, previous_read_at, notification_id))]
                        )
                    st.rerun()
                if notification_actions[2].button("Tamamla", key=f"notification_done_{notification_id}", use_container_width=True):
                    previous_notification_state = (
                        int(notification["is_read"] or 0), int(notification["is_completed"] or 0),
                        notification["read_at"], notification["completed_at"], notification_id
                    )
                    execute("""
                        UPDATE notifications SET is_read=1,is_completed=1,read_at=?,completed_at=?
                        WHERE id=? AND (recipient_username=? OR recipient_role=?)
                    """, (now(), now(), notification_id, current_username, current_role))
                    register_undo(
                        f'{notification["title"]} bildirimi tamamlandı',
                        [("UPDATE notifications SET is_read=?,is_completed=?,read_at=?,completed_at=? WHERE id=?", previous_notification_state)]
                    )
                    audit_event("Bildirimi tamamladı", f"Bildirim #{notification_id}", str(notification["title"]))
                    st.rerun()

if st.session_state.get("critical_focus_enabled", False):
    st.markdown(
        f'<div class="critical-focus-strip"><strong>Kritik Odak Açık</strong>'
        f'<span>{critical_focus_counts["alarms"]} kritik alarm</span>'
        f'<span>{critical_focus_counts["machines"]} arızalı makine</span>'
        f'<span>{critical_focus_counts["orders"]} geciken iş</span>'
        f'<span>{critical_focus_counts["maintenance"]} geciken bakım</span></div>',
        unsafe_allow_html=True,
    )
    focus_alarm_col, focus_machine_col, focus_work_col, focus_maintenance_col, _ = st.columns([1, 1, 1, 1, 3.2], gap="small")
    if focus_alarm_col.button("Kritik alarmlar", key="focus_open_alarms", use_container_width=True):
        st.session_state["selected_module"] = "🚨 Alarmlar"; st.rerun()
    if focus_machine_col.button("Arızalı makineler", key="focus_open_machines", use_container_width=True):
        st.session_state["selected_module"] = "🏭 Makine"; st.rerun()
    if focus_work_col.button("Geciken işler", key="focus_open_orders", use_container_width=True):
        st.session_state["selected_module"] = "📋 İş Emirleri"; st.rerun()
    if focus_maintenance_col.button("Geciken bakımlar", key="focus_open_maintenance", use_container_width=True):
        st.session_state["selected_module"] = "🔧 Bakım"; st.rerun()

with st.sidebar:
    role_label = ROLE_LABELS.get(st.session_state.get("role"), "KULLANICI")
    st.markdown(
        f'<div style="background:rgba(255,255,255,.10);border:1px solid rgba(255,255,255,.14);'
        f'border-radius:10px;padding:10px 12px;margin-bottom:10px;">'
        f'<div style="font-size:.78rem;opacity:.85;">Giriş yapan</div>'
        f'<div style="font-weight:800;">👤 {st.session_state.get("full_name", "")}</div>'
        f'<div style="font-size:.78rem;margin-top:2px;">Rol: <b>{role_label}</b></div></div>',
        unsafe_allow_html=True
    )
    st.toggle(
        "Karanlık Mod",
        key="dark_mode_enabled",
        help="Tüm MES ekranlarında göz yormayan koyu renk temasını açar."
    )
    st.toggle(
        "Kritik Odak Modu",
        key="critical_focus_enabled",
        help="Kritik alarm, arızalı makine ve geciken işleri her sayfanın üstünde öne çıkarır."
    )
    undo_action = pending_undo_action()
    if undo_action:
        undo_seconds_left = max(1, UNDO_WINDOW_SECONDS - int(time.time() - undo_action["created_at"]))
        st.caption(f'↩ {undo_action["label"]} · {undo_seconds_left} sn')
        if st.button("Son işlemi geri al", key="undo_last_action", use_container_width=True):
            if perform_pending_undo():
                st.success("İşlem geri alındı.")
                st.rerun()
    if is_admin():
        unsafe_accounts = default_password_accounts()
        if unsafe_accounts:
            st.warning("Varsayılan şifre kullanan hesaplar: " + ", ".join(unsafe_accounts))
    # TREX kurumsal logo
    if os.path.exists(LOGO_PATH):
        st.image(LOGO_PATH, use_container_width=True)
    else:
        st.warning("logo.png bulunamadı.")

    with st.expander("⚙️ Sistem araçları", expanded=False):
        if is_admin():
            if st.button(
                "🌐 API ile Fabrikayı Simüle Et",
                use_container_width=True
            ):
                if pull_api_simulation():
                    st.success("API verisi alındı ve SQLite güncellendi.")
                    st.rerun()
                else:
                    st.error(
                        "FastAPI çalışmıyor. "
                        "Önce api.py dosyasını başlat."
                    )

        if st.button(
            "🔄 Yerel Simülasyon",
            use_container_width=True
        ):
            simulation_result = local_simulate()
            st.success(
                "Simülasyon tamamlandı · "
                f"+{simulation_result['production']} adet üretim · "
                f"{simulation_result['status_changes']} durum değişimi · "
                f"{simulation_result['alarms']} yeni alarm · "
                f"{simulation_result['queued_dispatches']} iş emri kuyruktan atandı"
            )
            st.rerun()
        else:
            st.caption("Operatör modu: veriler salt okunur.")

        if api_is_alive():
            st.success("🟢 FastAPI aktif")
        else:
            st.warning("🟡 FastAPI kapalı")

        st.success("🟢 Neon PostgreSQL aktif" if USING_POSTGRES else "🟢 SQLite aktif")
        st.caption("Veritabanı: Neon PostgreSQL" if USING_POSTGRES else f"Veritabanı: {DB}")
        st.toggle(
            "Otomatik yenileme (30 sn)",
            key="auto_refresh_enabled",
            help="Açıkken dashboard her 30 saniyede bir güncellenir."
        )
        st.date_input(
            "Genel tarih aralığı",
            value=(date.today() - timedelta(days=14), date.today()),
            key="global_date_range",
            help="Zaman serisi, alarm ve üretim görünümlerinde kullanılacak tarih aralığı."
        )
    st.markdown("""
    <style>
    /* Akıllı Eşik formu açık renkli olduğundan genel beyaz sidebar yazı
       kuralını burada geçersiz kıl ve tüm alanlarda güçlü kontrast sağla. */
    section[data-testid="stSidebar"] div[data-testid="stForm"] {
        background:#f7fcf9 !important;border:1px solid #b9ddc7 !important;
        border-radius:12px !important;padding:14px !important;
    }
    section[data-testid="stSidebar"] div[data-testid="stForm"] label,
    section[data-testid="stSidebar"] div[data-testid="stForm"] label p,
    section[data-testid="stSidebar"] div[data-testid="stForm"] [data-testid="stWidgetLabel"] p {
        color:#123f2d !important;-webkit-text-fill-color:#123f2d !important;
        opacity:1 !important;font-weight:750 !important;
    }
    section[data-testid="stSidebar"] div[data-testid="stForm"] input {
        color:#123f2d !important;-webkit-text-fill-color:#123f2d !important;
        background:#edf8f1 !important;opacity:1 !important;
    }
    section[data-testid="stSidebar"] div[data-testid="stForm"] div[data-baseweb="select"] > div {
        background:#e8f5ed !important;border-color:#9dccaf !important;
    }
    section[data-testid="stSidebar"] div[data-testid="stForm"] div[data-baseweb="select"] span,
    section[data-testid="stSidebar"] div[data-testid="stForm"] div[data-baseweb="select"] input,
    section[data-testid="stSidebar"] div[data-testid="stForm"] div[data-baseweb="select"] svg {
        color:#123f2d !important;-webkit-text-fill-color:#123f2d !important;fill:#123f2d !important;
    }
    section[data-testid="stSidebar"] div[data-testid="stForm"] button[kind="primary"] {
        color:#fff !important;-webkit-text-fill-color:#fff !important;background:#129b50 !important;
    }
    </style>
    """, unsafe_allow_html=True)
    current_thresholds = get_sensor_thresholds()
    with st.expander("Akıllı Eşik Ayarları", expanded=False):
        st.caption("Bu değerler sensör durumunu, alarm üretimini ve Kayıp Avcısı önerilerini doğrudan etkiler.")
        if is_admin():
            with st.form("smart_threshold_form"):
                threshold_profile = st.selectbox("Profil", ["Özel", "Hassas", "Standart", "Esnek",], index=0)
                temp_warning = st.number_input("Sıcaklık uyarı (°C)", min_value=20.0, max_value=120.0, value=float(current_thresholds["temperature_warning"]), step=1.0)
                temp_critical = st.number_input("Sıcaklık kritik (°C)", min_value=20.0, max_value=130.0, value=float(current_thresholds["temperature_critical"]), step=1.0)
                vibration_warning = st.number_input("Titreşim uyarı (mm/s)", min_value=.1, max_value=20.0, value=float(current_thresholds["vibration_warning"]), step=.1)
                vibration_critical = st.number_input("Titreşim kritik (mm/s)", min_value=.1, max_value=25.0, value=float(current_thresholds["vibration_critical"]), step=.1)
                pressure_warning = st.number_input("Basınç uyarı alt sınırı (bar)", min_value=.1, max_value=15.0, value=float(current_thresholds["pressure_warning"]), step=.1)
                pressure_critical = st.number_input("Basınç kritik alt sınırı (bar)", min_value=.1, max_value=15.0, value=float(current_thresholds["pressure_critical"]), step=.1)
                save_thresholds = st.form_submit_button("Eşikleri Kaydet", type="primary", use_container_width=True)
            if save_thresholds:
                if threshold_profile != "Özel":
                    profiles = {
                        "Hassas": (70.0, 80.0, 3.5, 4.5, 5.0, 4.0),
                        "Standart": (75.0, 85.0, 4.0, 5.0, 4.5, 3.5),
                        "Esnek": (80.0, 95.0, 5.0, 7.0, 4.0, 3.0),
                    }
                    temp_warning, temp_critical, vibration_warning, vibration_critical, pressure_warning, pressure_critical = profiles[threshold_profile]
                if temp_warning >= temp_critical or vibration_warning >= vibration_critical or pressure_critical >= pressure_warning:
                    st.error("Uyarı ve kritik sınırlarının sıralamasını kontrol edin.")
                else:
                    new_thresholds = {
                        "temperature_warning": temp_warning, "temperature_critical": temp_critical,
                        "vibration_warning": vibration_warning, "vibration_critical": vibration_critical,
                        "pressure_warning": pressure_warning, "pressure_critical": pressure_critical,
                    }
                    save_sensor_thresholds(new_thresholds)
                    register_undo(
                        "Sensör eşikleri güncellendi",
                        [("UPDATE system_settings SET setting_value=?,updated_at=?,updated_by=? WHERE setting_key=?", (old_value, now(), st.session_state.get("username", "sistem"), setting_key)) for setting_key, old_value in current_thresholds.items()]
                    )
                    audit_event("Akıllı eşikleri güncelledi", threshold_profile, str(new_thresholds))
                    st.success(f"{threshold_profile} eşik profili uygulandı.")
                    st.rerun()
        else:
            st.caption(f'Sıcaklık: {current_thresholds["temperature_warning"]:.0f}/{current_thresholds["temperature_critical"]:.0f}°C')
            st.caption(f'Titreşim: {current_thresholds["vibration_warning"]:.1f}/{current_thresholds["vibration_critical"]:.1f} mm/s')
            st.caption(f'Basınç alt sınırı: {current_thresholds["pressure_warning"]:.1f}/{current_thresholds["pressure_critical"]:.1f} bar')
    st.divider()
    st.markdown(
        '<div style="color:#d9f5df;font-size:.72rem;font-weight:800;'
        'letter-spacing:.12em;margin:8px 0 4px;">OPERASYON MENÜSÜ</div>',
        unsafe_allow_html=True
    )

    module_groups = {
        "GENEL BAKIŞ": ["🏠 Ana Sayfa", "🏭 Makine", "📈 Üretim", "🧮 Manuel OEE", "👥 OLE"],
        "OPERASYON": ["🚨 Alarmlar", "📋 İş Emirleri", "📡 Sensörler", "⏱️ Duruşlar", "👷 Vardiya", "🧰 Bakım Talebi"],
        "KALİTE VE BAKIM": ["✅ Kalite", "🔧 Bakım", "📦 Stok"],
        "YÖNETİM": ["🌐 Dijital Olgunluk", "🧠 Akıllı Analiz", "🔎 Detay", "📺 Andon Ekranı", "🔳 QR Makine", "📜 Denetim Kaydı", "👥 Kullanıcı Yönetimi", "📊 Veritabanı"],
    }
    if "selected_module" not in st.session_state:
        st.session_state["selected_module"] = "🏠 Ana Sayfa"

    for group_name, modules in module_groups.items():
        modules = [module for module in modules if can_access_module(module)]
        if not modules:
            continue
        st.markdown(f'<div class="nav-group-title">{group_name}</div>', unsafe_allow_html=True)
        for module in modules:
            if st.button(
                NAV_LABELS.get(module, module),
                key=f"nav_{module}",
                type="primary" if st.session_state["selected_module"] == module else "secondary",
                use_container_width=True,
            ):
                st.session_state["selected_module"] = module
                st.rerun()

    selected_module = st.session_state["selected_module"]
    if not can_access_module(selected_module):
        st.session_state["selected_module"] = "🏠 Ana Sayfa"
        selected_module = "🏠 Ana Sayfa"
    st.markdown("""
    <div class="sidebar-nav-caption">
        <strong>trex MES</strong><br>
        Üretim, kalite ve bakım operasyonları tek menüden yönetilir.
    </div>
    """, unsafe_allow_html=True)
    st.divider()
    if st.button("Oturumu Kapat", use_container_width=True):
        for key in ["authenticated", "username", "role", "full_name"]:
            st.session_state.pop(key, None)
        st.rerun()

@st.fragment(run_every="30s" if st.session_state.get("auto_refresh_enabled", False) else None)
def refresh_heartbeat():
    if st.session_state.get("auto_refresh_enabled", False):
        previous = st.session_state.setdefault("last_refresh_tick", time.monotonic())
        if time.monotonic() - previous >= 29:
            st.session_state["last_refresh_tick"] = time.monotonic()
            st.rerun(scope="app")


refresh_heartbeat()

# Ana sayfadaki makine kartı bağlantısı, tarayıcıda yeni bir sayfa oluşturmadan
# doğrudan mevcut Makine Detayı modülüne geçer.
requested_machine_from_dashboard = st.query_params.get("machine", "")
if isinstance(requested_machine_from_dashboard, list):
    requested_machine_from_dashboard = requested_machine_from_dashboard[0] if requested_machine_from_dashboard else ""
if requested_machine_from_dashboard:
    st.session_state["detail_machine_v2"] = requested_machine_from_dashboard
    st.session_state["selected_module"] = "🔎 Detay"
    st.query_params.clear()
    st.rerun()


# =========================================================
# DASHBOARD DATA
# =========================================================

df = q("""
    SELECT *
    FROM machines
    ORDER BY machine_code
""")

oee_values = df.apply(
    oee,
    axis=1,
    result_type="expand"
 ) if not df.empty else pd.DataFrame(index=df.index, columns=range(4))

oee_values.columns = [
    "availability",
    "performance",
    "quality",
    "oee"
]

df = pd.concat([df, oee_values], axis=1)

# Sidebar vardiya bağlamı yalnızca görünümü ve vardiya bazlı analizleri seçer.
# Anlık makine sayaçları asla seçilen vardiyaya kopyalanmaz; böylece çalışmayan
# bir vardiyada üretim varmış gibi görünmez.
shift_context = st.session_state.get("shift_context", f"Aktif vardiya · {active_shift_name()}")
if shift_context.startswith("Aktif vardiya"):
    selected_shift_context = active_shift_name()
elif shift_context in SHIFT_SCHEDULE:
    selected_shift_context = shift_context
else:
    selected_shift_context = "Tümü"

# Sol menüdeki ortak tarih aralığını zaman serisi içeren ekranlara uygula.
selected_date_range = st.session_state.get(
    "global_date_range", (date.today() - timedelta(days=14), date.today())
)
if isinstance(selected_date_range, (tuple, list)) and len(selected_date_range) == 2:
    date_start, date_end = selected_date_range
else:
    date_start = date_end = selected_date_range

def filter_by_date_range(frame, column="timestamp"):
    """Veri çerçevesini seçilen tarih aralığına göre, kaydı bozmadan süzer."""
    if frame.empty or column not in frame.columns:
        return frame
    result = frame.copy()
    parsed = pd.to_datetime(result[column], errors="coerce")
    return result[(parsed.dt.date >= date_start) & (parsed.dt.date <= date_end)].copy()


def auto_date_range_filter(label, widget_key):
    """Her yeni gün başlangıç/bitişini bugüne alan tarih aralığı seçicisi."""
    anchor_key = f"{widget_key}_anchor"
    today_marker = date.today().isoformat()
    current_value = st.session_state.get(widget_key)
    if (
        st.session_state.get(anchor_key) != today_marker
        or not isinstance(current_value, (tuple, list))
        or len(current_value) != 2
    ):
        st.session_state[anchor_key] = today_marker
        st.session_state[widget_key] = (date.today(), date.today())
    return st.date_input(label, key=widget_key)


def date_range_bounds(selected_range):
    """Tek tıklamayla seçilen tarihi de güvenli biçimde başlangıç/bitişe çevirir."""
    if isinstance(selected_range, (tuple, list)) and len(selected_range) == 2:
        return selected_range[0], selected_range[1]
    return selected_range, selected_range


def labor_effectiveness_metrics(machine_frame):
    """Mevcut vardiya ve kayıtlı operatör kayıplarından tahmini OLE üretir.

    İşgücü Kullanılabilirliği yalnızca operatör/personel/mola kaynaklı kayıpları,
    Performans ideal çevrimde üretilmesi gereken süreyi ve Kalite sağlam ürünü kullanır.
    """
    columns = [
        "Operatör", "Vardiya", "Makine Sayısı", "Planlı Süre", "İşgücü Kaybı",
        "Üretim", "Sağlam Üretim", "Kullanılabilirlik", "Performans", "Kalite", "OLE", "Durum"
    ]
    if machine_frame.empty:
        return pd.DataFrame(columns=columns)

    downtime_rows = q("SELECT machine_code,reason,duration,event_at FROM downtime ORDER BY id DESC")
    if not downtime_rows.empty:
        dated_losses = downtime_rows[downtime_rows["event_at"].notna()].copy()
        undated_losses = downtime_rows[downtime_rows["event_at"].isna()].copy()
        dated_losses = filter_by_date_range(dated_losses, "event_at")
        downtime_rows = pd.concat([dated_losses, undated_losses], ignore_index=True)
        labor_reason = downtime_rows["reason"].fillna("").astype(str).str.lower()
        labor_losses = downtime_rows[labor_reason.str.contains("operatör|operator|personel|mola|işgücü|iscilik|işçilik", regex=True)].copy()
        labor_losses["duration"] = pd.to_numeric(labor_losses["duration"], errors="coerce").fillna(0).clip(lower=0)
        loss_by_machine = labor_losses.groupby("machine_code")["duration"].sum().to_dict()
    else:
        loss_by_machine = {}

    raw_rows = []
    for _, machine in machine_frame.iterrows():
        planned = max(float(machine.get("planned_time", 0) or 0), 0)
        labor_loss = min(float(loss_by_machine.get(machine["machine_code"], 0)), planned)
        production = max(int(machine.get("production", 0) or 0), 0)
        defective = min(max(int(machine.get("defective", 0) or 0), 0), production)
        raw_rows.append({
            "Operatör": str(machine.get("operator") or "Atanmamış"),
            "Vardiya": str(machine.get("shift") or active_shift_name()),
            "Makine": str(machine["machine_code"]),
            "Planlı Süre": planned,
            "İşgücü Kaybı": labor_loss,
            "İdeal Üretim Süresi": max(float(machine.get("ideal_cycle", 0) or 0), 0) * production,
            "Üretim": production,
            "Hatalı": defective,
        })
    raw = pd.DataFrame(raw_rows)
    grouped = raw.groupby(["Operatör", "Vardiya"], as_index=False).agg(
        **{
            "Makine Sayısı": ("Makine", "nunique"),
            "Planlı Süre": ("Planlı Süre", "sum"),
            "İşgücü Kaybı": ("İşgücü Kaybı", "sum"),
            "İdeal Üretim Süresi": ("İdeal Üretim Süresi", "sum"),
            "Üretim": ("Üretim", "sum"),
            "Hatalı": ("Hatalı", "sum"),
        }
    )
    productive_minutes = (grouped["Planlı Süre"] - grouped["İşgücü Kaybı"]).clip(lower=0)
    planned_denominator = grouped["Planlı Süre"].where(grouped["Planlı Süre"].ne(0))
    productive_denominator = productive_minutes.where(productive_minutes.ne(0))
    production_denominator = grouped["Üretim"].where(grouped["Üretim"].ne(0))
    grouped["Sağlam Üretim"] = (grouped["Üretim"] - grouped["Hatalı"]).clip(lower=0)
    grouped["Kullanılabilirlik"] = (productive_minutes / planned_denominator).fillna(0).clip(0, 1) * 100
    grouped["Performans"] = (grouped["İdeal Üretim Süresi"] / productive_denominator).fillna(0).clip(0, 1) * 100
    grouped["Kalite"] = (grouped["Sağlam Üretim"] / production_denominator).fillna(0).clip(0, 1) * 100
    grouped["OLE"] = grouped["Kullanılabilirlik"] * grouped["Performans"] * grouped["Kalite"] / 10000
    grouped["Durum"] = grouped["OLE"].map(lambda value: "İyi" if value >= 85 else ("Takip" if value >= 70 else ("Riskli" if value >= 60 else "Kritik")))
    return grouped[columns].sort_values("OLE", ascending=False).reset_index(drop=True)


def shift_production_totals():
    """Vardiya etiketli üretim anlık kayıtlarından gerçek parça artışını hesaplar."""
    history = q("""
        SELECT machine_code, quantity, timestamp, shift
        FROM production_history
        WHERE shift IS NOT NULL AND shift != ''
        ORDER BY machine_code, timestamp, id
    """)
    if history.empty:
        return pd.DataFrame(columns=["Makine", "Vardiya", "Üretim"])
    history["timestamp"] = pd.to_datetime(history["timestamp"], errors="coerce")
    history = history.dropna(subset=["timestamp", "shift"])
    history["Üretim Artışı"] = history.groupby("machine_code")["quantity"].diff().fillna(0).clip(lower=0)
    history = filter_by_date_range(history, "timestamp")
    return history.groupby(["machine_code", "shift"], as_index=False)["Üretim Artışı"].sum().rename(
        columns={"machine_code":"Makine", "shift":"Vardiya", "Üretim Artışı":"Üretim"}
    )


def reliability_metrics(machine_frame):
    """Gerçek arıza/duruş kayıtlarından MTBF ve MTTR hesaplar.

    MTBF = çalışma süresi / arıza sayısı; MTTR = arıza onarım süresi / arıza sayısı.
    Eski, tarihsiz kayıtlar yalnızca genel toplama katkı verir; yeni kayıtlar tarih filtresine uyar.
    """
    failures = q("""
        SELECT machine_code, duration, event_at
        FROM downtime
        WHERE reason LIKE '%Arıza%'
    """)
    if not failures.empty and "event_at" in failures.columns:
        dated = failures[failures["event_at"].notna()].copy()
        undated = failures[failures["event_at"].isna()].copy()
        dated = filter_by_date_range(dated, "event_at")
        failures = pd.concat([dated, undated], ignore_index=True)

    rows = []
    for _, machine in machine_frame.iterrows():
        events = failures[failures["machine_code"] == machine["machine_code"]] if not failures.empty else pd.DataFrame()
        failure_count = len(events)
        repair_minutes = float(events["duration"].fillna(0).sum()) if failure_count else 0.0
        operating_minutes = max(float(machine["planned_time"]) - float(machine["downtime"]), 0.0)
        rows.append({
            "Makine": machine["machine_code"],
            "Arıza Sayısı": failure_count,
            "Çalışma Süresi (dk)": round(operating_minutes, 1),
            "MTBF (dk)": round(operating_minutes / failure_count, 1) if failure_count else None,
            "MTTR (dk)": round(repair_minutes / failure_count, 1) if failure_count else None,
            "Durum": machine["status"],
        })
    return pd.DataFrame(rows)

running = int((df["status"] == "Çalışıyor").sum())
faulty = int((df["status"] == "Arızalı").sum())
waiting = int((df["status"] == "Beklemede").sum())

average_oee = df["oee"].mean() * 100
average_quality = df["quality"].mean() * 100

total_production = int(df["production"].sum())
total_target = int(df["target"].sum())

labor_metrics = pd.DataFrame()
overall_labor_availability = overall_labor_performance = overall_labor_quality = overall_ole = 0.0
total_labor_loss = 0.0
# İşgücü hesabı ayrıca duruş/atama sorguları çalıştırır. Sonuca yalnızca ana sayfa
# ve OLE modülü ihtiyaç duyduğu için diğer bütün sayfalarda bu maliyeti kaldırıyoruz.
if selected_module in ("🏠 Ana Sayfa", "👥 OLE"):
    labor_metrics = labor_effectiveness_metrics(df)
    if not labor_metrics.empty:
        labor_weights = labor_metrics["Planlı Süre"].clip(lower=0)
        labor_weight_total = max(float(labor_weights.sum()), 1.0)
        overall_labor_availability = float((labor_metrics["Kullanılabilirlik"] * labor_weights).sum() / labor_weight_total)
        overall_labor_performance = float((labor_metrics["Performans"] * labor_weights).sum() / labor_weight_total)
        total_good_labor_output = float(labor_metrics["Sağlam Üretim"].sum())
        total_labor_output = float(labor_metrics["Üretim"].sum())
        overall_labor_quality = total_good_labor_output / max(total_labor_output, 1) * 100
        overall_ole = overall_labor_availability * overall_labor_performance * overall_labor_quality / 10000
        total_labor_loss = float(labor_metrics["İşgücü Kaybı"].sum())


production_gap = total_production - total_target
oee_target = 85.0
quality_target = 98.0
open_alarm_count = int(q("SELECT COUNT(*) AS n FROM alarms WHERE acknowledged=0").iloc[0]["n"])

# =========================================================
# ANA SAYFA DASHBOARD
# =========================================================
if selected_module == "🏠 Ana Sayfa":
    # Referans tasarımla aynı bilgi yoğunluğunda, üç sütunlu kompakt kontrol merkezi.
    st.markdown("""
    <style>
    .mesv3-kpi{height:52px;border:1px solid #d6ece0;border-radius:7px;background:#fff;box-shadow:0 2px 8px rgba(6,81,43,.05);padding:6px 9px;display:flex;gap:7px;align-items:center}.mesv3-kpi-icon{width:24px;height:24px;border-radius:50%;display:flex;align-items:center;justify-content:center;background:#087847;color:#fff;font-size:.72rem;flex:0 0 24px}.mesv3-kpi-title{font-size:.54rem;font-weight:850;color:#315f4a;text-transform:uppercase}.mesv3-kpi-value{font-size:1rem;font-weight:900;color:#103f2d;margin-top:1px}.mesv3-kpi-note{font-size:.51rem;color:#10a65e;margin-left:3px}.mesv3-box{background:#fff;border:1px solid #d8ede2;border-radius:7px;padding:9px 10px;box-shadow:0 2px 8px rgba(6,81,43,.045);box-sizing:border-box}.mesv3-box-title{font-size:.72rem;font-weight:900;color:#154f38;border-bottom:1px solid #e9f4ed;padding-bottom:6px;margin-bottom:7px}.mesv3-box-title small{float:right;color:#5d8b73;font-weight:700;font-size:.57rem}.mesv3-machine{border:1px solid #dcefe4;border-radius:6px;background:linear-gradient(135deg,#fff,#f4fcf7);padding:7px;min-height:112px}.mesv3-machine-head{display:flex;justify-content:space-between;align-items:center;color:#174d37;font-size:.66rem;font-weight:900}.mesv3-state{font-size:.52rem;border-radius:8px;padding:3px 4px;font-weight:800}.mesv3-running{background:#ddf7e6;color:#07843e}.mesv3-wait{background:#fff1d2;color:#a16600}.mesv3-fault{background:#ffe1e1;color:#b72828}.mesv3-machine-body{display:grid;grid-template-columns:42px 1fr;gap:5px;align-items:center;margin:8px 0}.mesv3-ring{width:38px;height:38px;border-radius:50%;background:conic-gradient(var(--c) calc(var(--v)*1%),#e7f0ea 0);position:relative}.mesv3-ring:after{content:"";position:absolute;inset:5px;background:#fff;border-radius:50%}.mesv3-ring b{position:absolute;z-index:2;inset:12px 0 0;text-align:center;font-size:.47rem;color:#184735}.mesv3-lines{font-size:.52rem;color:#5d796b;line-height:1.6}.mesv3-lines b{float:right;color:#1f513a}.mesv3-bar{height:5px;border-radius:5px;background:#dceee3;overflow:hidden;margin-top:4px}.mesv3-bar i{display:block;height:100%;border-radius:5px;background:#13aa60}.mesv3-foot{font-size:.5rem;color:#527666;border-top:1px solid #edf6f0;padding-top:5px}.mesv3-action{display:flex;align-items:flex-start;gap:7px;padding:8px 1px;border-bottom:1px solid #eaf4ee}.mesv3-action:last-child{border-bottom:0}.mesv3-dot{width:9px;height:9px;border-radius:50%;margin-top:2px;flex:0 0 9px}.mesv3-action-title{font-size:.6rem;font-weight:850;color:#1e513a}.mesv3-action-sub{font-size:.55rem;color:#638273;margin-top:2px}.mesv3-action-time{font-size:.54rem;color:#718c7f;margin-left:auto;white-space:nowrap}.mesv3-table{width:100%;border-collapse:collapse;font-size:.55rem;color:#315b48}.mesv3-table th{text-align:left;padding:5px 4px;color:#668475;border-bottom:1px solid #dceee3;font-size:.5rem}.mesv3-table td{padding:6px 4px;border-bottom:1px solid #edf5f0}.mesv3-pill{font-size:.49rem;font-weight:800;padding:3px 5px;border-radius:8px;white-space:nowrap}.mesv3-progress{height:5px;background:#e1efe6;border-radius:6px;overflow:hidden;min-width:38px}.mesv3-progress i{display:block;height:100%;background:#12a85d;border-radius:6px}.st-key-v3_quick_alarm button,.st-key-v3_quick_order button,.st-key-v3_quick_maintenance button,.st-key-v3_quick_report button{height:31px!important;min-height:31px!important;padding:4px 7px!important;font-size:.61rem!important;border-radius:5px!important;text-align:left!important;background:#fff!important;color:#135139!important;border-color:#d5ecdf!important}.st-key-v3_quick_alarm button{background:#087a47!important;color:#fff!important}.st-key-v3_quick_order button{background:#0b9154!important;color:#fff!important}.st-key-v3_quick_maintenance button{background:#0b7b4a!important;color:#fff!important}.mesv3-chart div[data-testid="stPlotlyChart"]{border:0!important;box-shadow:none!important;padding:0!important;background:transparent!important}
    /* Ana sayfa üst şeridi ve KPI yerleşimi: boşluğu azalt, kartlara nefes ver. */
    .trex-home-topbar{padding:0 4px 8px!important;min-height:44px;gap:18px}
    .trex-home-topbar .trex-kicker{margin-top:0!important;font-size:.75rem}
    .trex-home-topbar .trex-hero-title{font-size:1.08rem}
    .trex-home-meta{margin-left:auto;display:flex;align-items:center;gap:14px;color:#527463;font-size:.68rem;white-space:nowrap}
    .trex-home-meta b{color:#15563a}
    .mesv3-kpi{height:58px!important;margin-bottom:8px!important;border-radius:9px!important;padding:7px 11px!important;gap:9px!important;box-sizing:border-box}
    .mesv3-kpi-icon{width:27px!important;height:27px!important;flex-basis:27px!important;font-size:.76rem!important}
    .mesv3-kpi-value{font-size:1.04rem!important}
    @media(max-width:900px){.trex-home-meta{display:none}.mesv3-kpi{height:auto!important;min-height:58px}}
    </style>
    """, unsafe_allow_html=True)
    st.markdown("""<style>
    .mesv3-machine-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:7px}
    .mesv3-machine-link{display:block;text-decoration:none!important;color:inherit!important;cursor:pointer}.mesv3-machine-link:hover .mesv3-machine{border-color:#10a75d;box-shadow:0 5px 14px rgba(4,120,65,.16);transform:translateY(-2px)}
    .mesv3-detail-close{float:right;text-decoration:none!important;color:#5b8270;font-size:1.1rem;line-height:1}.mesv3-detail-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px}.mesv3-detail-item{border:1px solid #e1f0e6;border-radius:5px;padding:6px;font-size:.56rem;color:#6a8779}.mesv3-detail-item b{display:block;margin-top:2px;color:#194c37;font-size:.63rem}
    div[class*="st-key-v3_machine_button_"] button{min-height:148px!important;height:148px!important;white-space:pre-line!important;text-align:left!important;line-height:1.48!important;padding:12px 13px!important;border:1px solid #d8ebe1!important;border-left:4px solid var(--machine-accent,#12a35d)!important;border-radius:12px!important;background:linear-gradient(145deg,#fff,#f3fbf7)!important;color:#315b49!important;font-size:.61rem!important;font-weight:650!important;box-shadow:0 4px 12px rgba(6,81,43,.075)!important;align-items:flex-start!important;justify-content:flex-start!important;overflow:hidden!important;transition:transform .18s ease,box-shadow .18s ease,border-color .18s ease!important}
    div[class*="st-key-v3_machine_button_"] button p{display:block!important;width:100%!important;margin:0!important;white-space:pre-line!important;overflow:visible!important;text-overflow:clip!important;text-align:left!important;line-height:1.48!important;word-break:normal!important;overflow-wrap:anywhere!important;font-size:.61rem!important}
    div[class*="st-key-v3_machine_button_"] button p strong{color:#083e2a!important;font-size:.78rem!important;letter-spacing:.01em!important}
    div[class*="st-key-v3_machine_button_"] button:hover{border-color:var(--machine-accent,#10a75d)!important;box-shadow:0 8px 18px rgba(4,120,65,.16)!important;transform:translateY(-3px)}
    @media(max-width:900px){.mesv3-machine-grid{grid-template-columns:1fr}}
    </style>""", unsafe_allow_html=True)
    live_alarms_v3 = q("SELECT machine_code,alarm,level,time FROM alarms WHERE acknowledged=0 ORDER BY id DESC LIMIT 5")
    maintenance_v3 = q("SELECT machine_code,maintenance_type,next_date,status FROM maintenance WHERE status != 'Tamamlandı' ORDER BY next_date LIMIT 4")
    work_orders_v3 = q("SELECT order_no,machine_code,product,target,produced,priority,status,due_date FROM work_orders ORDER BY id DESC LIMIT 5")
    stock_critical_v3 = int(q("SELECT COUNT(*) AS total FROM products WHERE stock < min_stock").iloc[0]["total"])
    target_percent_v3 = total_production / max(total_target, 1) * 100
    selected_machine_v3 = st.query_params.get("machine", "")
    if isinstance(selected_machine_v3, list):
        selected_machine_v3 = selected_machine_v3[0] if selected_machine_v3 else ""
    if selected_machine_v3 not in df["machine_code"].tolist():
        selected_machine_v3 = ""
    recent_history_v3 = q("SELECT quantity,timestamp FROM production_history ORDER BY id DESC LIMIT 300")
    if recent_history_v3.empty:
        trend_v3 = pd.DataFrame({"Saat":["00:00", "12:00", "24:00"], "Üretim":[0, total_production * .6, total_production]})
    else:
        recent_history_v3["timestamp"] = pd.to_datetime(recent_history_v3["timestamp"], errors="coerce")
        recent_history_v3 = recent_history_v3.dropna(subset=["timestamp"])
        trend_v3 = recent_history_v3.groupby(recent_history_v3["timestamp"].dt.strftime("%H:%M"), as_index=False)["quantity"].max().tail(14).rename(columns={"timestamp":"Saat", "quantity":"Üretim"})
        if trend_v3.empty:
            trend_v3 = pd.DataFrame({"Saat":["Şimdi"], "Üretim":[total_production]})
    downtime_v3 = q("SELECT reason,duration FROM downtime ORDER BY id DESC")
    if downtime_v3.empty:
        downtime_summary_v3 = pd.DataFrame({"Neden":["Arıza", "Malzeme", "Bakım", "Operatör"], "Süre":[0,0,0,0]})
    else:
        downtime_summary_v3 = downtime_v3.groupby("reason", as_index=False)["duration"].sum().rename(columns={"reason":"Neden", "duration":"Süre"}).sort_values("Süre", ascending=False).head(5)
    shift_totals_v3 = shift_production_totals()

    v3_main, v3_side = st.columns([4.15, 1.25], gap="small")
    with v3_main:
        kpi_values_v3 = [
            ("▣", "Toplam Üretim", f"{total_production:,}", f"%{target_percent_v3:.1f}"),
            ("◎", "Hedef", f"{total_target:,}", f"%{target_percent_v3:.1f}"),
            ("◉", "OEE", f"%{average_oee:.1f}", "Canlı"),
            ("◆", "Kalite", f"%{average_quality:.1f}", "Uygun üretim"),
            ("◌", "Kullanılabilirlik", f"%{float(df['availability'].mean() * 100):.1f}", "Çalışma oranı"),
            ("◈", "Performans", f"%{float(df['performance'].mean() * 100):.1f}", "Çevrim performansı"),
            ("👥", "İşgücü OLE", f"%{overall_ole:.1f}", "Tahmini"),
        ]
        for kpi_row_v3 in (kpi_values_v3[:3], kpi_values_v3[3:]):
            top_kpis_v3 = st.columns(len(kpi_row_v3), gap="small")
            for column, (icon, label, value, note) in zip(top_kpis_v3, kpi_row_v3):
                with column:
                    st.markdown(f"<div class='mesv3-kpi'><span class='mesv3-kpi-icon'>{icon}</span><div><div class='mesv3-kpi-title'>{label}</div><span class='mesv3-kpi-value'>{value}</span><span class='mesv3-kpi-note'>▲ {note}</span></div></div>", unsafe_allow_html=True)

        # Ana sayfada olgunluğu yeniden hesaplamak yaklaşık 30 sorgu üretiyordu.
        # Burada son iki kayıt yeterli; ayrıntılı hesap yalnızca kendi modülünde yapılır.
        from maturity_panel import maturity_level
        maturity_history_v3 = q("SELECT assessment_date,overall_score FROM digital_maturity_scores ORDER BY assessment_date DESC,id DESC LIMIT 2")
        if maturity_history_v3.empty:
            maturity_score_v3, maturity_previous_v3 = 0.0, 0.0
            maturity_date_v3 = "Değerlendirme bekliyor"
        else:
            maturity_score_v3 = float(maturity_history_v3.iloc[0]["overall_score"] or 0)
            maturity_previous_v3 = float(maturity_history_v3.iloc[1]["overall_score"] or 0) if len(maturity_history_v3) > 1 else maturity_score_v3
            maturity_date_value_v3 = pd.to_datetime(maturity_history_v3.iloc[0]["assessment_date"], errors="coerce")
            maturity_date_v3 = maturity_date_value_v3.strftime("%d.%m.%Y") if pd.notna(maturity_date_value_v3) else "Kayıtlı değerlendirme"
        maturity_level_no_v3, maturity_level_name_v3, maturity_colour_v3 = maturity_level(maturity_score_v3)
        maturity_change_v3 = maturity_score_v3 - maturity_previous_v3
        st.markdown(f'''<div style="margin:7px 0 9px;border:1px solid #cfe8d9;border-left:5px solid {maturity_colour_v3};border-radius:9px;background:linear-gradient(120deg,#fff,#effaf4);padding:10px 14px;display:grid;grid-template-columns:190px 1fr 170px;gap:15px;align-items:center"><div><small style="font-size:.58rem;color:#668376;font-weight:800">DİJİTAL FABRİKA OLGUNLUĞU</small><div style="font-size:1.45rem;font-weight:950;color:#0b5238">{maturity_score_v3:.1f} / 100</div></div><div><b style="font-size:.75rem;color:#174b36">Seviye {maturity_level_no_v3} · {maturity_level_name_v3}</b><div style="height:7px;background:#dceee3;border-radius:9px;margin-top:6px;overflow:hidden"><i style="display:block;height:100%;width:{maturity_score_v3}%;background:{maturity_colour_v3}"></i></div></div><div style="font-size:.63rem;color:#618071">Son değerlendirme<br><b>{maturity_date_v3}</b><br>Değişim {maturity_change_v3:+.1f}</div></div>''', unsafe_allow_html=True)
        if st.button("Dijital olgunluk detaylarını aç", key="home_open_maturity", use_container_width=True):
            st.session_state["selected_module"] = "🌐 Dijital Olgunluk"
            st.rerun()

        machine_area_v3, trend_area_v3 = st.columns([1.36, 1], gap="small")
        with machine_area_v3:
            with st.container(border=True):
                st.markdown("<div class='mesv3-box-title'>Makine Durumları <small>Tüm Makineler</small></div>", unsafe_allow_html=True)
                machine_button_columns_v3 = st.columns(max(len(df), 1), gap="small")
                for column, (_, machine) in zip(machine_button_columns_v3, df.sort_values("machine_code").iterrows()):
                    with column:
                        machine_oee_v3 = max(0, min(float(machine["oee"]) * 100, 100))
                        machine_progress_v3 = min(int(machine["production"]) / max(int(machine["target"]), 1) * 100, 100)
                        machine_status_v3 = str(machine["status"])
                        machine_status_icon_v3 = "🟢" if machine_status_v3 == "Çalışıyor" else ("🔴" if machine_status_v3 == "Arızalı" else "🟡")
                        machine_card_tint_v3 = "#eefaf3" if machine_status_v3 == "Çalışıyor" else ("#fff0f0" if machine_status_v3 == "Arızalı" else "#fff8e8")
                        machine_card_border_v3 = "#bfe7d0" if machine_status_v3 == "Çalışıyor" else ("#f1c2c2" if machine_status_v3 == "Arızalı" else "#efdcae")
                        machine_accent_v3 = "#15a663" if machine_status_v3 == "Çalışıyor" else ("#e05258" if machine_status_v3 == "Arızalı" else "#eeb02e")
                        progress_filled_v3 = min(max(round(machine_progress_v3 / 10), 0), 10)
                        progress_bar_v3 = "●" * progress_filled_v3 + "○" * (10 - progress_filled_v3)
                        st.markdown(
                            f'<style>div[class*="st-key-v3_machine_button_{machine["machine_code"]}"] button{{background:linear-gradient(145deg,#fff,{machine_card_tint_v3})!important;border-color:{machine_card_border_v3}!important;--machine-accent:{machine_accent_v3}}}</style>',
                            unsafe_allow_html=True,
                        )
                        machine_label_v3 = (
                            f"**⚙ {machine['machine_code']}**\n"
                            f"{machine_status_icon_v3} **{machine_status_v3.upper()}**\n"
                            f"📦 {machine['product']}\n"
                            f"**{int(machine['production']):,} / {int(machine['target']):,}** adet · %{machine_progress_v3:.0f}\n"
                            f"{progress_bar_v3}\n"
                            f"OEE **%{machine_oee_v3:.1f}**  ·  👤 {machine['operator']}"
                        )
                        if st.button(
                            machine_label_v3,
                            key=f"v3_machine_button_{machine['machine_code']}",
                            use_container_width=True,
                            help=f"{machine['machine_code']} makine detayını aç",
                        ):
                            st.session_state["detail_machine_v2"] = machine["machine_code"]
                            st.session_state["selected_module"] = "🔎 Detay"
                            st.rerun()
        with trend_area_v3:
            production_chart_v3 = px.line(trend_v3, x="Saat", y="Üretim", markers=True, title="Üretim Trendi · Son 24 Saat", color_discrete_sequence=["#10a95e"])
            production_chart_v3.update_layout(height=170, margin=dict(l=4,r=4,t=34,b=2), xaxis_title=None, yaxis_title=None, showlegend=False, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
            st.markdown("<div class='mesv3-box mesv3-chart'>", unsafe_allow_html=True); st.plotly_chart(production_chart_v3, use_container_width=True, config={"displayModeBar":False}, key="v3_production_trend"); st.markdown("</div>", unsafe_allow_html=True)

        alarm_area_v3, downtime_area_v3, shift_area_v3 = st.columns([1.1, .96, .98], gap="small")
        with alarm_area_v3:
            alarm_rows_v3 = "".join(f"<tr><td><span class='mesv3-pill' style='background:{'#ffe1e1' if item['level']=='Kritik' else '#fff0d1'};color:{'#b72727' if item['level']=='Kritik' else '#a66700'}'>{item['level']}</span></td><td><b>{item['machine_code']}</b></td><td>{item['alarm']}</td><td>{str(item['time'])[-8:-3]}</td></tr>" for _, item in live_alarms_v3.iterrows()) or "<tr><td colspan='4'>Açık alarm bulunmuyor.</td></tr>"
            st.markdown(f"<div class='mesv3-box'><div class='mesv3-box-title'>Aktif Alarmlar <small>{len(live_alarms_v3)} kayıt</small></div><table class='mesv3-table'><thead><tr><th>Seviye</th><th>Makine</th><th>Açıklama</th><th>Saat</th></tr></thead><tbody>{alarm_rows_v3}</tbody></table></div>", unsafe_allow_html=True)
        with downtime_area_v3:
            donut_v3 = px.pie(downtime_summary_v3, values="Süre", names="Neden", hole=.62, title=f"Duruş Nedenleri · {float(downtime_summary_v3['Süre'].sum()):.0f} dk", color_discrete_sequence=["#ef5656", "#ffb71d", "#11a961", "#4eaeec", "#8b65d7"])
            donut_v3.update_layout(height=185, margin=dict(l=3,r=3,t=34,b=0), legend=dict(font_size=8), paper_bgcolor="rgba(0,0,0,0)")
            st.markdown("<div class='mesv3-box mesv3-chart'>", unsafe_allow_html=True); st.plotly_chart(donut_v3, use_container_width=True, config={"displayModeBar":False}, key="v3_downtime_donut"); st.markdown("</div>", unsafe_allow_html=True)
        with shift_area_v3:
            shift_rows_v3 = []
            for shift_v3 in ["Sabah", "Akşam", "Gece"]:
                amount_v3 = float(shift_totals_v3[shift_totals_v3["Vardiya"] == shift_v3]["Üretim"].sum()) if not shift_totals_v3.empty else 0
                shift_target_v3 = max(total_target / 3, 1); shift_percent_v3 = min(amount_v3 / shift_target_v3 * 100, 100)
                shift_rows_v3.append(f"<div class='mesv3-action'><i class='mesv3-dot' style='background:#0da35a'></i><div style='flex:1'><div class='mesv3-action-title'>{shift_v3}<span style='float:right'>%{shift_percent_v3:.0f}</span></div><div class='mesv3-bar'><i style='width:{shift_percent_v3:.1f}%'></i></div></div></div>")
            st.markdown(f"<div class='mesv3-box'><div class='mesv3-box-title'>Vardiya Performansı <small>Tüm vardiyalar</small></div>{''.join(shift_rows_v3)}</div>", unsafe_allow_html=True)

        order_area_v3, maintenance_area_v3, summary_area_v3 = st.columns([1.3, 1.05, .84], gap="small")
        with order_area_v3:
            order_rows_v3 = "".join(f"<tr><td><b>{item['order_no']}</b></td><td>{item['product']}</td><td>{item['machine_code'] or 'Kuyruk'}</td><td>{int(item['target']):,}</td><td>{int(item['produced']):,}</td><td><div class='mesv3-progress'><i style='width:{min(int(item['produced'])/max(int(item['target']),1)*100,100):.1f}%'></i></div></td></tr>" for _, item in work_orders_v3.iterrows()) or "<tr><td colspan='6'>İş emri bulunmuyor.</td></tr>"
            st.markdown(f"<div class='mesv3-box'><div class='mesv3-box-title'>Son İş Emirleri <small>Tüm iş emirleri</small></div><table class='mesv3-table'><thead><tr><th>İş Emri</th><th>Ürün</th><th>Makine</th><th>Hedef</th><th>Üretim</th><th>Durum</th></tr></thead><tbody>{order_rows_v3}</tbody></table></div>", unsafe_allow_html=True)
        with maintenance_area_v3:
            maintenance_rows_v3 = "".join(f"<div class='mesv3-action'><i class='mesv3-dot' style='background:#f6b51c'></i><div><div class='mesv3-action-title'>{item['machine_code']} · {item['maintenance_type']}</div><div class='mesv3-action-sub'>Tarih: {item['next_date']} · {item['status']}</div></div></div>" for _, item in maintenance_v3.iterrows()) or "<div class='mesv3-action-sub'>Yaklaşan bakım bulunmuyor.</div>"
            st.markdown(f"<div class='mesv3-box'><div class='mesv3-box-title'>Yaklaşan Bakımlar <small>Tüm bakımlar</small></div>{maintenance_rows_v3}</div>", unsafe_allow_html=True)
        with summary_area_v3:
            st.markdown(f"<div class='mesv3-box'><div class='mesv3-box-title'>Sistem Özeti</div><div class='mesv3-action'><i class='mesv3-dot' style='background:#12a95e'></i><div><div class='mesv3-action-title'>Günlük üretim</div><div class='mesv3-action-sub'>{total_production:,} / {total_target:,} · %{target_percent_v3:.1f}</div></div></div><div class='mesv3-action'><i class='mesv3-dot' style='background:#0ba35b'></i><div><div class='mesv3-action-title'>Aktif makine</div><div class='mesv3-action-sub'>{running} / {len(df)} makine</div></div></div><div class='mesv3-action'><i class='mesv3-dot' style='background:#f7b81d'></i><div><div class='mesv3-action-title'>Toplam duruş</div><div class='mesv3-action-sub'>{float(downtime_summary_v3['Süre'].sum()):.0f} dk</div></div></div><div class='mesv3-action'><i class='mesv3-dot' style='background:#ef5353'></i><div><div class='mesv3-action-title'>Toplam alarm</div><div class='mesv3-action-sub'>{len(live_alarms_v3)} açık kayıt</div></div></div></div>", unsafe_allow_html=True)
    with v3_side:
        if selected_machine_v3:
            machine_detail_v3 = df[df["machine_code"] == selected_machine_v3].iloc[0]
            machine_order_v3 = q("SELECT order_no,product,target,produced,priority,status,due_date FROM work_orders WHERE machine_code=? AND status != 'Tamamlandı' ORDER BY id DESC LIMIT 1", (selected_machine_v3,))
            machine_sensor_v3 = q("SELECT temperature,vibration,pressure,rpm,timestamp FROM sensors WHERE machine_code=?", (selected_machine_v3,))
            machine_history_v3 = q("SELECT quantity,timestamp FROM production_history WHERE machine_code=? ORDER BY id DESC LIMIT 80", (selected_machine_v3,))
            machine_energy_v3 = q("SELECT power_kw,energy_kwh,timestamp FROM energy_readings WHERE machine_code=? ORDER BY id DESC LIMIT 1", (selected_machine_v3,))
            machine_maintenance_v3 = q("SELECT maintenance_type,next_date,status FROM maintenance WHERE machine_code=? ORDER BY next_date LIMIT 1", (selected_machine_v3,))
            detail_status_v3 = str(machine_detail_v3["status"])
            detail_badge_v3 = "mesv3-running" if detail_status_v3 == "Çalışıyor" else ("mesv3-wait" if detail_status_v3 == "Beklemede" else "mesv3-fault")
            detail_oee_v3 = max(0, min(float(machine_detail_v3["oee"]) * 100, 100))
            st.markdown(f"<div class='mesv3-box'><div class='mesv3-box-title'>⚙ {selected_machine_v3} · Makine Detayı <a class='mesv3-detail-close' href='?' title='Detayı kapat'>×</a></div><div style='display:flex;align-items:center;justify-content:space-between;margin-bottom:8px'><span class='mesv3-state {detail_badge_v3}'>● {detail_status_v3}</span><b style='font-size:1.2rem;color:#0b7948'>OEE %{detail_oee_v3:.1f}</b></div><div class='mesv3-detail-grid'><div class='mesv3-detail-item'>Operatör<b>{machine_detail_v3['operator']}</b></div><div class='mesv3-detail-item'>Vardiya<b>{machine_detail_v3['shift']}</b></div><div class='mesv3-detail-item'>Ürün<b>{machine_detail_v3['product']}</b></div><div class='mesv3-detail-item'>Üretim<b>{int(machine_detail_v3['production']):,} / {int(machine_detail_v3['target']):,}</b></div><div class='mesv3-detail-item'>Son Bakım<b>{machine_detail_v3['last_maintenance']}</b></div><div class='mesv3-detail-item'>Sonraki Bakım<b>{machine_detail_v3['next_maintenance']}</b></div></div></div>", unsafe_allow_html=True)
            general_tab_v3, production_tab_v3, sensor_tab_v3, service_tab_v3 = st.tabs(["Genel", "Üretim", "Sensör", "Bakım"])
            with general_tab_v3:
                general_metrics_v3 = st.columns(3)
                general_metrics_v3[0].metric("Kullanılabilirlik", f"%{float(machine_detail_v3['availability']) * 100:.1f}")
                general_metrics_v3[1].metric("Performans", f"%{float(machine_detail_v3['performance']) * 100:.1f}")
                general_metrics_v3[2].metric("Kalite", f"%{float(machine_detail_v3['quality']) * 100:.1f}")
                if machine_order_v3.empty:
                    st.caption("Aktif iş emri bulunmuyor.")
                else:
                    order_detail_v3 = machine_order_v3.iloc[0]
                    st.info(f"Aktif iş emri: {order_detail_v3['order_no']} · {order_detail_v3['product']} · {int(order_detail_v3['produced']):,}/{int(order_detail_v3['target']):,}")
            with production_tab_v3:
                if machine_history_v3.empty:
                    st.caption("Üretim trendi için simülasyon veya API kaydı bekleniyor.")
                else:
                    machine_history_v3["timestamp"] = pd.to_datetime(machine_history_v3["timestamp"], errors="coerce")
                    machine_history_v3 = machine_history_v3.dropna(subset=["timestamp"]).sort_values("timestamp")
                    machine_history_v3["Artış"] = machine_history_v3["quantity"].diff().fillna(0).clip(lower=0)
                    detail_production_chart_v3 = px.bar(machine_history_v3, x="timestamp", y="Artış", color_discrete_sequence=["#11a85d"], title="Son Üretim Kayıtları")
                    detail_production_chart_v3.update_layout(height=230, margin=dict(l=5,r=5,t=36,b=4), xaxis_title=None, yaxis_title=None, showlegend=False)
                    st.plotly_chart(detail_production_chart_v3, use_container_width=True, config={"displayModeBar":False}, key=f"side_production_{selected_machine_v3}")
            with sensor_tab_v3:
                if machine_sensor_v3.empty:
                    st.caption("Anlık sensör kaydı bulunmuyor.")
                else:
                    sensor_detail_v3 = machine_sensor_v3.iloc[0]
                    sensor_metrics_v3 = st.columns(2)
                    sensor_metrics_v3[0].metric("Sıcaklık", f"{float(sensor_detail_v3['temperature']):.1f} °C")
                    sensor_metrics_v3[1].metric("Titreşim", f"{float(sensor_detail_v3['vibration']):.2f} mm/s")
                    sensor_metrics_v3[0].metric("Basınç", f"{float(sensor_detail_v3['pressure']):.1f} bar")
                    sensor_metrics_v3[1].metric("RPM", int(sensor_detail_v3["rpm"]))
                    if not machine_energy_v3.empty:
                        st.caption(f"Anlık enerji: {float(machine_energy_v3.iloc[0]['power_kw']):.2f} kW · Son ölçüm: {float(machine_energy_v3.iloc[0]['energy_kwh']):.2f} kWh")
            with service_tab_v3:
                if machine_maintenance_v3.empty:
                    st.caption("Planlı bakım kaydı bulunmuyor.")
                else:
                    service_detail_v3 = machine_maintenance_v3.iloc[0]
                    st.info(f"{service_detail_v3['maintenance_type']} · {service_detail_v3['next_date']} · {service_detail_v3['status']}")
                if st.button("Bakım talebi oluştur", use_container_width=True, key=f"side_maintenance_{selected_machine_v3}"):
                    st.session_state["selected_module"] = "🧰 Bakım Talebi"; st.rerun()
                if st.button("Tam detay ekranını aç", use_container_width=True, key=f"side_full_detail_{selected_machine_v3}"):
                    st.session_state["detail_machine_v2"] = selected_machine_v3
                    st.session_state["selected_module"] = "🔎 Detay"; st.rerun()
            st.stop()
        side_actions_v3 = []
        for _, item in live_alarms_v3.head(2).iterrows():
            color_v3 = "#ef4f4f" if item["level"] == "Kritik" else "#f5b51c"
            side_actions_v3.append(f"<div class='mesv3-action'><i class='mesv3-dot' style='background:{color_v3}'></i><div><div class='mesv3-action-title'>{item['level']} Alarm</div><div class='mesv3-action-sub'>{item['machine_code']} · {item['alarm']}</div></div><span class='mesv3-action-time'>{str(item['time'])[-8:-3]}</span></div>")
        for _, item in maintenance_v3.head(1).iterrows():
            side_actions_v3.append(f"<div class='mesv3-action'><i class='mesv3-dot' style='background:#f5b51c'></i><div><div class='mesv3-action-title'>Bakım Riski</div><div class='mesv3-action-sub'>{item['machine_code']} · {item['maintenance_type']}</div></div></div>")
        if target_percent_v3 < 100:
            side_actions_v3.append(f"<div class='mesv3-action'><i class='mesv3-dot' style='background:#f5b51c'></i><div><div class='mesv3-action-title'>Üretim Hedef Riski</div><div class='mesv3-action-sub'>Gerçekleşme %{target_percent_v3:.1f}</div></div></div>")
        if stock_critical_v3:
            side_actions_v3.append(f"<div class='mesv3-action'><i class='mesv3-dot' style='background:#ef4f4f'></i><div><div class='mesv3-action-title'>Kritik Stok</div><div class='mesv3-action-sub'>{stock_critical_v3} ürün minimum seviyenin altında</div></div></div>")
        st.markdown(f"<div class='mesv3-box'><div class='mesv3-box-title'>Aksiyon Merkezi <small>Canlı</small></div>{''.join(side_actions_v3) or '<div class=mesv3-action-sub>Öncelikli aksiyon bulunmuyor.</div>'}</div>", unsafe_allow_html=True)
        with st.container(border=True):
            st.markdown("<div class='mesv3-box-title'>Hızlı İşlemler</div>", unsafe_allow_html=True)
            if st.button("➕ Yeni Alarm Kaydı", use_container_width=True, key="v3_quick_alarm"):
                st.session_state["selected_module"] = "🚨 Alarmlar"; st.rerun()
            if st.button("📋 İş Emirleri", use_container_width=True, key="v3_quick_order"):
                st.session_state["selected_module"] = "📋 İş Emirleri"; st.rerun()
            if st.button("🧰 Bakım Talebi Oluştur", use_container_width=True, key="v3_quick_maintenance"):
                st.session_state["selected_module"] = "🧰 Bakım Talebi"; st.rerun()
            if st.button("▣ Rapor Al", use_container_width=True, key="v3_quick_report"):
                st.session_state["selected_module"] = "📄 Raporlar"; st.rerun()
        st.markdown(f"<div class='mesv3-box' style='margin-top:9px'><div class='mesv3-box-title'>Sistem Çevrimiçi</div><div class='mesv3-action'><i class='mesv3-dot' style='background:#12a95e'></i><div class='mesv3-action-sub'>Veritabanı: {'Neon PostgreSQL' if USING_POSTGRES else 'Yerel SQLite'}</div></div><div class='mesv3-action'><i class='mesv3-dot' style='background:#12a95e'></i><div class='mesv3-action-sub'>Yerel simülasyon yedeği hazır</div></div><div class='mesv3-action'><i class='mesv3-dot' style='background:#12a95e'></i><div class='mesv3-action-sub'>Son güncelleme: {datetime.now():%H:%M:%S}</div></div></div>", unsafe_allow_html=True)
    st.stop()

if selected_module == "🏠 Ana Sayfa":
    # Ana sayfa: referans görseldeki oranlar ve bilgi hiyerarşisiyle yeniden kurulmuş kompakt panel.
    st.markdown("""
    <style>
    .home-v2-title{background:linear-gradient(120deg,#effcf5,#fff);border:1px solid #d8eee1;border-radius:12px;padding:12px 16px;margin-bottom:10px}.home-v2-title h2{font-size:1.15rem!important;margin:0!important;color:#123d2d!important}.home-v2-title p{margin:2px 0 0;font-size:.74rem;color:#62806f}.home-v2-kpi{min-height:126px;background:#fff;border:1px solid #cdeadc;border-radius:11px;padding:13px 14px;box-shadow:0 3px 12px rgba(7,84,45,.06)}.home-v2-kpi-label{font-size:.73rem;font-weight:850;color:#315b49}.home-v2-kpi-value{font-size:1.55rem;font-weight:900;color:#123a2a;margin-top:9px}.home-v2-kpi-note{font-size:.68rem;color:#13a15b;margin-top:3px;font-weight:700}.home-v2-track{height:7px;border-radius:99px;background:#e2f3e9;overflow:hidden;margin-top:15px}.home-v2-track i{display:block;height:100%;background:linear-gradient(90deg,#10aa5c,#55cf8a);border-radius:99px}.home-v2-panel{background:#fff;border:1px solid #d6ebde;border-radius:12px;padding:11px;box-shadow:0 3px 12px rgba(7,84,45,.055);height:100%;box-sizing:border-box}.home-v2-panel-title{color:#134d36;font-size:.84rem;font-weight:900;padding:0 2px 9px;border-bottom:1px solid #e7f3eb;margin-bottom:9px}.home-v2-machine{background:linear-gradient(145deg,#fff,#f8fffa);border:1px solid #dcefe4;border-radius:10px;padding:11px;min-height:170px}.home-v2-machine-head{display:flex;justify-content:space-between;align-items:center;font-weight:900;color:#153f2e;font-size:.88rem}.home-v2-badge{font-size:.62rem;border-radius:99px;padding:4px 7px;font-weight:800}.home-v2-ok{color:#078640;background:#ddf8e7}.home-v2-wait{color:#9c6500;background:#fff1cf}.home-v2-fault{color:#bb3030;background:#ffe0e0}.home-v2-machine-body{display:grid;grid-template-columns:94px 1fr;gap:7px;align-items:center;margin:12px 0 8px}.home-v2-ring{--ring:#13a95c;--angle:80;width:82px;height:82px;border-radius:50%;background:conic-gradient(var(--ring) calc(var(--angle)*1%),#e6f0e9 0);position:relative;margin:auto}.home-v2-ring:after{content:"";position:absolute;inset:9px;border-radius:50%;background:#fff}.home-v2-ring b{position:absolute;z-index:2;inset:25px 0 0;text-align:center;font-size:.78rem;color:#144535}.home-v2-spec{font-size:.68rem;color:#506e5e;line-height:1.85}.home-v2-spec b{float:right;color:#244c3a}.home-v2-machine-foot{border-top:1px solid #edf5f0;padding-top:7px;font-size:.65rem;color:#607c6d}.home-v2-action{display:flex;gap:9px;padding:10px 2px;border-bottom:1px solid #eaf3ee;align-items:flex-start}.home-v2-action:last-child{border:0}.home-v2-action-dot{width:11px;height:11px;border-radius:50%;margin-top:3px;flex:0 0 11px}.home-v2-action-main{font-size:.7rem;font-weight:850;color:#23503d}.home-v2-action-sub{font-size:.66rem;color:#597868;margin-top:3px}.home-v2-action-time{margin-left:auto;font-size:.66rem;color:#748f80;white-space:nowrap}.home-v2-summary{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px}.home-v2-summary-item{border:1px solid #e0f0e6;border-radius:9px;padding:9px 8px;min-height:55px}.home-v2-summary-item small{display:block;color:#607e6d;font-size:.63rem;font-weight:750}.home-v2-summary-item strong{display:block;margin-top:4px;font-size:.86rem;color:#124533;overflow-wrap:anywhere}.st-key-v2_quick_order button,.st-key-v2_quick_maintenance button{height:58px!important;min-height:58px!important;font-size:.72rem!important;white-space:normal!important;line-height:1.25!important;color:#075337!important;background:#f7fffa!important;border-color:#d4ecdf!important}.st-key-v2_quick_order button:hover,.st-key-v2_quick_maintenance button:hover{background:#e5f9ec!important}.home-v2-chart div[data-testid="stPlotlyChart"]{border:0!important;box-shadow:none!important;padding:0!important}.home-v2-note{font-size:.66rem;color:#738e80;text-align:right;margin-top:5px}@media(max-width:1000px){.home-v2-machine-body{grid-template-columns:80px 1fr}}
    </style>
    """, unsafe_allow_html=True)

    live_alarms = q("SELECT machine_code,alarm,level,time FROM alarms WHERE acknowledged=0 ORDER BY id DESC LIMIT 4")
    due_maintenance = q("SELECT machine_code,maintenance_type,next_date FROM maintenance WHERE status != 'Tamamlandı' ORDER BY next_date LIMIT 2")
    open_work_orders = int(q("SELECT COUNT(*) AS total FROM work_orders WHERE status != 'Tamamlandı'").iloc[0]["total"])
    target_percent = total_production / max(total_target, 1) * 100
    today_name = datetime.now().strftime("%d %B %Y")

    main_area, action_area = st.columns([4.05, 1.38], gap="small")
    with main_area:
        st.markdown(f"<div class='home-v2-title'><h2>🏭 Fabrika Genel Durumu</h2><p>Tüm hatlar ve makineler anlık olarak izleniyor · {today_name}</p></div>", unsafe_allow_html=True)
        kpi_values = [
            ("⚙ ÜRETİM", f"{total_production:,}", f"Hedef: {total_target:,}", target_percent),
            ("◎ HEDEF", f"{total_target:,}", f"Kalan: {max(total_target-total_production,0):,} adet", target_percent),
            ("◉ OEE", f"%{average_oee:.1f}", "Canlı performans", average_oee),
            ("⬟ KALİTE", f"%{average_quality:.1f}", "Uygun üretim oranı", average_quality),
        ]
        kpi_cols = st.columns(4, gap="small")
        for col, (title, value, note, percent) in zip(kpi_cols, kpi_values):
            with col:
                st.markdown(f"<div class='home-v2-kpi'><div class='home-v2-kpi-label'>{title}</div><div class='home-v2-kpi-value'>{value}</div><div class='home-v2-kpi-note'>▲ {note}</div><div class='home-v2-track'><i style='width:{min(max(percent,0),100):.1f}%'></i></div></div>", unsafe_allow_html=True)

        machine_cards = []
        for _, machine in df.sort_values("machine_code").iterrows():
            status = str(machine["status"])
            badge = "home-v2-ok" if status == "Çalışıyor" else ("home-v2-wait" if status == "Beklemede" else "home-v2-fault")
            ring_color = "#11aa5a" if status == "Çalışıyor" else ("#ffb616" if status == "Beklemede" else "#ef5656")
            oee = max(0, min(float(machine["oee"]) * 100, 100))
            machine_cards.append(f"""<div class='home-v2-machine'><div class='home-v2-machine-head'><span>⚙ {machine['machine_code']}</span><span class='home-v2-badge {badge}'>● {status}</span></div><div class='home-v2-machine-body'><div class='home-v2-ring' style='--ring:{ring_color};--angle:{oee:.1f}'><b>OEE<br><span style='font-size:1rem'>%{oee:.1f}</span></b></div><div class='home-v2-spec'>Üretim <b>{int(machine['production']):,} / {int(machine['target']):,}</b><br>Kullanılabilirlik <b>%{float(machine['availability'])*100:.1f}</b><br>Performans <b>%{float(machine['performance'])*100:.1f}</b><br>Kalite <b>%{float(machine['quality'])*100:.1f}</b></div></div><div class='home-v2-machine-foot'>Ürün: {machine['product']} &nbsp; | &nbsp; Operatör: {machine['operator']}</div></div>""")
        st.markdown("<div class='home-v2-panel' style='margin-top:10px'><div class='home-v2-panel-title'>⚙ Makine Durumları <span style='float:right;color:#698575;font-size:.68rem'>● Tüm Makineler</span></div>", unsafe_allow_html=True)
        card_columns = st.columns(max(len(machine_cards), 1), gap="small")
        for col, card in zip(card_columns, machine_cards):
            with col:
                st.markdown(card, unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True)

        downtime_rows = q("SELECT reason,duration FROM downtime ORDER BY id DESC")
        if downtime_rows.empty:
            downtime_chart = pd.DataFrame({"Neden": ["Arıza", "Bakım", "Malzeme", "Diğer"], "Süre": [0, 0, 0, 0]})
        else:
            downtime_chart = downtime_rows.groupby("reason", as_index=False)["duration"].sum().rename(columns={"reason":"Neden", "duration":"Süre"}).sort_values("Süre", ascending=False).head(5)
        history = q("SELECT quantity,timestamp FROM production_history ORDER BY id DESC LIMIT 500")
        if history.empty:
            production_trend = pd.DataFrame({"Saat": ["Şimdi"], "Üretim": [total_production]})
        else:
            history["timestamp"] = pd.to_datetime(history["timestamp"], errors="coerce")
            history = history.dropna(subset=["timestamp"])
            production_trend = history.groupby(history["timestamp"].dt.strftime("%H:%M"), as_index=False)["quantity"].max().tail(12).rename(columns={"timestamp":"Saat", "quantity":"Üretim"})
        lower_a, lower_b, lower_c = st.columns([1.18, 1.1, .9], gap="small")
        with lower_a:
            chart = px.bar(production_trend, x="Saat", y="Üretim", title="Son 24 Saat Üretim Trendi", color_discrete_sequence=["#12a95e"])
            chart.add_scatter(x=production_trend["Saat"], y=production_trend["Üretim"], mode="lines+markers", line=dict(color="#42c985"), name="Üretim")
            chart.update_layout(height=225, margin=dict(l=5,r=5,t=37,b=4), xaxis_title=None, yaxis_title=None, showlegend=False, paper_bgcolor="rgba(0,0,0,0)",plot_bgcolor="rgba(0,0,0,0)")
            st.markdown("<div class='home-v2-panel home-v2-chart'>", unsafe_allow_html=True); st.plotly_chart(chart, use_container_width=True, config={"displayModeBar":False}, key="v2_production_trend"); st.markdown("</div>", unsafe_allow_html=True)
        with lower_b:
            donut = px.pie(downtime_chart, values="Süre", names="Neden", hole=.6, title=f"Duruş Analizi · {float(downtime_chart['Süre'].sum()):.0f} dk", color_discrete_sequence=["#ef5656", "#ffb720", "#4ab8f5", "#16aa5e", "#9264df"])
            donut.update_layout(height=225, margin=dict(l=5,r=5,t=37,b=4), legend=dict(font_size=9), paper_bgcolor="rgba(0,0,0,0)")
            st.markdown("<div class='home-v2-panel home-v2-chart'>", unsafe_allow_html=True); st.plotly_chart(donut, use_container_width=True, config={"displayModeBar":False}, key="v2_downtime_donut"); st.markdown("</div>", unsafe_allow_html=True)
        with lower_c:
            oee_chart = df[["machine_code", "oee"]].copy(); oee_chart["OEE"] = (oee_chart["oee"] * 100).round(1)
            bars = px.bar(oee_chart, x="OEE", y="machine_code", orientation="h", title="Makine Bazında OEE", text="OEE", color="OEE", color_continuous_scale=["#ffb51c", "#13a95c"])
            bars.update_traces(texttemplate="%{text:.1f}%", textposition="outside")
            bars.update_layout(height=225, margin=dict(l=5,r=20,t=37,b=4), xaxis_range=[0,105], xaxis_title=None, yaxis_title=None, coloraxis_showscale=False, paper_bgcolor="rgba(0,0,0,0)",plot_bgcolor="rgba(0,0,0,0)")
            st.markdown("<div class='home-v2-panel home-v2-chart'>", unsafe_allow_html=True); st.plotly_chart(bars, use_container_width=True, config={"displayModeBar":False}, key="v2_oee_bars"); st.markdown("</div>", unsafe_allow_html=True)

        shift_name = active_shift_name()
        shift_total = shift_production_totals()
        shift_quantity = float(shift_total[shift_total["Vardiya"] == shift_name]["Üretim"].sum()) if not shift_total.empty else 0
        maintenance_rows = due_maintenance.head(3)
        bottom_a, bottom_b, bottom_c, bottom_d = st.columns([1.12, 1.05, 1.08, .95], gap="small")
        with bottom_a:
            st.markdown(f"<div class='home-v2-panel'><div class='home-v2-panel-title'>Bugünün Özeti</div><div class='home-v2-summary'><div class='home-v2-summary-item'><small>Üretim hedefi</small><strong>%{target_percent:.1f}</strong></div><div class='home-v2-summary-item'><small>En yüksek duruş</small><strong>{downtime_chart.iloc[0]['Neden'] if not downtime_chart.empty else '—'}</strong></div><div class='home-v2-summary-item'><small>Açık alarm</small><strong>{len(live_alarms)}</strong></div><div class='home-v2-summary-item'><small>Kritik stok</small><strong>{int(q('SELECT COUNT(*) AS total FROM products WHERE stock < min_stock').iloc[0]['total'])}</strong></div></div></div>", unsafe_allow_html=True)
        with bottom_b:
            st.markdown(f"<div class='home-v2-panel'><div class='home-v2-panel-title'>Vardiya Durumu <span style='float:right;color:#0d9551'>{shift_name}</span></div><div style='font-size:.76rem;color:#587767;margin:7px 0'>Üretim <b style='float:right;color:#124533'>{shift_quantity:,.0f} adet</b></div><div class='home-v2-track'><i style='width:{min(target_percent,100):.1f}%'></i></div><div style='font-size:.74rem;margin-top:12px;color:#265642'>OEE <b style='float:right'>%{average_oee:.1f}</b></div><div class='home-v2-note'>Aktif vardiya operasyon görünümü</div></div>", unsafe_allow_html=True)
        with bottom_c:
            maintenance_html = "".join(f"<div class='home-v2-action'><i class='home-v2-action-dot' style='background:#ffb51c'></i><div><div class='home-v2-action-main'>{item['machine_code']} · {item['maintenance_type']}</div><div class='home-v2-action-sub'>{item['next_date']}</div></div></div>" for _, item in maintenance_rows.iterrows()) or "<div class='home-v2-action-sub'>Yaklaşan bakım kaydı yok.</div>"
            st.markdown(f"<div class='home-v2-panel'><div class='home-v2-panel-title'>Yaklaşan Bakımlar</div>{maintenance_html}</div>", unsafe_allow_html=True)
        with bottom_d:
            with st.container(border=True):
                st.markdown("<div class='home-v2-panel-title'>Hızlı İşlemler</div>", unsafe_allow_html=True)
                if st.button("📋 Yeni İş Emri", use_container_width=True, key="v2_quick_order"):
                    st.session_state["selected_module"] = "📋 İş Emirleri"; st.rerun()
                if st.button("🧰 Bakım Talebi", use_container_width=True, key="v2_quick_maintenance"):
                    st.session_state["selected_module"] = "🧰 Bakım Talebi"; st.rerun()
    with action_area:
        action_html = []
        for _, item in live_alarms.iterrows():
            color = "#ef4c4c" if item["level"] == "Kritik" else "#f5b616"
            action_html.append(f"<div class='home-v2-action'><i class='home-v2-action-dot' style='background:{color}'></i><div><div class='home-v2-action-main'>{item['level']} Alarm</div><div class='home-v2-action-sub'>{item['machine_code']} · {item['alarm']}</div></div><span class='home-v2-action-time'>{str(item['time'])[-8:-3]}</span></div>")
        for _, item in due_maintenance.iterrows():
            action_html.append(f"<div class='home-v2-action'><i class='home-v2-action-dot' style='background:#f5b616'></i><div><div class='home-v2-action-main'>Bakım Riski</div><div class='home-v2-action-sub'>{item['machine_code']} · {item['maintenance_type']}</div></div><span class='home-v2-action-time'>{item['next_date']}</span></div>")
        if target_percent < 100:
            action_html.append(f"<div class='home-v2-action'><i class='home-v2-action-dot' style='background:#f5b616'></i><div><div class='home-v2-action-main'>Üretim Hedef Riski</div><div class='home-v2-action-sub'>Üretim %{target_percent:.1f} · hedefin altında</div></div></div>")
        st.markdown(f"<div class='home-v2-panel'><div class='home-v2-panel-title'>🔔 Aksiyon Merkezi <span style='float:right;color:#0f9860;font-size:.67rem'>Canlı</span></div>{''.join(action_html) or '<div class=reference-empty>Öncelikli kayıt yok.</div>'}</div>", unsafe_allow_html=True)
        if st.button("Tüm alarmları gör", use_container_width=True, key="v2_all_alarms"):
            st.session_state["selected_module"] = "🚨 Alarmlar"; st.rerun()
    st.stop()

if selected_module == "🏠 Ana Sayfa":
    # Referans tasarımdaki gibi: tek ekranda karar vermeye odaklı, kompakt yönetici görünümü.
    st.markdown("""
    <style>
    .reference-dashboard{max-width:1500px;margin:0 auto}.reference-head{display:flex;justify-content:space-between;align-items:center;background:#fff;border:1px solid #d8ece1;border-radius:13px;padding:11px 16px;margin-bottom:9px;box-shadow:0 3px 12px rgba(7,84,45,.06)}
    .reference-head h2{margin:0!important;font-size:1.05rem!important;color:#075337!important}.reference-head p{margin:2px 0 0;color:#6d8879;font-size:.73rem}.reference-meta{display:flex;gap:18px;text-align:right;color:#416b56;font-size:.7rem}.reference-kpi{background:#fff;border:1px solid #d9ece2;border-radius:11px;padding:11px 13px;min-height:78px;box-shadow:0 3px 11px rgba(7,84,45,.05)}.reference-kpi small{display:block;color:#668174;font-weight:750;font-size:.7rem}.reference-kpi strong{display:block;color:#075337;font-size:1.33rem;margin:4px 0 1px}.reference-kpi span{color:#18a35c;font-size:.65rem;font-weight:700}
    .reference-panel{background:rgba(255,255,255,.96);border:1px solid #d9ece2;border-radius:12px;padding:11px 12px;box-shadow:0 3px 11px rgba(7,84,45,.05);height:100%}.reference-panel-title{font-size:.83rem;font-weight:850;color:#075337;padding-bottom:8px;margin-bottom:8px;border-bottom:1px solid #e7f2eb}.reference-machine{display:grid;grid-template-columns:1.15fr .85fr .8fr .85fr;gap:8px;align-items:center;padding:8px 4px;border-bottom:1px solid #edf5f0;font-size:.72rem;color:#345646}.reference-machine:last-child{border-bottom:0}.reference-machine b{color:#093c28}.reference-state{border-radius:999px;padding:3px 6px;font-size:.64rem;font-weight:800;text-align:center}.state-run{color:#07823c;background:#d9f7e3}.state-wait{color:#9a6500;background:#fff1cf}.state-fault{color:#bb3030;background:#ffe1e1}.reference-action{display:flex;gap:8px;align-items:center;padding:7px 2px;border-bottom:1px solid #edf5f0;font-size:.71rem}.reference-action:last-child{border:0}.reference-action-dot{width:8px;height:8px;border-radius:50%;flex:0 0 8px}.reference-action-title{font-weight:750;color:#173d2a}.reference-action-note{margin-left:auto;color:#7a9285;white-space:nowrap}.reference-empty{padding:20px 5px;color:#789084;font-size:.75rem;text-align:center}.reference-footer-note{font-size:.67rem;color:#789084;margin-top:7px}.reference-dashboard div[data-testid="stPlotlyChart"]{padding:1px;border-radius:11px;box-shadow:none}.reference-dashboard .stButton button{min-height:35px!important;font-size:.74rem!important;padding:4px 8px!important}
    @media(max-width:900px){.reference-head{align-items:flex-start;gap:10px}.reference-meta{display:none}.reference-machine{grid-template-columns:1fr 1fr}.reference-dashboard{padding-bottom:10px}}
    </style>
    """, unsafe_allow_html=True)

    user_name = st.session_state.get("full_name", st.session_state.get("username", "Kullanıcı"))
    st.markdown(f"""<div class="reference-dashboard"><div class="reference-head">
        <div><h2>Hoş geldiniz, {user_name}</h2><p>Fabrika operasyonunun canlı özeti ve güncel öncelikleri</p></div>
        <div class="reference-meta"><div>📅 {date.today():%d.%m.%Y}<br><b>Operasyon günü</b></div><div>🏭 {len(df)} makine<br><b>Sistem aktif</b></div></div>
    </div>""", unsafe_allow_html=True)

    remaining_target = max(total_target - total_production, 0)
    dashboard_kpis = [
        ("Toplam üretim", f"{total_production:,}", f"Hedefin %{(total_production / max(total_target, 1) * 100):.0f}'i"),
        ("Günlük hedef", f"{total_target:,}", f"Kalan {remaining_target:,} adet"),
        ("Ortalama OEE", f"%{average_oee:.1f}", "Canlı performans"),
        ("Kalite", f"%{average_quality:.1f}", "Uygun üretim oranı"),
    ]
    kpi_columns = st.columns(4)
    for column, (label, value, note) in zip(kpi_columns, dashboard_kpis):
        with column:
            st.markdown(f'<div class="reference-kpi"><small>{label}</small><strong>{value}</strong><span>● {note}</span></div>', unsafe_allow_html=True)

    recent_alarms = q("SELECT machine_code,alarm,level,time FROM alarms WHERE acknowledged=0 ORDER BY id DESC LIMIT 3")
    maintenance_alerts = q("SELECT machine_code,maintenance_type,next_date FROM maintenance WHERE status != 'Tamamlandı' ORDER BY next_date LIMIT 2")
    action_items = []
    for _, item in recent_alarms.iterrows():
        action_items.append(("#ef5a5a" if item["level"] == "Kritik" else "#f0a534", item["machine_code"], item["alarm"], item["level"]))
    for _, item in maintenance_alerts.iterrows():
        action_items.append(("#28a868", item["machine_code"], f"{item['maintenance_type']} · {item['next_date']}", "Bakım"))

    dashboard_left, dashboard_right = st.columns([1.45, 1])
    with dashboard_left:
        machine_html = []
        for _, machine in df.sort_values(["status", "machine_code"]).iterrows():
            status = str(machine["status"])
            state_class = "state-run" if status == "Çalışıyor" else ("state-wait" if status == "Beklemede" else "state-fault")
            machine_html.append(f"<div class='reference-machine'><b>{machine['machine_code']}</b><span class='reference-state {state_class}'>● {status}</span><span>{int(machine['production']):,} / {int(machine['target']):,}</span><b>%{float(machine['oee']) * 100:.1f}</b></div>")
        st.markdown(f"<div class='reference-panel'><div class='reference-panel-title'>⚙ Makine Durumları <span style='float:right;color:#779184;font-weight:650'>Üretim / Hedef · OEE</span></div>{''.join(machine_html) or '<div class=reference-empty>Makine kaydı yok.</div>'}</div>", unsafe_allow_html=True)
    with dashboard_right:
        action_html = []
        for color, machine_code, subject, tag in action_items[:5]:
            action_html.append(f"<div class='reference-action'><i class='reference-action-dot' style='background:{color}'></i><div><div class='reference-action-title'>{machine_code} · {subject}</div></div><span class='reference-action-note'>{tag}</span></div>")
        st.markdown(f"<div class='reference-panel'><div class='reference-panel-title'>⚠ Aksiyon Merkezi <span style='float:right;color:#779184;font-weight:650'>{len(action_items)} kayıt</span></div>{''.join(action_html) or '<div class=reference-empty>Şu an acil aksiyon gerektiren kayıt yok.</div>'}</div>", unsafe_allow_html=True)

    quick_alarm, quick_work, quick_maintenance = st.columns(3)
    with quick_alarm:
        if st.button(f"🚨 Alarmlar · {len(recent_alarms)} açık", use_container_width=True, key="home_ref_alarm"):
            st.session_state["selected_module"] = "🚨 Alarmlar"; st.rerun()
    with quick_work:
        open_orders = int(q("SELECT COUNT(*) AS total FROM work_orders WHERE status != 'Tamamlandı'").iloc[0]["total"])
        if st.button(f"📋 İş emirleri · {open_orders} açık", use_container_width=True, key="home_ref_orders"):
            st.session_state["selected_module"] = "📋 İş Emirleri"; st.rerun()
    with quick_maintenance:
        if st.button("🧰 Bakım planı", use_container_width=True, key="home_ref_maintenance"):
            st.session_state["selected_module"] = "🔧 Bakım"; st.rerun()

    history = q("SELECT machine_code,quantity,timestamp FROM production_history ORDER BY id DESC LIMIT 500")
    if history.empty:
        trend = pd.DataFrame({"Zaman": [datetime.now()], "Üretim": [total_production]})
    else:
        history["timestamp"] = pd.to_datetime(history["timestamp"], errors="coerce")
        history = history.dropna(subset=["timestamp"])
        trend = history.groupby(history["timestamp"].dt.strftime("%H:%M"), as_index=False)["quantity"].max().tail(12).rename(columns={"timestamp":"Zaman", "quantity":"Üretim"})
    shift_totals = shift_production_totals()
    shift_view = pd.DataFrame({"Vardiya": ["Sabah", "Akşam", "Gece"], "Üretim": [0, 0, 0]})
    if not shift_totals.empty:
        recorded = shift_totals.groupby("Vardiya", as_index=False)["Üretim"].sum()
        shift_view = shift_view.drop(columns="Üretim").merge(recorded, on="Vardiya", how="left").fillna({"Üretim": 0})
    trend_col, shift_col, recent_col = st.columns([1.25, .9, 1.05])
    with trend_col:
        trend_chart = px.line(trend, x="Zaman", y="Üretim", markers=True, title="Üretim Trendi", color_discrete_sequence=["#11965a"])
        trend_chart.update_layout(height=220, margin=dict(l=8, r=8, t=38, b=8), xaxis_title=None, yaxis_title=None, showlegend=False, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
        st.plotly_chart(trend_chart, use_container_width=True, config={"displayModeBar": False}, key="reference_home_trend")
    with shift_col:
        shift_chart = px.bar(shift_view, x="Üretim", y="Vardiya", orientation="h", title="Vardiya Performansı", text="Üretim", color_discrete_sequence=["#0b8d55"])
        shift_chart.update_layout(height=220, margin=dict(l=8, r=8, t=38, b=8), xaxis_title=None, yaxis_title=None, showlegend=False, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
        st.plotly_chart(shift_chart, use_container_width=True, config={"displayModeBar": False}, key="reference_home_shift")
    with recent_col:
        recent_audit = q("SELECT username,action,entity,timestamp FROM audit_log ORDER BY id DESC LIMIT 5")
        activity_html = []
        for _, item in recent_audit.iterrows():
            activity_html.append(f"<div class='reference-action'><i class='reference-action-dot' style='background:#18a45e'></i><div><div class='reference-action-title'>{item['action']}</div><span style='color:#718b7d'>{item['entity']} · {item['username']}</span></div></div>")
        st.markdown(f"<div class='reference-panel'><div class='reference-panel-title'>◷ Son İşlemler</div>{''.join(activity_html) or '<div class=reference-empty>Henüz işlem kaydı yok.</div>'}<div class='reference-footer-note'>Veriler canlı kayıtlardan güncellenir.</div></div>", unsafe_allow_html=True)

    st.markdown("</div>", unsafe_allow_html=True)
    st.stop()

    # =====================================================
    # TREX MES - FABRİKA GENELİ CANLI OPERASYON / ANALİZ
    # =====================================================
    st.markdown("""
    <style>
    .main .block-container{max-width:1600px;padding-top:1rem!important}
    .analytics-title{font-size:1.55rem;font-weight:900;color:#102b20;margin-bottom:0}
    .analytics-sub{font-size:.88rem;color:#49665a;margin-top:-3px;margin-bottom:10px}
    div[data-testid="stPlotlyChart"]{background:rgba(255,255,255,.97);border:1px solid #d6e5da;border-radius:13px;padding:3px;box-shadow:0 4px 14px rgba(0,55,28,.10)}
    .mini-kpi{background:#fff;border:1px solid #d7e6db;border-radius:13px;padding:11px 14px;min-height:112px;box-shadow:0 4px 14px rgba(0,55,28,.10)}
    .mini-kpi-title{font-size:.78rem;font-weight:800;color:#1c3128}.mini-kpi-value{font-size:1.8rem;font-weight:900;color:#111;margin-top:7px}.mini-kpi-note{font-size:.72rem;color:#64766d}
    /* Canlı üretim ekranı: neon vurgulu, tıklanabilir üst KPI kartları */
    .st-key-kpi_oee button,.st-key-kpi_availability button,.st-key-kpi_performance button,.st-key-kpi_quality button,.st-key-kpi_alarms button,.st-key-kpi_production button{min-height:112px!important;border-radius:15px!important;border:1px solid rgba(120,230,255,.35)!important;color:#f6fbff!important;text-align:left!important;white-space:pre-line!important;padding:16px!important;font-weight:800!important;line-height:1.65!important;box-shadow:0 10px 24px rgba(7,27,65,.24)!important;transition:transform .18s ease,box-shadow .18s ease!important}
    .st-key-kpi_oee button:hover,.st-key-kpi_availability button:hover,.st-key-kpi_performance button:hover,.st-key-kpi_quality button:hover,.st-key-kpi_alarms button:hover,.st-key-kpi_production button:hover{transform:translateY(-3px);box-shadow:0 14px 30px rgba(14,124,215,.34)!important;color:#fff!important}
    .st-key-kpi_oee button{background:radial-gradient(circle at 18% 20%,#397b5f 0,#124632 42%,#08261c 100%)!important;box-shadow:inset 0 0 20px rgba(104,244,177,.16),0 10px 24px rgba(7,70,39,.24)!important}
    .st-key-kpi_availability button{background:radial-gradient(circle at 18% 20%,#4b8762 0,#1b5538 42%,#0c2d20 100%)!important;box-shadow:inset 0 0 20px rgba(139,244,165,.17),0 10px 24px rgba(7,70,39,.24)!important}
    .st-key-kpi_performance button{background:radial-gradient(circle at 18% 20%,#2e7354 0,#104b34 42%,#082b20 100%)!important;box-shadow:inset 0 0 20px rgba(81,235,145,.16),0 10px 24px rgba(7,70,39,.24)!important}
    .st-key-kpi_quality button{background:radial-gradient(circle at 18% 20%,#448a69 0,#1d5b40 42%,#0e3225 100%)!important;box-shadow:inset 0 0 20px rgba(136,247,184,.18),0 10px 24px rgba(7,70,39,.24)!important}
    .st-key-kpi_alarms button{background:radial-gradient(circle at 18% 20%,#627d48 0,#334d25 42%,#1b2d16 100%)!important;box-shadow:inset 0 0 20px rgba(191,231,111,.16),0 10px 24px rgba(54,74,20,.24)!important}
    .st-key-kpi_production button{background:radial-gradient(circle at 18% 20%,#2d805e 0,#15543d 42%,#092d21 100%)!important;box-shadow:inset 0 0 20px rgba(89,245,163,.18),0 10px 24px rgba(7,70,39,.24)!important}
    .st-key-emerald_production button,.st-key-emerald_alarms button,.st-key-emerald_machines button,.st-key-emerald_target button{min-height:54px!important;height:54px!important;border-radius:12px!important;background:linear-gradient(135deg,#0d5a3d,#063d2a)!important;border:1px solid rgba(111,238,167,.34)!important;color:#f5fff8!important;text-align:left!important;white-space:pre-line!important;padding:7px 12px!important;line-height:1.28!important;font-size:.82rem!important;box-shadow:inset 0 0 18px rgba(100,246,165,.12),0 6px 14px rgba(6,69,42,.16)!important}
    .st-key-emerald_production button:hover,.st-key-emerald_alarms button:hover,.st-key-emerald_machines button:hover,.st-key-emerald_target button:hover{background:linear-gradient(135deg,#117048,#075034)!important;transform:translateY(-2px)!important;color:#fff!important}
    .mini-speed-card{height:100px;background:rgba(255,255,255,.95);border:1px solid #d6e7da;border-radius:13px;padding:7px 8px;box-shadow:0 4px 12px rgba(7,70,39,.07);text-align:center;overflow:hidden}.mini-speed-title{font-size:.71rem;font-weight:800;color:#285842;margin-bottom:1px}.speed-wrap{position:relative;width:116px;height:65px;margin:0 auto;overflow:hidden}.speed-ring{position:absolute;width:112px;height:112px;left:2px;top:0;border-radius:50%;background:conic-gradient(from 270deg,var(--gcolor) 0deg var(--angle),#e8f2ec var(--angle) 180deg,transparent 180deg)}.speed-ring:after{content:"";position:absolute;width:86px;height:86px;border-radius:50%;left:13px;top:13px;background:#fff}.speed-number{position:absolute;z-index:2;bottom:0;left:0;width:100%;font-size:1rem;font-weight:900;color:#073e27}
    </style>
    """, unsafe_allow_html=True)

    st.markdown('<div class="analytics-title">🏭 trex MES - Manufacturing Execution System</div><div class="analytics-sub">Fabrika Geneli Canlı Operasyon ve Analiz Paneli</div>', unsafe_allow_html=True)

    avg_availability = float(df["availability"].mean()*100)
    avg_performance = float(df["performance"].mean()*100)
    open_alarms = int(q("SELECT COUNT(*) AS n FROM alarms WHERE acknowledged=0").iloc[0]["n"])

    def compact_speed_card(title, value, color="#19b66a"):
        value = max(0, min(float(value), 100))
        return f'''<div class="mini-speed-card"><div class="mini-speed-title">{title}</div>
        <div class="speed-wrap"><div class="speed-ring" style="--gcolor:{color};--angle:{value * 1.8:.1f}deg"></div>
        <div class="speed-number">%{value:.1f}</div></div></div>'''

    # Üst sıra: hız göstergesi benzeri canlı OEE göstergeleri.
    k1,k2,k3,k4=st.columns(4)
    with k1:
        st.markdown(compact_speed_card("OEE", average_oee, "#12a85a"), unsafe_allow_html=True)
    with k2:
        st.markdown(compact_speed_card("Kullanılabilirlik", avg_availability, "#25b87d"), unsafe_allow_html=True)
    with k3:
        st.markdown(compact_speed_card("Performans", avg_performance, "#0f8d61"), unsafe_allow_html=True)
    with k4:
        st.markdown(compact_speed_card("Kalite", average_quality, "#38bd74"), unsafe_allow_html=True)

    # Alt sıra: operasyonel KPI kartları.
    p1,p2,p3,p4=st.columns(4)
    with p1:
        if st.button(f"Toplam Üretim\n{total_production:,} adet", key="emerald_production", use_container_width=True):
            st.session_state["selected_module"]="📈 Üretim"; st.rerun()
    with p2:
        if st.button(f"Açık Alarmlar\n{open_alarms} kayıt", key="emerald_alarms", use_container_width=True):
            st.session_state["selected_module"]="🚨 Alarmlar"; st.rerun()
    with p3:
        if st.button(f"Çalışan Makine\n{running} / {len(df)} makine", key="emerald_machines", use_container_width=True):
            st.session_state["selected_module"]="🏭 Makine"; st.rerun()
    with p4:
        remaining_target = max(total_target - total_production, 0)
        if st.button(f"Kalan Hedef\n{remaining_target:,} adet", key="emerald_target", use_container_width=True):
            st.session_state["selected_module"]="📋 İş Emirleri"; st.rerun()

    # 1. satır: üretim kayıpları / OEE heatmap / Pareto
    c1,c2,c3=st.columns([1.05,1.35,1.15])
    downtime_df=q("SELECT machine_code,reason,duration FROM downtime ORDER BY id DESC")
    with c1:
        if downtime_df.empty:
            loss=pd.DataFrame({"Kayıp":["Arıza","Ayar","Malzeme","Hata/Fire"],"Oran":[0,0,0,0]})
        else:
            mapped=downtime_df.copy(); mapped["Kayıp"]=mapped["reason"].fillna("Diğer")
            loss=mapped.groupby("Kayıp",as_index=False)["duration"].sum().sort_values("duration",ascending=False).head(5).rename(columns={"duration":"Oran"})
        fig=px.bar(loss,x="Kayıp",y="Oran",title="Üretim Kayıpları (Material)",text_auto='.1f')
        fig.update_layout(height=255,margin=dict(l=15,r=10,t=42,b=25),showlegend=False)
        st.plotly_chart(fig,use_container_width=True,key="home_loss")
    with c2:
        hist=filter_by_date_range(q("SELECT machine_code,quantity,timestamp FROM production_history ORDER BY id DESC LIMIT 1000"))
        if not hist.empty:
            hist["timestamp"] = pd.to_datetime(hist["timestamp"], errors="coerce")
            hist = hist.dropna(subset=["timestamp"])
            hist["Gün"] = hist["timestamp"].dt.date
            # Günlük üretim temposunu mevcut makine OEE değeriyle birleştirerek
            # saatlik değil, gün gün karşılaştırılabilir OEE görünümü oluşturur.
            chart_data = hist.groupby(["machine_code", "Gün"], as_index=False)["quantity"].max()
            chart_data = chart_data.merge(df[["machine_code", "oee"]], on="machine_code", how="left")
            daily_peak = chart_data.groupby("machine_code")["quantity"].transform("max").clip(lower=1)
            chart_data["OEE"] = (chart_data["oee"] * (0.88 + 0.12 * chart_data["quantity"] / daily_peak) * 100).clip(upper=100).round(1)
        else:
            chart_data = df[["machine_code", "oee"]].copy()
            chart_data["Gün"] = date.today()
            chart_data["OEE"] = (chart_data["oee"] * 100).round(1)
        fig = px.bar(chart_data, x="Gün", y="OEE", color="machine_code", barmode="group", title="Hat / Makine Bazlı Günlük OEE", text_auto=".1f")
        fig.update_layout(height=255,margin=dict(l=15,r=10,t=42,b=25),yaxis_range=[0,100],xaxis_title="Gün",yaxis_title="OEE (%)",legend_title_text="Makine")
        st.plotly_chart(fig,use_container_width=True,key="home_oee_bar")
    with c3:
        if downtime_df.empty: pareto=pd.DataFrame({"reason":["Veri yok"],"duration":[0]})
        else: pareto=downtime_df.groupby("reason",as_index=False)["duration"].sum().sort_values("duration",ascending=False).head(6)
        pareto["cum"]=(pareto["duration"].cumsum()/max(pareto["duration"].sum(),1)*100)
        fig=go.Figure(); fig.add_bar(x=pareto["reason"],y=pareto["duration"],name="Süre"); fig.add_scatter(x=pareto["reason"],y=pareto["cum"],name="Kümülatif %",yaxis="y2",mode="lines+markers")
        fig.update_layout(title="Duruş Sebepleri Pareto (Süre)",height=255,margin=dict(l=15,r=15,t=42,b=25),yaxis2=dict(overlaying="y",side="right",range=[0,110]),legend=dict(orientation="h",y=1.12,x=.55))
        st.plotly_chart(fig,use_container_width=True,key="home_pareto")

    # 2. satır: trend / zaman çizelgesi / plan-gerçekleşen
    c4,c5,c6=st.columns(3)
    with c4:
        trend_hist=filter_by_date_range(q("SELECT machine_code,quantity,timestamp FROM production_history ORDER BY id DESC LIMIT 1000"))
        if trend_hist.empty:
            trend_df=pd.DataFrame({"Zaman":["10 Eyl","11 Eyl","12 Eyl","13 Eyl","14 Eyl","15 Eyl","16 Eyl"]*4,"Değer":[average_oee,80,84,82,85,81,average_oee]*4,"Bileşen":sum(([x]*7 for x in ["OEE","Kullanılabilirlik","Performans","Kalite"]),[])})
        else:
            trend_hist["timestamp"]=pd.to_datetime(trend_hist["timestamp"],errors="coerce")
            daily=trend_hist.dropna().groupby(trend_hist["timestamp"].dt.strftime("%d %b"),as_index=False)["quantity"].mean().tail(7)
            vals=[average_oee,avg_availability,avg_performance,average_quality]; names=["OEE","Kullanılabilirlik","Performans","Kalite"]
            rows=[]
            for name,v in zip(names,vals):
                for i,t in enumerate(daily["timestamp"].tolist() if "timestamp" in daily else range(7)): rows.append({"Zaman":str(t),"Değer":max(0,min(100,v+((i*7+len(name))%13)-6)),"Bileşen":name})
            trend_df=pd.DataFrame(rows)
        fig=px.line(trend_df,x="Zaman",y="Değer",color="Bileşen",markers=True,title="OEE Bileşenleri Trendi")
        fig.update_layout(height=250,margin=dict(l=15,r=10,t=42,b=25),yaxis_range=[0,100])
        st.plotly_chart(fig,use_container_width=True,key="home_components")
    with c5:
        states=[]
        base=pd.Timestamp.today().normalize()+pd.Timedelta(hours=8)
        for j,(_,m) in enumerate(df.head(6).iterrows()):
            states += [{"Makine":m["machine_code"],"Başlangıç":base,"Bitiş":base+pd.Timedelta(hours=4+j%2),"Durum":"Çalışma"},{"Makine":m["machine_code"],"Başlangıç":base+pd.Timedelta(hours=4+j%2),"Bitiş":base+pd.Timedelta(hours=6),"Durum":"Bekleme/Ayar"},{"Makine":m["machine_code"],"Başlangıç":base+pd.Timedelta(hours=6),"Bitiş":base+pd.Timedelta(hours=7),"Durum":"Arıza"},{"Makine":m["machine_code"],"Başlangıç":base+pd.Timedelta(hours=7),"Bitiş":base+pd.Timedelta(hours=10),"Durum":"Çalışma"}]
        # Başlığı Plotly grafiğinin dışına alıyoruz; böylece legend ve araç çubuğu ile çakışmaz.
        st.markdown("<div style='font-size:1.02rem;font-weight:800;color:#0a4d2d;margin:0 0 2px 4px;'>Makine Durum Zaman Çizelgesi (Hat 3)</div>", unsafe_allow_html=True)
        tl=pd.DataFrame(states)
        fig=px.timeline(tl,x_start="Başlangıç",x_end="Bitiş",y="Makine",color="Durum")
        fig.update_yaxes(autorange="reversed", title_text="Makine")
        fig.update_layout(
            height=285,
            margin=dict(l=15,r=10,t=58,b=28),
            legend=dict(
                title_text="Durum",
                orientation="h",
                yanchor="bottom", y=1.02,
                xanchor="left", x=0
            )
        )
        st.plotly_chart(
            fig,
            use_container_width=True,
            key="home_timeline",
            config={"displayModeBar": False}
        )
    with c6:
        prod=df[["machine_code","target","production"]].melt(id_vars="machine_code",var_name="Tür",value_name="Adet"); prod["Tür"]=prod["Tür"].map({"target":"Planlanan","production":"Gerçekleşen"})
        fig=px.bar(prod,x="machine_code",y="Adet",color="Tür",barmode="group",title="Planlanan ve Gerçekleşen Üretim")
        fig.update_layout(height=250,margin=dict(l=15,r=10,t=42,b=25),legend=dict(orientation="h",y=1.13))
        st.plotly_chart(fig,use_container_width=True,key="home_plan_actual")

    # 3. satır: MTBF/MTTR / bubble / treemap / sankey
    c7,c8,c9=st.columns(3)
    with c7:
        reliability = reliability_metrics(df)
        chart_reliability = reliability.dropna(subset=["MTBF (dk)", "MTTR (dk)"])
        if chart_reliability.empty:
            st.info("MTBF/MTTR için en az bir 'Arıza' duruş kaydı gerekir.")
        else:
            fig=px.scatter(chart_reliability,x="MTBF (dk)",y="MTTR (dk)",color="Durum",size="Arıza Sayısı",hover_name="Makine",hover_data=["Arıza Sayısı","Çalışma Süresi (dk)"],title="Gerçek Kayıtlara Göre MTBF & MTTR")
            fig.update_layout(height=245,margin=dict(l=12,r=8,t=42,b=25),showlegend=False)
            st.plotly_chart(fig,use_container_width=True,key="home_mtbf")
            st.caption("MTBF = çalışma süresi / arıza sayısı · MTTR = onarım süresi / arıza sayısı")
    with c8:
        if downtime_df.empty: bub=pd.DataFrame({"Neden":["Veri yok"],"Süre":[1],"Sıklık":[1]})
        else: bub=downtime_df.groupby("reason").agg(Süre=("duration","sum"),Sıklık=("duration","size")).reset_index().rename(columns={"reason":"Neden"})
        fig=px.scatter(bub,x="Süre",y="Sıklık",size="Süre",color="Neden",title="Duruş Sıklığı vs Duruş Süresi",size_max=35)
        fig.update_layout(height=245,margin=dict(l=12,r=8,t=42,b=25),showlegend=False)
        st.plotly_chart(fig,use_container_width=True,key="home_bubble")
    with c9:
        if downtime_df.empty: tree=pd.DataFrame({"Kayıp":["Malzeme","Kalıp Ayarı","Arıza","İnsan Kaynaklı"],"Değer":[34,22,18,14]})
        else: tree=downtime_df.groupby("reason",as_index=False)["duration"].sum().rename(columns={"reason":"Kayıp","duration":"Değer"})
        fig=px.treemap(tree,path=["Kayıp"],values="Değer",title="Kayıp Dağılımı (Treemap)")
        fig.update_layout(height=245,margin=dict(l=8,r=8,t=42,b=8))
        st.plotly_chart(fig,use_container_width=True,key="home_tree")

    st.markdown(f'<div style="font-size:.72rem;color:#688176;padding:2px 4px 8px">trex MES | Digital Manufacturing Solutions <span style="float:right">Veri son güncelleme: {datetime.now():%d.%m.%Y %H:%M:%S} · 🟢 Sistem Çalışıyor</span></div>',unsafe_allow_html=True)



st.markdown("""<style>
.machine-panel-title{font-size:1.03rem;font-weight:850;color:#10291b;margin:0 0 6px 10px}.machine-live-card{background:#fff;border:1px solid #d8e5dc;border-radius:12px;box-shadow:0 5px 15px rgba(0,45,25,.13);overflow:hidden;margin-bottom:12px}.machine-live-head,.machine-live-row{display:grid;grid-template-columns:1.05fr 1.1fr 1.15fr 1.45fr .9fr .75fr 1.35fr 1.25fr 1.15fr .85fr;align-items:center;column-gap:8px}.machine-live-head{padding:8px 12px 5px;font-size:.68rem;border-bottom:1px solid #edf2ee}.machine-live-row{padding:7px 12px;font-size:.73rem;color:#23352b;border-bottom:1px solid #edf2ee}.mc-code{display:flex;align-items:center;gap:7px;font-size:.82rem}.mc-icon{width:25px;height:25px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;background:linear-gradient(145deg,#dfe5e1,#9da8a1)}.status-pill{border-radius:12px;padding:3px 7px;font-size:.67rem;font-weight:700}.status-pill.run{background:#c9f7d6;color:#08742d}.status-pill.wait{background:#ffe7ae;color:#8b5b00}.status-pill.fault{background:#ffd1d1;color:#a21d1d}.num{text-align:right;font-weight:750}.mini-wrap{display:flex;align-items:center;gap:5px;white-space:nowrap}.mini-wrap span{font-size:.68rem;min-width:37px}.mini-track{height:5px;flex:1;background:#e7eee9;border-radius:8px;overflow:hidden}.mini-track i{height:100%;display:block;border-radius:8px}.mini-track i.green{background:#19a85b}.mini-track i.amber{background:#e6a82d}.mini-track i.orange{background:#ed7040}.oee-cell{display:flex;align-items:center;gap:5px}.oee-dot{width:18px;height:18px;border-radius:50%;background:conic-gradient(#14a95b calc(var(--oee)*1%),#e6eee9 0);position:relative}.oee-dot:after{content:"";position:absolute;inset:4px;background:#fff;border-radius:50%}.energy-title{font-size:1.18rem;font-weight:900;color:#183a26;margin:10px 0 7px}.energy-kpi{min-height:76px;background:#fff;border:1px solid #d9e5dc;border-radius:11px;box-shadow:0 4px 13px rgba(0,40,22,.13);display:flex;align-items:center;padding:10px 14px;gap:12px}.energy-kpi.compact{min-height:54px;padding:7px 10px;margin-bottom:7px;gap:9px}.energy-kpi.compact .energy-symbol{font-size:1.4rem}.energy-kpi.compact small{font-size:.66rem}.energy-kpi.compact strong{font-size:1rem}.energy-kpi.compact span{font-size:.58rem}.energy-symbol{font-size:2rem;color:#10a84f}.energy-symbol.blue{color:#1aa6c8}.energy-symbol.leaf{color:#26a34f}.energy-kpi small{display:block;color:#52675b;font-size:.72rem}.energy-kpi strong{display:block;color:#0c2014;font-size:1.28rem}.energy-kpi span{font-size:.64rem;color:#799083}.power-title{margin-top:12px;padding:7px 10px;margin-bottom:-5px}@media(max-width:1100px){.machine-live-card{overflow-x:auto}.machine-live-head,.machine-live-row{min-width:1050px}}
</style>""",unsafe_allow_html=True)
if selected_module == "🏭 Makine":
    from machine_panel import render_machine_panel
    render_machine_panel(q, df)

    if is_admin():
        st.markdown("---")
        st.markdown("#### Yeni makine ekle")
        st.caption("Kaydedildiğinde makine; sensör, vardiya, bakım, enerji, QR ve iş emri modüllerine otomatik dahil edilir.")
        with st.container(border=True):
            col1, col2, col3 = st.columns(3)
            with col2:
                new_shift = st.selectbox(
                    "Vardiya",
                    ["Sabah", "Akşam", "Gece"],
                    index=["Sabah", "Akşam", "Gece"].index(active_shift_name()),
                    key="new_machine_shift"
                )
            operator_names = q(
                "SELECT name FROM operators WHERE active=1 AND shift=? ORDER BY name",
                (new_shift,)
            )["name"].dropna().tolist()
            with col1:
                new_machine_code = st.text_input("Makine kodu", placeholder="Örn. CNC-04")
                new_product = st.text_input("Başlangıç ürünü", value="Atama bekliyor")
                new_status = st.selectbox("İlk durum", ["Beklemede", "Çalışıyor"], index=0)
            with col2:
                new_operator = st.selectbox("Operatör", operator_names or ["Bu vardiyada operatör yok"], key="new_machine_operator")
                new_cycle = st.number_input("İdeal çevrim süresi (sn)", min_value=1.0, value=60.0, step=1.0)
            with col3:
                new_production = st.number_input("Başlangıç üretim", min_value=0, value=0, step=1)
                new_target = st.number_input("Başlangıç hedefi", min_value=0, value=0, step=1, help="İş emri yoksa 0 bırakın.")
                new_next_maintenance = st.date_input("Sonraki bakım", value=date.today() + timedelta(days=30))
            if not operator_names:
                st.warning(f"{new_shift} vardiyası için aktif operatör bulunmuyor. Önce Vardiya ekranından operatör ekleyin.")
            if st.button("➕ Makineyi sisteme ekle", use_container_width=True, disabled=not operator_names, key="add_machine_button"):
                machine_code = new_machine_code.strip().upper().replace(" ", "-")
                if not machine_code:
                    st.error("Makine kodu zorunludur.")
                elif not machine_code.replace("-", "").isalnum():
                    st.error("Makine kodunda yalnızca harf, rakam ve tire kullanın.")
                else:
                    try:
                        c = conn()
                        c.execute("""
                            INSERT INTO machines(machine_code,status,production,target,planned_time,downtime,ideal_cycle,defective,product,operator,shift,last_maintenance,next_maintenance)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """, (machine_code, new_status, int(new_production), int(new_target), 0, 0, float(new_cycle), 0, new_product, new_operator, new_shift, str(date.today()), str(new_next_maintenance)))
                        machine_id = c.execute("SELECT id FROM machines WHERE machine_code=?", (machine_code,)).fetchone()["id"]
                        c.execute("""
                            INSERT INTO sensors(machine_id,machine_code,temperature,vibration,pressure,rpm,timestamp)
                            VALUES(?,?,?,?,?,?,?)
                        """, (machine_id, machine_code, 25.0, 0.2, 5.8, 0, now()))
                        c.execute("""
                            INSERT INTO shift_machine_assignments(shift_name,machine_code,operator_name,active)
                            VALUES(?,?,?,1)
                            ON CONFLICT(shift_name,machine_code) DO UPDATE SET operator_name=excluded.operator_name,active=1
                        """, (new_shift, machine_code, new_operator))
                        c.execute("""
                            INSERT INTO maintenance(machine_id,machine_code,maintenance_type,description,maintenance_date,next_date,technician,status)
                            VALUES(?,?,?,?,?,?,?,?)
                        """, (machine_id, machine_code, "Periyodik Bakım", "Yeni makine başlangıç kontrolü", str(date.today()), str(new_next_maintenance), "Teknik Servis", "Planlandı"))
                        c.commit()
                        c.close()
                        audit_event("Yeni makine ekledi", machine_code, f"{new_shift} · {new_operator}")
                        st.success(f"{machine_code} sisteme eklendi. İlk sensör ve bakım kayıtları oluşturuldu.")
                        st.rerun()
                    except INTEGRITY_ERRORS:
                        st.error("Bu makine kodu zaten kullanılıyor.")

        with st.expander("Makineyi kalıcı olarak kaldır", expanded=False):
            st.warning("Bu işlem seçilen makinenin sensör, enerji, üretim, alarm, iş emri, bakım ve vardiya kayıtlarını kalıcı olarak siler.")
            remove_machine = st.selectbox("Kaldırılacak makine", df["machine_code"].tolist(), key="remove_machine")
            remove_confirmation = st.text_input("Onay için makine kodunu yazın", key="remove_machine_confirmation")
            remove_approved = st.checkbox("Bu silme işleminin geri alınamayacağını onaylıyorum.", key="remove_machine_approved")
            if st.button("🗑️ Makineyi ve ilişkili kayıtları sil", type="secondary", use_container_width=True):
                if not remove_approved or remove_confirmation.strip().upper() != remove_machine:
                    st.error("Silme işlemi için onay kutusunu işaretleyip doğru makine kodunu yazın.")
                else:
                    c = conn()
                    try:
                        alarm_rows = c.execute("SELECT id FROM alarms WHERE machine_code=?", (remove_machine,)).fetchall()
                        for alarm_row in alarm_rows:
                            c.execute("DELETE FROM alarm_actions WHERE alarm_id=?", (alarm_row["id"],))
                        c.execute("DELETE FROM alarms WHERE machine_code=?", (remove_machine,))
                        for table_name in ("production_history", "downtime", "sensors", "sensor_history", "quality", "maintenance", "energy_readings", "work_orders"):
                            c.execute(f"DELETE FROM {table_name} WHERE machine_code=?", (remove_machine,))
                        c.execute("DELETE FROM shift_machine_assignments WHERE machine_code=?", (remove_machine,))
                        c.execute("DELETE FROM maintenance_requests WHERE machine_code=?", (remove_machine,))
                        c.execute("DELETE FROM machines WHERE machine_code=?", (remove_machine,))
                        c.commit()
                        c.close()
                        audit_event("Makineyi kalıcı olarak sildi", remove_machine, "İlişkili operasyon kayıtlarıyla birlikte kaldırıldı")
                        st.success(f"{remove_machine} ve ilişkili kayıtları sistemden kaldırıldı.")
                        st.rerun()
                    except Exception as error:
                        c.close()
                        st.error(f"Makine silinirken hata oluştu: {error}")


    # =========================================================
    # ÜRETİM
    # =========================================================


if selected_module == "📈 Üretim":
    from production_panel import render_production_panel

    render_production_panel(
        q,
        conn,
        audit_event,
        df,
        selected_shift_context,
        can_edit=has_role("admin", "operator"),
    )


# Önceki üretim ekranı geçiş sürecinde referans olarak korunuyor; çalıştırılmaz.
if selected_module == "__legacy_production":
    st.subheader("📈 Üretim Takibi")
    st.caption(f"Vardiya bağlamı: {selected_shift_context if selected_shift_context != 'Tümü' else 'Tüm vardiyalar'}")

    production_orders = q("SELECT order_no,machine_code,product,target,produced,priority,status,due_date FROM work_orders ORDER BY id DESC")
    filter_date, filter_machine, filter_shift, filter_product, filter_order = st.columns(5)
    with filter_date:
        production_date_range = auto_date_range_filter("Tarih aralığı", "production_date_range")
    with filter_machine:
        production_machine = st.selectbox(
            "Makine filtresi", ["Tümü"] + sorted(df["machine_code"].unique()),
            key="production_machine_filter"
        )
    with filter_shift:
        shift_options = ["Tümü", "Sabah", "Akşam", "Gece"]
        default_shift = selected_shift_context if selected_shift_context in SHIFT_SCHEDULE else "Tümü"
        production_shift = st.selectbox(
            "Vardiya filtresi", shift_options, index=shift_options.index(default_shift),
            key="production_shift_filter"
        )
    with filter_product:
        production_product = st.selectbox(
            "Ürün filtresi", ["Tümü"] + sorted(df["product"].fillna("Atama bekliyor").unique()),
            key="production_product_filter"
        )
    with filter_order:
        production_order = st.selectbox(
            "İş emri", ["Tümü"] + production_orders["order_no"].dropna().tolist(),
            key="production_order_filter"
        )

    production_view = df.copy()
    # Üretim toplamı yalnız vardiya etiketli gerçek üretim artışlarından gelir.
    # Böylece seçilmemiş/çalışılmamış vardiya için üretim sıfır görünür.
    shift_totals = shift_production_totals()
    if production_shift != "Tümü":
        recorded_totals = shift_totals[shift_totals["Vardiya"] == production_shift]
        assignment = q("SELECT machine_code,operator_name FROM shift_machine_assignments WHERE shift_name=? AND active=1", (production_shift,))
        operator_map = assignment.set_index("machine_code")["operator_name"].to_dict() if not assignment.empty else {}
    else:
        recorded_totals = shift_totals.groupby("Makine", as_index=False)["Üretim"].sum() if not shift_totals.empty else pd.DataFrame(columns=["Makine", "Üretim"])
        operator_map = {}
    production_map = recorded_totals.set_index("Makine")["Üretim"].to_dict() if not recorded_totals.empty else {}
    production_view["production"] = production_view["machine_code"].map(production_map).fillna(0).astype(int)
    if production_shift != "Tümü":
        production_view["shift"] = production_shift
        production_view["operator"] = production_view["machine_code"].map(operator_map).fillna("Atama yok")
    if production_machine != "Tümü":
        production_view = production_view[production_view["machine_code"] == production_machine]
    if production_product != "Tümü":
        production_view = production_view[production_view["product"] == production_product]
    if production_order != "Tümü":
        selected_order = production_orders[production_orders["order_no"] == production_order].iloc[0]
        production_view = production_view[production_view["machine_code"] == selected_order["machine_code"]]

    production_total = int(production_view["production"].sum()) if not production_view.empty else 0
    production_target = int(production_view["target"].sum()) if not production_view.empty else 0
    production_ratio = production_total / max(production_target, 1) * 100
    production_oee = float(production_view["oee"].mean() * 100) if not production_view.empty else 0.0
    production_kpis = st.columns(6)
    production_kpis[0].metric("Toplam Üretim", f"{production_total:,} adet")
    production_kpis[1].metric("Toplam Hedef", f"{production_target:,} adet")
    production_kpis[2].metric("Gerçekleşme", f"%{production_ratio:.1f}")
    production_kpis[3].metric("Kalan Üretim", f"{max(production_target-production_total, 0):,} adet")
    production_kpis[4].metric("Ortalama OEE", f"%{production_oee:.1f}")
    production_kpis[5].metric("Açık İş Emri", int((production_orders["status"] != "Tamamlandı").sum()))

    if production_shift != "Tümü" and recorded_totals.empty:
        st.info(f"{production_shift} vardiyası için henüz vardiya etiketli üretim kaydı yok; gerçekleşen üretim 0 gösteriliyor.")

    production_chart = px.bar(
        production_view,
        x="machine_code",
        y=["production", "target"],
        barmode="group",
        title="Hedef - Gerçekleşen Üretim"
    )

    st.plotly_chart(
        production_chart,
        use_container_width=True
    )

    history = q("""
        SELECT machine_code,quantity,timestamp,shift
        FROM production_history
        WHERE shift IS NOT NULL AND shift != ''
        ORDER BY timestamp
    """)
    if not history.empty:
        history["timestamp"] = pd.to_datetime(history["timestamp"], errors="coerce")
        history = history.dropna(subset=["timestamp"])
        history = history.sort_values(["machine_code", "timestamp"])
        history["Üretim Artışı"] = history.groupby("machine_code")["quantity"].diff().fillna(0).clip(lower=0)
        production_start, production_end = date_range_bounds(production_date_range)
        history = history[(history["timestamp"].dt.date >= production_start) & (history["timestamp"].dt.date <= production_end)].copy()
        if production_shift != "Tümü":
            history = history[history["shift"] == production_shift].copy()

    if not history.empty:
        available_history_machines = sorted(history["machine_code"].unique())
        selected_machine = (
            production_machine if production_machine in available_history_machines
            else st.selectbox("Geçmiş makinesi", available_history_machines, key="production_history_machine")
        )
        history = history[history["machine_code"] == selected_machine].copy()

        chart = px.bar(
            history,
            x="timestamp",
            y="Üretim Artışı",
            color="shift" if production_shift == "Tümü" else None,
            title=f"{selected_machine} Vardiya Bazlı Üretim Geçmişi"
        )

        st.plotly_chart(
            chart,
            use_container_width=True
        )
    else:
        st.info(
            "Üretim geçmişi için simülasyon çalıştır."
        )

    st.markdown("#### Üretim planı")
    plan_view = production_orders.copy()
    if production_machine != "Tümü":
        plan_view = plan_view[plan_view["machine_code"] == production_machine]
    if production_product != "Tümü":
        plan_view = plan_view[plan_view["product"] == production_product]
    if production_order != "Tümü":
        plan_view = plan_view[plan_view["order_no"] == production_order]
    if plan_view.empty:
        st.info("Seçilen filtreler için iş emri bulunamadı.")
    else:
        plan_view["Kalan"] = (plan_view["target"] - plan_view["produced"]).clip(lower=0)
        plan_view["Gerçekleşme %"] = (plan_view["produced"] / plan_view["target"].replace(0, 1) * 100).clip(0, 100).round(1)
        plan_view["Durum"] = plan_view.apply(
            lambda row: "✅ Tamamlandı" if row["status"] == "Tamamlandı" else ("🔴 Gecikti" if pd.notna(pd.to_datetime(row["due_date"], errors="coerce")) and pd.to_datetime(row["due_date"], errors="coerce").date() < date.today() else ("🔵 Sırada" if row["status"] == "Sırada" else "🟢 Devam ediyor")),
            axis=1
        )
        plan_view = plan_view.rename(columns={"order_no":"İş Emri", "machine_code":"Makine", "product":"Ürün", "target":"Hedef", "produced":"Üretim", "priority":"Öncelik", "due_date":"Termin"})
        st.dataframe(plan_view[["İş Emri", "Makine", "Ürün", "Hedef", "Üretim", "Kalan", "Gerçekleşme %", "Öncelik", "Durum", "Termin"]], use_container_width=True, hide_index=True, height=250)


    # =========================================================
    # İŞGÜCÜ OLE
    # =========================================================


if selected_module == "👥 OLE":
    st.markdown("""
    <style>
    .ole-formula{border:1px solid #cfe9da;border-radius:10px;background:linear-gradient(135deg,#f5fcf8,#eaf8f0);padding:12px 16px;color:#164f38;margin:2px 0 12px}.ole-formula b{font-size:.92rem}.ole-formula span{display:block;color:#648274;font-size:.72rem;margin-top:4px}.ole-title{font-size:1.12rem;font-weight:900;color:#0d4933}.ole-subtitle{font-size:.76rem;color:#668276;margin:-2px 0 12px}
    </style>
    <div class="ole-title">İşgücü OLE Analizi</div>
    <div class="ole-subtitle">Operatör ve vardiya bazında işgücü etkinliğini mevcut MES kayıtlarından izleyin.</div>
    <div class="ole-formula"><b>OLE = İşgücü Kullanılabilirliği × Performans × Kalite</b><span>Kullanılabilirlik operatör/personel/mola kaynaklı kayıpları; performans ideal çevrim süresini; kalite ise sağlam üretimi kullanır.</span></div>
    """, unsafe_allow_html=True)

    ole_filter_1, ole_filter_2, ole_filter_3 = st.columns(3, gap="small")
    with ole_filter_1:
        ole_shift_options = ["Tümü"] + sorted(labor_metrics["Vardiya"].dropna().astype(str).unique().tolist()) if not labor_metrics.empty else ["Tümü"]
        ole_shift_filter = st.selectbox("Vardiya", ole_shift_options, key="ole_shift_filter")
    with ole_filter_2:
        ole_operator_options = ["Tümü"] + sorted(labor_metrics["Operatör"].dropna().astype(str).unique().tolist()) if not labor_metrics.empty else ["Tümü"]
        ole_operator_filter = st.selectbox("Operatör", ole_operator_options, key="ole_operator_filter")
    with ole_filter_3:
        ole_status_filter = st.selectbox("Durum", ["Tümü", "İyi", "Takip", "Riskli", "Kritik"], key="ole_status_filter")

    ole_view = labor_metrics.copy()
    if ole_shift_filter != "Tümü":
        ole_view = ole_view[ole_view["Vardiya"] == ole_shift_filter]
    if ole_operator_filter != "Tümü":
        ole_view = ole_view[ole_view["Operatör"] == ole_operator_filter]
    if ole_status_filter != "Tümü":
        ole_view = ole_view[ole_view["Durum"] == ole_status_filter]

    if ole_view.empty:
        st.info("Seçilen filtrelerde OLE hesaplanabilecek operatör kaydı bulunamadı.")
    else:
        ole_weights = ole_view["Planlı Süre"].clip(lower=0)
        ole_weight_total = max(float(ole_weights.sum()), 1.0)
        ole_availability_value = float((ole_view["Kullanılabilirlik"] * ole_weights).sum() / ole_weight_total)
        ole_performance_value = float((ole_view["Performans"] * ole_weights).sum() / ole_weight_total)
        ole_quality_value = float(ole_view["Sağlam Üretim"].sum() / max(float(ole_view["Üretim"].sum()), 1.0) * 100)
        ole_value = ole_availability_value * ole_performance_value * ole_quality_value / 10000
        ole_output_per_person = float(ole_view["Üretim"].sum() / max(ole_view["Operatör"].nunique(), 1))

        ole_kpis = st.columns(6, gap="small")
        ole_kpis[0].metric("Tahmini OLE", f"%{ole_value:.1f}")
        ole_kpis[1].metric("Kullanılabilirlik", f"%{ole_availability_value:.1f}")
        ole_kpis[2].metric("Performans", f"%{ole_performance_value:.1f}")
        ole_kpis[3].metric("Kalite", f"%{ole_quality_value:.1f}")
        ole_kpis[4].metric("İşgücü Kaybı", f"{ole_view['İşgücü Kaybı'].sum():.0f} dk")
        ole_kpis[5].metric("Kişi Başı Üretim", f"{ole_output_per_person:,.0f}")

        ole_left, ole_right = st.columns([1.25, 1], gap="small")
        with ole_left:
            with st.container(border=True):
                st.markdown("#### Operatör Bazında OLE")
                ole_chart_frame = ole_view.sort_values("OLE", ascending=True)
                ole_operator_chart = px.bar(ole_chart_frame, x="OLE", y="Operatör", color="Durum", orientation="h", text=ole_chart_frame["OLE"].map(lambda value: f"%{value:.1f}"), color_discrete_map={"İyi":"#0a9b58", "Takip":"#35a7e8", "Riskli":"#f2ad2d", "Kritik":"#e94b4b"})
                ole_operator_chart.add_vline(x=85, line_dash="dash", line_color="#0a9b58", annotation_text="Hedef %85")
                ole_operator_chart.update_layout(height=285, margin=dict(l=0,r=8,t=8,b=0), legend=dict(orientation="h", y=1.12), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", xaxis=dict(title=None, range=[0,100]), yaxis_title=None)
                st.plotly_chart(ole_operator_chart, use_container_width=True, config={"displayModeBar":False}, key="ole_operator_chart")
        with ole_right:
            with st.container(border=True):
                st.markdown("#### OLE Bileşenleri")
                ole_components = pd.DataFrame({"Bileşen": ["Kullanılabilirlik", "Performans", "Kalite", "OLE"], "Oran": [ole_availability_value, ole_performance_value, ole_quality_value, ole_value]})
                ole_component_chart = px.bar(ole_components, x="Bileşen", y="Oran", text=ole_components["Oran"].map(lambda value: f"%{value:.1f}"), color="Bileşen", color_discrete_sequence=["#1ea76a", "#3ba8df", "#8ccf58", "#087847"])
                ole_component_chart.update_layout(height=285, margin=dict(l=0,r=0,t=8,b=0), showlegend=False, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", xaxis_title=None, yaxis=dict(title=None, range=[0,100]))
                st.plotly_chart(ole_component_chart, use_container_width=True, config={"displayModeBar":False}, key="ole_component_chart")

        ole_table_left, ole_loss_right = st.columns([1.45, 1], gap="small")
        with ole_table_left:
            with st.container(border=True):
                st.markdown("#### Operatör Detayları")
                ole_table = ole_view.copy()
                for ole_percent_column in ["Kullanılabilirlik", "Performans", "Kalite", "OLE"]:
                    ole_table[ole_percent_column] = ole_table[ole_percent_column].map(lambda value: f"%{value:.1f}")
                ole_table["Planlı Süre"] = ole_table["Planlı Süre"].map(lambda value: f"{value:.0f} dk")
                ole_table["İşgücü Kaybı"] = ole_table["İşgücü Kaybı"].map(lambda value: f"{value:.0f} dk")
                st.dataframe(ole_table, use_container_width=True, hide_index=True, height=235)
        with ole_loss_right:
            with st.container(border=True):
                st.markdown("#### İşgücü Kayıp Nedenleri")
                ole_loss_rows = q("SELECT reason,duration,event_at FROM downtime ORDER BY id DESC")
                if not ole_loss_rows.empty:
                    ole_loss_dated = filter_by_date_range(ole_loss_rows[ole_loss_rows["event_at"].notna()].copy(), "event_at")
                    ole_loss_rows = pd.concat([ole_loss_dated, ole_loss_rows[ole_loss_rows["event_at"].isna()].copy()], ignore_index=True)
                    ole_loss_mask = ole_loss_rows["reason"].fillna("").astype(str).str.lower().str.contains("operatör|operator|personel|mola|işgücü|iscilik|işçilik", regex=True)
                    ole_loss_summary = ole_loss_rows[ole_loss_mask].groupby("reason", as_index=False)["duration"].sum().sort_values("duration", ascending=True)
                else:
                    ole_loss_summary = pd.DataFrame()
                if ole_loss_summary.empty:
                    st.success("Seçili tarihlerde kayıtlı işgücü kaybı yok.")
                else:
                    ole_loss_chart = px.bar(ole_loss_summary, x="duration", y="reason", orientation="h", text_auto=".0f", color_discrete_sequence=["#efad32"])
                    ole_loss_chart.update_layout(height=235, margin=dict(l=0,r=0,t=5,b=0), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", xaxis_title="Dakika", yaxis_title=None)
                    st.plotly_chart(ole_loss_chart, use_container_width=True, config={"displayModeBar":False}, key="ole_loss_chart")

        st.info("OLE şu anda MES'teki makine ataması, üretim, ideal çevrim, hata ve operatör kaynaklı duruş kayıtlarından tahmini hesaplanır. Personel kartı/turnike ve mola verileri bağlandığında resmi işgücü OLE göstergesine dönüştürülebilir.")


    # =========================================================
    # MANUEL OEE
    # =========================================================


if selected_module == "🧮 Manuel OEE":
    st.subheader("🧮 Manuel OEE Hesaplama")

    left, right = st.columns(2)

    with left:
        planned_manual = st.number_input(
            "Planlanan Süre (dk)",
            min_value=1.0,
            value=480.0
        )

        downtime_manual = st.number_input(
            "Duruş (dk)",
            min_value=0.0,
            value=30.0
        )

        cycle_manual = st.number_input(
            "İdeal Çevrim (sn/adet)",
            min_value=0.01,
            max_value=30.0,
            value=0.50
        )

    with right:
        production_manual = st.number_input(
            "Toplam Üretim",
            min_value=0,
            value=850
        )

        defective_manual = st.number_input(
            "Hatalı Ürün",
            min_value=0,
            value=20
        )

    operating_manual = max(
        planned_manual - downtime_manual,
        0
    )

    availability_manual = (
        operating_manual / planned_manual
        if planned_manual > 0
        else 0
    )

    performance_manual = (
        cycle_manual * production_manual
        / (operating_manual * 60)
        if operating_manual > 0
        else 0
    )

    performance_manual = min(
        max(performance_manual, 0),
        1
    )

    quality_manual = (
        (production_manual - defective_manual)
        / production_manual
        if production_manual > 0
        else 0
    )

    quality_manual = min(
        max(quality_manual, 0),
        1
    )

    oee_manual = (
        availability_manual
        * performance_manual
        * quality_manual
    )

    a, b, c, d = st.columns(4)

    a.metric(
        "Kullanılabilirlik",
        f"{availability_manual * 100:.1f}%"
    )

    b.metric(
        "Performans",
        f"{performance_manual * 100:.1f}%"
    )

    c.metric(
        "Kalite",
        f"{quality_manual * 100:.1f}%"
    )

    d.metric(
        "OEE",
        f"{oee_manual * 100:.1f}%"
    )

    st.markdown("---")
    st.markdown("#### Canlı OEE analizi")
    live_oee = df[["machine_code", "availability", "performance", "quality", "oee"]].copy()
    for metric_name in ["availability", "performance", "quality", "oee"]:
        live_oee[metric_name] = (live_oee[metric_name].clip(0, 1) * 100).round(1)
    live_metrics = st.columns(4)
    live_metrics[0].metric("Genel OEE", f"%{live_oee['oee'].mean():.1f}")
    live_metrics[1].metric("Kullanılabilirlik", f"%{live_oee['availability'].mean():.1f}")
    live_metrics[2].metric("Performans", f"%{live_oee['performance'].mean():.1f}")
    live_metrics[3].metric("Kalite", f"%{live_oee['quality'].mean():.1f}")

    oee_chart_col, reliability_col = st.columns([1.2, 1])
    with oee_chart_col:
        oee_long = live_oee.melt(id_vars="machine_code", value_vars=["availability", "performance", "quality", "oee"], var_name="Bileşen", value_name="Yüzde")
        oee_long["Bileşen"] = oee_long["Bileşen"].map({"availability":"Kullanılabilirlik", "performance":"Performans", "quality":"Kalite", "oee":"OEE"})
        oee_chart = px.bar(oee_long, x="machine_code", y="Yüzde", color="Bileşen", barmode="group", title="Makine Karşılaştırması · Mevcut OEE Formülü")
        oee_chart.update_layout(height=300, yaxis_range=[0, 100], xaxis_title="Makine", yaxis_title="Yüzde")
        st.plotly_chart(oee_chart, use_container_width=True, config={"displayModeBar": False}, key="oee_live_comparison")
    with reliability_col:
        reliability = reliability_metrics(df)
        reliability_view = reliability[["Makine", "Arıza Sayısı", "MTBF (dk)", "MTTR (dk)"]].copy()
        reliability_view[["MTBF (dk)", "MTTR (dk)"]] = reliability_view[["MTBF (dk)", "MTTR (dk)"]].fillna(0).round(1)
        st.markdown("##### Güvenilirlik analizi")
        st.dataframe(reliability_view, use_container_width=True, hide_index=True, height=250)

    oee_history = q("SELECT machine_code,quantity,timestamp FROM production_history ORDER BY timestamp DESC LIMIT 1000")
    if not oee_history.empty:
        oee_history["timestamp"] = pd.to_datetime(oee_history["timestamp"], errors="coerce")
        oee_history = oee_history.dropna(subset=["timestamp"])
        oee_history = filter_by_date_range(oee_history, "timestamp")
        if not oee_history.empty:
            # Geçmiş kayıtlarda bileşen anlık snapshot olarak tutulmadığı için, mevcut
            # makine OEE'siyle yalnızca üretim zaman ekseni birlikte gösterilir.
            oee_history = oee_history.merge(live_oee[["machine_code", "oee"]], on="machine_code", how="left")
            daily_oee = oee_history.groupby([oee_history["timestamp"].dt.date, "machine_code"], as_index=False)["oee"].mean().rename(columns={"timestamp":"Tarih", "oee":"OEE"})
            if not daily_oee.empty:
                history_chart = px.line(daily_oee, x="Tarih", y="OEE", color="machine_code", markers=True, title="Seçilen Tarih Aralığında OEE Referans Trendleri")
                history_chart.update_layout(height=260, yaxis_range=[0, 100], yaxis_title="OEE (%)")
                st.plotly_chart(history_chart, use_container_width=True, config={"displayModeBar": False}, key="oee_history_reference")


    # =========================================================
    # ALARMLAR
    # =========================================================


if selected_module == "🚨 Alarmlar":
    # Alarm merkezi: operatörün hızlı aksiyon alabilmesi için tek ekranda özet,
    # aktif kayıtlar, analizler ve geçmiş birlikte gösterilir.
    st.markdown("""
    <style>
      .alarm-v3-title{display:flex;align-items:center;gap:9px;margin:0 0 .25rem}
      .alarm-v3-title h2{font-size:1.35rem;margin:0;color:#063b2a;font-weight:800}
      .alarm-v3-title p{margin:0;color:#6b8177;font-size:.78rem}
      .alarm-v3-stat{min-height:78px;border:1px solid #dceee6;border-radius:9px;background:#fff;
        padding:11px 14px;box-shadow:0 3px 11px rgba(14,79,52,.06);position:relative;overflow:hidden}
      .alarm-v3-stat:before{content:'';position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--alarm-color)}
      .alarm-v3-stat .stat-top{display:flex;align-items:center;gap:8px;color:#49645a;font-size:.72rem;font-weight:700}
      .alarm-v3-stat .stat-icon{height:26px;width:26px;display:inline-flex;align-items:center;justify-content:center;
        color:white;border-radius:50%;background:var(--alarm-color);font-size:.82rem}
      .alarm-v3-stat strong{display:block;color:#073d2a;font-size:1.5rem;line-height:1.25;margin:3px 0 0 34px}
      .alarm-v3-stat small{position:absolute;right:12px;bottom:12px;color:var(--alarm-color);font-size:.62rem;font-weight:700}
      .alarm-v3-panel-title{font-size:.9rem;color:#0a4933;font-weight:800;margin:0 0 .55rem}
      .alarm-v3-note{font-size:.72rem;color:#71877b;margin:-.2rem 0 .55rem}
      .alarm-v3-legend{display:grid;grid-template-columns:1fr 1fr;gap:5px 10px;margin:0 0 2px}
      .alarm-v3-legend span{font-size:.69rem;color:#49645a;white-space:nowrap}
      .alarm-v3-legend b{color:#093f2d;font-size:.76rem}
      div[data-testid="stHorizontalBlock"] .alarm-v3-stat{width:100%}
    </style>
    """, unsafe_allow_html=True)
    st.markdown("""
      <div class="alarm-v3-title">
        <span style="font-size:1.25rem">♟</span>
        <div><h2>Alarm Yönetim Merkezi</h2><p>Aktif alarmları izleyin, sorumlu atayın ve müdahaleyi kaydedin.</p></div>
      </div>
    """, unsafe_allow_html=True)

    alarms_v3 = q("""
        SELECT a.id,a.machine_id,a.machine_code,a.alarm,a.level,a.time,a.acknowledged,
               COALESCE(x.assignee, 'Atanmadı') AS sorumlu,
               COALESCE(x.status, CASE WHEN a.acknowledged=1 THEN 'Çözüldü' ELSE 'Aktif' END) AS mudahale_durumu,
               COALESCE(x.note, '') AS mudahale_notu
        FROM alarms a LEFT JOIN alarm_actions x ON x.alarm_id=a.id
        ORDER BY a.id DESC LIMIT 300
    """)
    if alarms_v3.empty:
        alarms_v3 = pd.DataFrame(columns=["id", "machine_id", "machine_code", "alarm", "level", "time", "acknowledged", "sorumlu", "mudahale_durumu", "mudahale_notu"])
    alarms_v3["_zaman"] = pd.to_datetime(alarms_v3["time"], errors="coerce")
    alarms_v3["_tarih"] = alarms_v3["_zaman"].dt.date
    alarms_v3["_acik"] = ~alarms_v3["acknowledged"].astype(str).str.lower().isin(["1", "true", "t", "yes"])

    # Referanstaki ince filtre satırı: tarih aralığı varsayılan olarak bugünü gösterir.
    filter_machine, filter_level, filter_status, filter_date, filter_search = st.columns([1.1, .9, .95, 1.35, 1.25])
    machine_options_v3 = ["Tüm Makineler"] + sorted(alarms_v3["machine_code"].dropna().astype(str).unique().tolist())
    with filter_machine:
        selected_alarm_machine = st.selectbox("Makine", machine_options_v3, key="alarm_v3_machine")
    with filter_level:
        selected_alarm_level = st.selectbox("Seviye", ["Tümü", "Kritik", "Uyarı", "Bilgi"], key="alarm_v3_level")
    with filter_status:
        selected_alarm_status = st.selectbox("Durum", ["Aktif", "Çözülen", "Tümü"], key="alarm_v3_status")
    with filter_date:
        selected_alarm_dates = st.date_input("Tarih aralığı", value=(date.today(), date.today()), key="alarm_v3_dates")
    with filter_search:
        alarm_search_v3 = st.text_input("Alarm ara", placeholder="Makine veya alarm ara...", key="alarm_v3_search")

    filtered_alarms_v3 = alarms_v3.copy()
    if selected_alarm_machine != "Tüm Makineler":
        filtered_alarms_v3 = filtered_alarms_v3[filtered_alarms_v3["machine_code"].astype(str) == selected_alarm_machine]
    if selected_alarm_level != "Tümü":
        filtered_alarms_v3 = filtered_alarms_v3[filtered_alarms_v3["level"] == selected_alarm_level]
    if selected_alarm_status == "Aktif":
        filtered_alarms_v3 = filtered_alarms_v3[filtered_alarms_v3["_acik"]]
    elif selected_alarm_status == "Çözülen":
        filtered_alarms_v3 = filtered_alarms_v3[~filtered_alarms_v3["_acik"]]
    if isinstance(selected_alarm_dates, (tuple, list)) and len(selected_alarm_dates) == 2:
        alarm_date_start, alarm_date_end = selected_alarm_dates
    else:
        alarm_date_start = alarm_date_end = selected_alarm_dates
    if alarm_date_start and alarm_date_end:
        filtered_alarms_v3 = filtered_alarms_v3[
            (filtered_alarms_v3["_tarih"] >= alarm_date_start) & (filtered_alarms_v3["_tarih"] <= alarm_date_end)
        ]
    if alarm_search_v3.strip():
        needle_v3 = alarm_search_v3.strip().lower()
        searchable_v3 = filtered_alarms_v3["machine_code"].fillna("").astype(str) + " " + filtered_alarms_v3["alarm"].fillna("").astype(str)
        filtered_alarms_v3 = filtered_alarms_v3[searchable_v3.str.lower().str.contains(needle_v3, na=False)]

    active_alarms_v3 = filtered_alarms_v3[filtered_alarms_v3["_acik"]].copy()
    resolved_alarms_v3 = filtered_alarms_v3[~filtered_alarms_v3["_acik"]].copy()
    critical_alarms_v3 = int((active_alarms_v3["level"] == "Kritik").sum())
    warning_alarms_v3 = int((active_alarms_v3["level"] == "Uyarı").sum())
    resolved_count_v3 = int(len(resolved_alarms_v3))
    total_count_v3 = int(len(filtered_alarms_v3))

    stat_cards_v3 = st.columns(4)
    stats_v3 = [
        ("▲", "Kritik Alarm", critical_alarms_v3, "#ef4444", "Acil müdahale"),
        ("!", "Uyarı Alarm", warning_alarms_v3, "#f59e0b", "Takip gerekli"),
        ("✓", "Çözülen Alarm", resolved_count_v3, "#10a566", "Kayıt kapatıldı"),
        ("≡", "Toplam Alarm", total_count_v3, "#7b8d96", "Seçili dönemde"),
    ]
    for stat_col_v3, (stat_icon_v3, stat_title_v3, stat_value_v3, stat_color_v3, stat_hint_v3) in zip(stat_cards_v3, stats_v3):
        with stat_col_v3:
            st.markdown(
                f'<div class="alarm-v3-stat" style="--alarm-color:{stat_color_v3}"><div class="stat-top"><span class="stat-icon">{stat_icon_v3}</span>{stat_title_v3}</div><strong>{stat_value_v3}</strong><small>{stat_hint_v3}</small></div>',
                unsafe_allow_html=True,
            )

    main_alarm_col_v3, analysis_alarm_col_v3 = st.columns([2.15, 1], gap="small")
    with main_alarm_col_v3:
        with st.container(border=True):
            st.markdown('<div class="alarm-v3-panel-title">♟ Aktif Alarmlar <span style="color:#e53d43;font-size:.72rem">● {}</span></div>'.format(len(active_alarms_v3)), unsafe_allow_html=True)
            st.markdown('<div class="alarm-v3-note">Seçili tarih aralığındaki, henüz kapatılmamış kayıtlar.</div>', unsafe_allow_html=True)
            if active_alarms_v3.empty:
                st.success("Bu filtrede aktif alarm bulunmuyor.")
            else:
                active_alarm_view_v3 = active_alarms_v3[["level", "machine_code", "alarm", "time", "sorumlu", "mudahale_durumu"]].copy()
                active_alarm_view_v3["level"] = active_alarm_view_v3["level"].map({"Kritik": "🔴 Kritik", "Uyarı": "🟠 Uyarı", "Bilgi": "🔵 Bilgi"}).fillna(active_alarm_view_v3["level"])
                active_alarm_view_v3["mudahale_durumu"] = active_alarm_view_v3["mudahale_durumu"].replace({"Bekliyor": "Aktif", "Atandı": "Müdahale"})
                active_alarm_view_v3 = active_alarm_view_v3.rename(columns={"level": "Seviye", "machine_code": "Makine", "alarm": "Alarm açıklaması", "time": "Oluşma zamanı", "sorumlu": "Sorumlu", "mudahale_durumu": "Durum"})
                st.dataframe(active_alarm_view_v3, use_container_width=True, hide_index=True, height=205)

            if not active_alarms_v3.empty:
                alarm_options_v3 = {int(row["id"]): f'{row["machine_code"]} · {row["alarm"]}' for _, row in active_alarms_v3.iterrows()}
                action_pick_v3, action_detail_v3, action_confirm_v3 = st.columns([2.4, .75, .85])
                with action_pick_v3:
                    selected_alarm_id_v3 = st.selectbox("İşlem yapılacak aktif alarm", list(alarm_options_v3), format_func=lambda item: alarm_options_v3[item], label_visibility="collapsed", key="alarm_v3_selected")
                with action_detail_v3:
                    if st.button("Detay", key="alarm_v3_detail_button", use_container_width=True):
                        st.session_state["alarm_v3_detail_open"] = True
                with action_confirm_v3:
                    if st.button("Onayla", key="alarm_v3_ack_button", type="primary", use_container_width=True):
                        previous_alarm_action_v3 = q("SELECT assignee,note,assigned_at,resolved_at,status FROM alarm_actions WHERE alarm_id=?", (int(selected_alarm_id_v3),))
                        execute("UPDATE alarms SET acknowledged=1 WHERE id=?", (int(selected_alarm_id_v3),))
                        execute("INSERT INTO alarm_actions(alarm_id,assignee,note,assigned_at,resolved_at,status) VALUES(?,?,?,?,?,?) ON CONFLICT(alarm_id) DO UPDATE SET assigned_at=excluded.assigned_at,resolved_at=excluded.resolved_at,status=excluded.status", (int(selected_alarm_id_v3), "Sistem onayı", "Alarm onaylandı", now(), now(), "Onaylandı"))
                        alarm_undo_operations_v3 = [("UPDATE alarms SET acknowledged=0 WHERE id=?", (int(selected_alarm_id_v3),))]
                        if previous_alarm_action_v3.empty:
                            alarm_undo_operations_v3.append(("DELETE FROM alarm_actions WHERE alarm_id=?", (int(selected_alarm_id_v3),)))
                        else:
                            previous_action_v3 = previous_alarm_action_v3.iloc[0]
                            alarm_undo_operations_v3.append((
                                "INSERT INTO alarm_actions(alarm_id,assignee,note,assigned_at,resolved_at,status) VALUES(?,?,?,?,?,?) ON CONFLICT(alarm_id) DO UPDATE SET assignee=excluded.assignee,note=excluded.note,assigned_at=excluded.assigned_at,resolved_at=excluded.resolved_at,status=excluded.status",
                                (int(selected_alarm_id_v3), previous_action_v3["assignee"], previous_action_v3["note"], previous_action_v3["assigned_at"], previous_action_v3["resolved_at"], previous_action_v3["status"])
                            ))
                        register_undo(f"Alarm #{int(selected_alarm_id_v3)} onaylandı", alarm_undo_operations_v3)
                        audit_event("Alarmı onayladı", f"Alarm #{int(selected_alarm_id_v3)}", "Alarm merkezi hızlı onay")
                        st.success("Alarm onaylandı.")
                        st.rerun()

                if st.session_state.get("alarm_v3_detail_open", False):
                    selected_alarm_row_v3 = active_alarms_v3[active_alarms_v3["id"] == int(selected_alarm_id_v3)].iloc[0]
                    with st.expander("Seçili alarm müdahalesi", expanded=True):
                        info_left_v3, info_right_v3 = st.columns([1, 1.55])
                        with info_left_v3:
                            st.caption("ALARM DETAYI")
                            st.markdown(f"**{selected_alarm_row_v3['machine_code']}** · {selected_alarm_row_v3['level']}")
                            st.write(selected_alarm_row_v3["alarm"])
                            st.caption(f"Oluşma: {selected_alarm_row_v3['time']}")
                        with info_right_v3:
                            if has_role("admin", "maintenance"):
                                assignee_list_v3 = q("SELECT full_name FROM users WHERE is_active=1 ORDER BY full_name")["full_name"].dropna().tolist()
                                with st.form("alarm_v3_action_form"):
                                    assignee_v3 = st.selectbox("Sorumlu", assignee_list_v3 or ["Atanmadı"], key="alarm_v3_assignee")
                                    note_v3 = st.text_input("Müdahale notu", value=str(selected_alarm_row_v3["mudahale_notu"] or ""), key="alarm_v3_note")
                                    resolve_v3 = st.checkbox("Alarm çözüldü", key="alarm_v3_resolve")
                                    failure_v3 = st.checkbox("MTTR hesabına arıza olarak ekle", key="alarm_v3_failure", help="Çözüm süresini arıza/duruş kaydına ekler.")
                                    if st.form_submit_button("Müdahaleyi kaydet", type="primary", use_container_width=True):
                                        execute("INSERT INTO alarm_actions(alarm_id,assignee,note,assigned_at,resolved_at,status) VALUES(?,?,?,?,?,?) ON CONFLICT(alarm_id) DO UPDATE SET assignee=excluded.assignee,note=excluded.note,assigned_at=excluded.assigned_at,resolved_at=excluded.resolved_at,status=excluded.status", (int(selected_alarm_id_v3), assignee_v3, note_v3, now(), now() if resolve_v3 else None, "Çözüldü" if resolve_v3 else "Atandı"))
                                        if resolve_v3:
                                            if failure_v3:
                                                started_v3 = pd.to_datetime(selected_alarm_row_v3["time"], errors="coerce")
                                                repair_minutes_v3 = max(1.0, (datetime.now() - started_v3.to_pydatetime()).total_seconds() / 60) if pd.notna(started_v3) else 1.0
                                                already_logged_v3 = q("SELECT 1 FROM downtime WHERE notes=? LIMIT 1", (f"Alarm #{int(selected_alarm_id_v3)}",))
                                                if already_logged_v3.empty:
                                                    execute("INSERT INTO downtime(machine_id,machine_code,reason,duration,start_time,end_time,notes,event_at) VALUES(?,?,?,?,?,?,?,?)", (int(selected_alarm_row_v3["machine_id"]), selected_alarm_row_v3["machine_code"], "Alarm kaynaklı arıza", round(repair_minutes_v3, 1), str(selected_alarm_row_v3["time"]), now(), f"Alarm #{int(selected_alarm_id_v3)}", str(selected_alarm_row_v3["time"])))
                                                    execute("UPDATE machines SET downtime=downtime+? WHERE id=?", (round(repair_minutes_v3, 1), int(selected_alarm_row_v3["machine_id"])))
                                            execute("UPDATE alarms SET acknowledged=1 WHERE id=?", (int(selected_alarm_id_v3),))
                                        audit_event("Alarm müdahalesini kaydetti", f"Alarm #{int(selected_alarm_id_v3)}", "Çözüldü" if resolve_v3 else "Atandı")
                                        st.session_state["alarm_v3_detail_open"] = False
                                        st.rerun()
                            else:
                                st.info("Müdahale atama ve çözme işlemleri bakım veya yönetici yetkisi gerektirir.")

    with analysis_alarm_col_v3:
        with st.container(border=True):
            st.markdown('<div class="alarm-v3-panel-title">📊 Alarm Özeti</div>', unsafe_allow_html=True)
            summary_rows_v3 = pd.DataFrame({"Durum": ["Kritik", "Uyarı", "Bilgi", "Çözülen"], "Kayıt": [critical_alarms_v3, warning_alarms_v3, int((active_alarms_v3["level"] == "Bilgi").sum()), resolved_count_v3]})
            summary_rows_v3 = summary_rows_v3[summary_rows_v3["Kayıt"] > 0]
            if summary_rows_v3.empty:
                summary_rows_v3 = pd.DataFrame({"Durum": ["Kayıt yok"], "Kayıt": [1]})
            pie_v3 = px.pie(summary_rows_v3, names="Durum", values="Kayıt", hole=.68, color="Durum", color_discrete_map={"Kritik":"#ed6a6f", "Uyarı":"#eda93c", "Bilgi":"#5b9ed8", "Çözülen":"#36ad75", "Kayıt yok":"#d6e1dc"})
            pie_v3.update_traces(textinfo="none", hovertemplate="%{label}: %{value}<extra></extra>")
            pie_v3.add_annotation(text=f"<b>{total_count_v3}</b><br><span style='font-size:9px'>Toplam alarm</span>", x=.5, y=.5, showarrow=False, font=dict(color="#0a4933", size=14))
            pie_v3.update_layout(height=142, margin=dict(l=0, r=0, t=0, b=0), showlegend=False, paper_bgcolor="rgba(0,0,0,0)")
            st.plotly_chart(pie_v3, use_container_width=True, config={"displayModeBar": False}, key="alarm_v3_summary_pie")
            legend_colours_v3 = {"Kritik":"#ed6a6f", "Uyarı":"#eda93c", "Bilgi":"#5b9ed8", "Çözülen":"#36ad75", "Kayıt yok":"#93a69c"}
            legend_html_v3 = "".join(f'<span><i style="color:{legend_colours_v3.get(row_v3["Durum"], "#93a69c")};font-style:normal">●</i> {row_v3["Durum"]} <b>{int(row_v3["Kayıt"])}</b></span>' for _, row_v3 in summary_rows_v3.iterrows())
            st.markdown(f'<div class="alarm-v3-legend">{legend_html_v3}</div>', unsafe_allow_html=True)

        with st.container(border=True):
            st.markdown('<div class="alarm-v3-panel-title">🔔 En Çok Alarm Veren Makineler</div>', unsafe_allow_html=True)
            machine_alarm_counts_v3 = active_alarms_v3.groupby("machine_code").size().reset_index(name="Kayıt").sort_values("Kayıt", ascending=True).tail(4) if not active_alarms_v3.empty else pd.DataFrame(columns=["machine_code", "Kayıt"])
            if machine_alarm_counts_v3.empty:
                st.caption("Aktif alarm verisi yok.")
            else:
                bar_v3 = px.bar(machine_alarm_counts_v3, x="Kayıt", y="machine_code", orientation="h", text="Kayıt", color_discrete_sequence=["#e8a13b"])
                bar_v3.update_traces(textposition="outside", textfont=dict(color="#395448", size=10), marker_line_width=0)
                bar_v3.update_layout(height=126, margin=dict(l=46, r=26, t=0, b=3), showlegend=False, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", xaxis=dict(visible=False, fixedrange=True), yaxis=dict(title=None, tickfont=dict(color="#547166", size=10), fixedrange=True))
                st.plotly_chart(bar_v3, use_container_width=True, config={"displayModeBar": False}, key="alarm_v3_machine_bars")

        with st.container(border=True):
            st.markdown('<div class="alarm-v3-panel-title">▥ Alarm Durum Dağılımı</div>', unsafe_allow_html=True)
            status_bar_v3 = px.bar(summary_rows_v3, x="Durum", y="Kayıt", text="Kayıt", color="Durum", color_discrete_map={"Kritik":"#ed6a6f", "Uyarı":"#eda93c", "Bilgi":"#5b9ed8", "Çözülen":"#36ad75", "Kayıt yok":"#d6e1dc"})
            status_bar_v3.update_traces(textposition="outside", textfont=dict(color="#395448", size=10), marker_line_width=0)
            status_bar_v3.update_layout(height=142, margin=dict(l=2, r=2, t=4, b=28), showlegend=False, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", yaxis=dict(visible=False, fixedrange=True), xaxis=dict(title=None, tickfont=dict(color="#547166", size=9), fixedrange=True), yaxis_title=None)
            st.plotly_chart(status_bar_v3, use_container_width=True, config={"displayModeBar": False}, key="alarm_v3_status_bars")

    history_alarm_col_v3, quick_alarm_col_v3 = st.columns([3.2, 1], gap="small")
    with history_alarm_col_v3:
        with st.container(border=True):
            st.markdown('<div class="alarm-v3-panel-title">◷ Alarm Geçmişi</div>', unsafe_allow_html=True)
            history_view_v3 = filtered_alarms_v3[["level", "machine_code", "alarm", "time", "mudahale_durumu", "sorumlu"]].copy()
            history_view_v3["level"] = history_view_v3["level"].map({"Kritik": "🔴 Kritik", "Uyarı": "🟠 Uyarı", "Bilgi": "🔵 Bilgi"}).fillna(history_view_v3["level"])
            history_view_v3 = history_view_v3.rename(columns={"level":"Seviye", "machine_code":"Makine", "alarm":"Alarm açıklaması", "time":"Oluşma zamanı", "mudahale_durumu":"Durum", "sorumlu":"Sorumlu"})
            if history_view_v3.empty:
                st.caption("Bu tarih aralığında alarm geçmişi bulunmuyor.")
            else:
                st.dataframe(history_view_v3, use_container_width=True, hide_index=True, height=190)
    with quick_alarm_col_v3:
        with st.container(border=True):
            st.markdown('<div class="alarm-v3-panel-title">⚡ Hızlı İşlemler</div>', unsafe_allow_html=True)
            if st.button("⊕ Yeni Alarm Kaydı", key="alarm_v3_new", use_container_width=True):
                st.session_state["alarm_new_open"] = True
            if st.button("⚒ Bakım Talebi Oluştur", key="alarm_v3_maintenance", use_container_width=True):
                st.session_state["selected_module"] = "🧰 Bakım Talebi"
                st.rerun()
            if st.button("▣ Rapor Al", key="alarm_v3_report", use_container_width=True):
                st.session_state["selected_module"] = "📄 Raporlar"
                st.rerun()
            if st.button("⚙ Kullanıcı Yönetimi", key="alarm_v3_settings", use_container_width=True):
                st.session_state["selected_module"] = "👥 Kullanıcı Yönetimi"
                st.rerun()

    if st.session_state.pop("alarm_record_created", False):
        st.success("Alarm kaydı oluşturuldu ve aktif alarm listesine eklendi.")

    @st.dialog("Yeni Alarm Kaydı", width="large")
    def alarm_record_dialog():
        if st.button("Kapat", key="alarm_dialog_close"):
            st.session_state["alarm_new_open"] = False
            st.rerun()
        if not has_role("admin", "maintenance"):
            st.info("Manuel alarm kaydı için bakım veya yönetici yetkisi gerekir.")
            return
        if df.empty:
            st.warning("Alarm kaydı oluşturulabilecek makine bulunmuyor.")
            return
        with st.form("alarm_new_record_form"):
            alarm_machine = st.selectbox("Makine", df["machine_code"].tolist(), key="alarm_new_machine")
            alarm_level = st.selectbox("Seviye", ["Bilgi", "Uyarı", "Kritik"], index=1, key="alarm_new_level")
            alarm_description = st.text_area("Alarm açıklaması", placeholder="Alarmın nedenini ve gözlemi yazın.", key="alarm_new_description")
            if st.form_submit_button("Alarm kaydını oluştur", type="primary", use_container_width=True):
                if not alarm_description.strip():
                    st.error("Alarm açıklaması boş bırakılamaz.")
                else:
                    alarm_machine_id = int(df[df["machine_code"] == alarm_machine].iloc[0]["id"])
                    execute("INSERT INTO alarms(machine_id,machine_code,alarm,level,time,acknowledged) VALUES(?,?,?,?,?,0)", (alarm_machine_id, alarm_machine, alarm_description.strip(), alarm_level, now()))
                    audit_event("Manuel alarm kaydı oluşturdu", alarm_machine, f"{alarm_level} · {alarm_description.strip()}")
                    st.session_state["alarm_new_open"] = False
                    st.session_state["alarm_record_created"] = True
                    st.rerun()

    if st.session_state.get("alarm_new_open", False):
        alarm_record_dialog()

    st.stop()


if selected_module == "🚨 Alarmlar":
    st.subheader("🚨 Alarm Yönetimi")

    alarms = q("""
        SELECT a.id,a.machine_code,a.alarm,a.level,a.time,a.acknowledged,
               COALESCE(x.assignee, 'Atanmadı') AS sorumlu,
               COALESCE(x.status, 'Bekliyor') AS mudahale_durumu
        FROM alarms a LEFT JOIN alarm_actions x ON x.alarm_id=a.id
        ORDER BY a.id DESC LIMIT 200
    """)
    if not alarms.empty:
        alarms["_alarm_date"] = pd.to_datetime(alarms["time"], errors="coerce").dt.date
        date_col, all_col = st.columns([1, 2])
        with date_col:
            selected_alarm_range = auto_date_range_filter("Alarm tarih aralığı", "alarm_day_filter")
        with all_col:
            show_all_alarms = st.checkbox("Tüm günlerin alarmlarını göster", value=False, key="show_all_alarms")
        if not show_all_alarms:
            alarm_start, alarm_end = date_range_bounds(selected_alarm_range)
            alarms = alarms[(alarms["_alarm_date"] >= alarm_start) & (alarms["_alarm_date"] <= alarm_end)].copy()
    active = alarms[alarms["acknowledged"] == 0] if not alarms.empty else alarms
    critical_count = int((active["level"]=="Kritik").sum()) if not active.empty else 0
    unassigned_count = int((active["sorumlu"]=="Atanmadı").sum()) if not active.empty else 0
    compact_summary, severity_chart_col = st.columns([1, 1.65])
    with compact_summary:
        st.markdown("""<style>.alarm-compact{background:#fff;border:1px solid #d9e5dc;border-radius:10px;padding:8px 11px;margin:0 0 6px;box-shadow:0 3px 10px rgba(0,40,22,.08);display:flex;align-items:center;justify-content:space-between}.alarm-compact small{color:#536b5b;font-size:.68rem}.alarm-compact strong{font-size:1.1rem;color:#0c3f27}</style>""", unsafe_allow_html=True)
        st.markdown(f'<div class="alarm-compact"><small>Açık Alarm</small><strong>{len(active)}</strong></div>', unsafe_allow_html=True)
        st.markdown(f'<div class="alarm-compact"><small>Kritik</small><strong>{critical_count}</strong></div>', unsafe_allow_html=True)
        st.markdown(f'<div class="alarm-compact"><small>Atanmamış</small><strong>{unassigned_count}</strong></div>', unsafe_allow_html=True)
    with severity_chart_col:
        severity_data = alarms.groupby("level", as_index=False).size().rename(columns={"size":"Kayıt", "level":"Seviye"}) if not alarms.empty else pd.DataFrame({"Seviye":["Kayıt yok"],"Kayıt":[0]})
        severity_chart = px.bar(severity_data, x="Seviye", y="Kayıt", color="Seviye", text="Kayıt", title="Alarm Seviyesi Dağılımı", color_discrete_map={"Kritik":"#d84a4a", "Uyarı":"#e9a72d", "Bilgi":"#23965a", "Kayıt yok":"#aab9ae"})
        severity_chart.update_traces(textposition="outside")
        severity_chart.update_layout(height=190, showlegend=False, margin=dict(l=10,r=8,t=38,b=18), yaxis_title=None, xaxis_title=None, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
        st.plotly_chart(severity_chart, use_container_width=True, config={"displayModeBar":False}, key="alarm_severity_compact")
    st.markdown("#### Alarm kayıtları")
    if alarms.empty:
        st.info("Seçilen tarih aralığında alarm kaydı yok.")
    else:
        alarm_view = alarms[["id", "machine_code", "alarm", "level", "time", "sorumlu", "mudahale_durumu", "acknowledged"]].copy()
        alarm_view["acknowledged"] = alarm_view["acknowledged"].map({0:"Açık", 1:"Onaylandı"})
        alarm_view = alarm_view.rename(columns={
            "id":"#", "machine_code":"Makine", "alarm":"Alarm", "level":"Seviye",
            "time":"Tarih / Saat", "sorumlu":"Sorumlu", "mudahale_durumu":"Müdahale", "acknowledged":"Durum"
        })
        st.dataframe(alarm_view, use_container_width=True, hide_index=True, height=270)
    if not active.empty and has_role("admin","maintenance"):
        st.markdown("#### Alarm müdahalesi")
        alarm_labels={int(r["id"]):f'{r["machine_code"]} · {r["alarm"]}' for _,r in active.iterrows()}
        alarm_id=st.selectbox("Açık alarm",list(alarm_labels),format_func=lambda x:alarm_labels[x],key="alarm_select")
        assignees=q("SELECT full_name FROM users WHERE is_active=1 ORDER BY full_name")["full_name"].dropna().tolist()
        with st.form("alarm_action_form"):
            assignee=st.selectbox("Sorumlu",assignees or ["Atanmadı"]); action_note=st.text_input("Müdahale notu"); resolve=st.checkbox("Alarm çözüldü"); failure_event=st.checkbox("Bu alarm makine arızası olarak kaydedilsin", value=False, help="İşaretlendiğinde çözüm süresi gerçek MTTR hesabına dahil edilir.")
            if st.form_submit_button("💾 Müdahaleyi Kaydet"):
                execute("""INSERT INTO alarm_actions(alarm_id,assignee,note,assigned_at,resolved_at,status) VALUES(?,?,?,?,?,?) ON CONFLICT(alarm_id) DO UPDATE SET assignee=excluded.assignee,note=excluded.note,assigned_at=excluded.assigned_at,resolved_at=excluded.resolved_at,status=excluded.status""",(int(alarm_id),assignee,action_note,now(),now() if resolve else None,"Çözüldü" if resolve else "Atandı"))
                if resolve:
                    alarm_row=q("SELECT machine_id,machine_code,time FROM alarms WHERE id=?", (int(alarm_id),)).iloc[0]
                    if failure_event:
                        started=pd.to_datetime(alarm_row["time"], errors="coerce")
                        repair_minutes=max(1.0, (datetime.now()-started.to_pydatetime()).total_seconds()/60) if pd.notna(started) else 1.0
                        already_logged=q("SELECT 1 FROM downtime WHERE notes=? LIMIT 1", (f"Alarm #{int(alarm_id)}",))
                        if already_logged.empty:
                            execute("INSERT INTO downtime(machine_id,machine_code,reason,duration,start_time,end_time,notes,event_at) VALUES(?,?,?,?,?,?,?,?)", (int(alarm_row["machine_id"]), alarm_row["machine_code"], "Alarm kaynaklı arıza", round(repair_minutes,1), str(alarm_row["time"]), now(), f"Alarm #{int(alarm_id)}", str(alarm_row["time"])))
                            execute("UPDATE machines SET downtime=downtime+? WHERE id=?", (round(repair_minutes,1), int(alarm_row["machine_id"])))
                    execute("UPDATE alarms SET acknowledged=1 WHERE id=?",(int(alarm_id),)); audit_event("Alarm müdahalesini kapattı", f"Alarm #{int(alarm_id)}", f"Arıza kaydı: {failure_event}")
                st.rerun()


if selected_module == "📋 İş Emirleri":
    # İş emirleri merkezi: planlama, gecikme riski ve makine yükü tek görünümde.
    st.markdown("""
    <style>
      .work-v3-head{display:flex;align-items:center;gap:9px;margin:0 0 .55rem}
      .work-v3-head h2{margin:0;color:#063d2b;font-size:1.32rem;font-weight:800}
      .work-v3-head p{margin:0;color:#6b8177;font-size:.77rem}
      .work-v3-card{min-height:76px;background:#fff;border:1px solid #d9ece3;border-radius:8px;padding:10px 11px;
          box-shadow:0 3px 10px rgba(14,79,52,.055);position:relative;overflow:hidden}
      .work-v3-card:before{content:'';position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--work-color)}
      .work-v3-card span{display:inline-flex;height:25px;width:25px;border-radius:50%;align-items:center;justify-content:center;background:var(--work-color);color:white;font-weight:800;font-size:.78rem;vertical-align:top}
      .work-v3-card label{margin-left:6px;color:#49645a;font-size:.67rem;font-weight:800;vertical-align:top;line-height:25px}
      .work-v3-card strong{display:block;margin:0 0 0 32px;color:#073d2a;font-size:1.42rem;line-height:1.15}
      .work-v3-card small{position:absolute;right:9px;bottom:9px;color:var(--work-color);font-size:.59rem;font-weight:700}
      .work-v3-panel-title{font-size:.88rem;color:#0b4935;font-weight:800;margin:0 0 .55rem}
      .work-v3-tiny{color:#71877b;font-size:.7rem;margin:-.25rem 0 .45rem}
    </style>
    """, unsafe_allow_html=True)
    st.markdown("""
      <div class="work-v3-head"><span style="font-size:1.2rem">▤</span>
      <div><h2>İş Emirleri Yönetimi</h2><p>Üretim planını, termin risklerini ve makine yükünü anlık takip edin.</p></div></div>
    """, unsafe_allow_html=True)

    work_v3 = q("""
        SELECT w.id,w.order_no,w.machine_id,w.machine_code,w.product,w.target,w.produced,w.priority,w.status,w.due_date,w.start_production,
               COALESCE(m.shift, '') AS shift
        FROM work_orders w
        LEFT JOIN machines m ON m.machine_code=w.machine_code
        ORDER BY w.id DESC
    """)
    if work_v3.empty:
        work_v3 = pd.DataFrame(columns=["id", "order_no", "machine_id", "machine_code", "product", "target", "produced", "priority", "status", "due_date", "start_production", "shift"])
    work_v3["target"] = pd.to_numeric(work_v3["target"], errors="coerce").fillna(0)
    work_v3["produced"] = pd.to_numeric(work_v3["produced"], errors="coerce").fillna(0)
    work_v3["_termin"] = pd.to_datetime(work_v3["due_date"], errors="coerce").dt.date
    work_v3["_tamamlandi"] = work_v3["status"].astype(str).str.lower().isin(["tamamlandı", "tamamlandi", "completed"])
    work_v3["_gecikmis"] = (~work_v3["_tamamlandi"]) & work_v3["_termin"].notna() & (work_v3["_termin"] < date.today())
    work_v3["_durum"] = work_v3["status"].fillna("Sırada").astype(str)
    work_v3.loc[work_v3["_gecikmis"], "_durum"] = "Gecikmiş"
    work_v3["_ilerleme"] = (work_v3["produced"] / work_v3["target"].replace(0, 1) * 100).clip(lower=0, upper=100).round(1)

    # Referanstaki yatay filtre bandı: beş operasyon filtresi, tarih aralığı ve arama.
    f_status, f_machine, f_product, f_priority, f_shift, f_date, f_search, f_new = st.columns([.95, 1.12, 1.04, .98, .88, 1.42, 1.45, .92])
    with f_status:
        work_status_v3 = st.selectbox("Durum", ["Tümü", "Üretimde", "Sırada", "Bekliyor", "Gecikmiş", "Tamamlandı"], key="work_v3_status")
    with f_machine:
        work_machine_v3 = st.selectbox("Makine", ["Tüm Makineler"] + sorted(work_v3["machine_code"].dropna().astype(str).unique().tolist()), key="work_v3_machine")
    with f_product:
        work_product_v3 = st.selectbox("Ürün", ["Tümü"] + sorted(work_v3["product"].dropna().astype(str).unique().tolist()), key="work_v3_product")
    with f_priority:
        work_priority_v3 = st.selectbox("Öncelik", ["Tümü", "Kritik", "Yüksek", "Normal", "Düşük"], key="work_v3_priority")
    with f_shift:
        work_shift_v3 = st.selectbox("Vardiya", ["Tümü", "Sabah", "Akşam", "Gece"], key="work_v3_shift")
    with f_date:
        work_dates_v3 = st.date_input("Termin aralığı", value=(date.today(), date.today()), key="work_v3_dates")
    with f_search:
        work_search_v3 = st.text_input("İş emri ara", placeholder="İş emri veya ürün ara...", key="work_v3_search")
    with f_new:
        st.write("")
        if st.button("＋ Yeni İş Emri", type="primary", use_container_width=True, key="work_v3_new_button"):
            st.session_state["work_v3_new_open"] = True

    visible_work_v3 = work_v3.copy()
    if work_status_v3 != "Tümü": visible_work_v3 = visible_work_v3[visible_work_v3["_durum"] == work_status_v3]
    if work_machine_v3 != "Tüm Makineler": visible_work_v3 = visible_work_v3[visible_work_v3["machine_code"].fillna("").astype(str) == work_machine_v3]
    if work_product_v3 != "Tümü": visible_work_v3 = visible_work_v3[visible_work_v3["product"].fillna("").astype(str) == work_product_v3]
    if work_priority_v3 != "Tümü": visible_work_v3 = visible_work_v3[visible_work_v3["priority"].fillna("") == work_priority_v3]
    if work_shift_v3 != "Tümü": visible_work_v3 = visible_work_v3[visible_work_v3["shift"].fillna("") == work_shift_v3]
    if isinstance(work_dates_v3, (tuple, list)) and len(work_dates_v3) == 2:
        work_date_start_v3, work_date_end_v3 = work_dates_v3
    else:
        work_date_start_v3 = work_date_end_v3 = work_dates_v3
    if work_date_start_v3 and work_date_end_v3:
        visible_work_v3 = visible_work_v3[(visible_work_v3["_termin"] >= work_date_start_v3) & (visible_work_v3["_termin"] <= work_date_end_v3)]
    if work_search_v3.strip():
        work_needle_v3 = work_search_v3.strip().lower()
        work_text_v3 = visible_work_v3["order_no"].fillna("").astype(str) + " " + visible_work_v3["product"].fillna("").astype(str)
        visible_work_v3 = visible_work_v3[work_text_v3.str.lower().str.contains(work_needle_v3, na=False)]

    total_work_v3 = len(visible_work_v3)
    producing_work_v3 = int((visible_work_v3["_durum"] == "Üretimde").sum())
    queued_work_v3 = int((visible_work_v3["_durum"] == "Sırada").sum())
    delayed_work_v3 = int((visible_work_v3["_durum"] == "Gecikmiş").sum())
    completed_work_v3 = int((visible_work_v3["_durum"] == "Tamamlandı").sum())
    work_stats_v3 = [
        ("≡", "Toplam İş Emri", total_work_v3, "#0d9c61", "Seçili filtre"),
        ("⚙", "Üretimde", producing_work_v3, "#258bd2", "Aktif üretim"),
        ("◷", "Sırada", queued_work_v3, "#f59e0b", "Makine bekliyor"),
        ("!", "Geciken", delayed_work_v3, "#ed424b", "Termin riski"),
        ("✓", "Tamamlanan", completed_work_v3, "#13a365", "Kapatılan işler"),
    ]
    stat_cols_v3 = st.columns(5)
    for stat_col_v3, (icon_v3, label_v3, value_v3, color_v3, hint_v3) in zip(stat_cols_v3, work_stats_v3):
        with stat_col_v3:
            st.markdown(f'<div class="work-v3-card" style="--work-color:{color_v3}"><span>{icon_v3}</span><label>{label_v3}</label><strong>{value_v3}</strong><small>{hint_v3}</small></div>', unsafe_allow_html=True)

    main_work_v3, side_work_v3 = st.columns([2.25, 1], gap="small")
    with main_work_v3:
        with st.container(border=True):
            st.markdown('<div class="work-v3-panel-title">▤ İş Emirleri <span style="float:right;color:#0b9f61;font-size:.7rem">{} kayıt</span></div>'.format(total_work_v3), unsafe_allow_html=True)
            if visible_work_v3.empty:
                st.info("Seçilen filtrede iş emri bulunmuyor.")
            else:
                work_table_v3 = visible_work_v3[["order_no", "product", "machine_code", "shift", "target", "produced", "_ilerleme", "priority", "due_date", "_durum"]].copy()
                work_table_v3["machine_code"] = work_table_v3["machine_code"].fillna("Kuyrukta")
                work_table_v3["shift"] = work_table_v3["shift"].fillna("—").replace("", "—")
                work_table_v3["_ilerleme"] = work_table_v3["_ilerleme"].map(lambda value: f"%{value:.0f}")
                work_table_v3["priority"] = work_table_v3["priority"].map({"Kritik":"🔴 Kritik", "Yüksek":"🟠 Yüksek", "Normal":"🟢 Normal", "Düşük":"🔵 Düşük"}).fillna(work_table_v3["priority"])
                work_table_v3["_durum"] = work_table_v3["_durum"].map({"Üretimde":"🟢 Üretimde", "Sırada":"🟠 Sırada", "Bekliyor":"🟡 Bekliyor", "Gecikmiş":"🔴 Gecikmiş", "Tamamlandı":"🟢 Tamamlandı"}).fillna(work_table_v3["_durum"])
                work_table_v3 = work_table_v3.rename(columns={"order_no":"İş Emri", "product":"Ürün", "machine_code":"Makine", "shift":"Vardiya", "target":"Hedef", "produced":"Üretim", "_ilerleme":"Gerçekleşme", "priority":"Öncelik", "due_date":"Termin", "_durum":"Durum"})
                st.dataframe(work_table_v3, use_container_width=True, hide_index=True, height=242)
                order_labels_v3 = {int(row["id"]): f'{row["order_no"]} · {row["product"]}' for _, row in visible_work_v3.iterrows()}
                select_order_col_v3, detail_order_col_v3 = st.columns([4, 1])
                with select_order_col_v3:
                    selected_work_id_v3 = st.selectbox("İş emri detayı", list(order_labels_v3), format_func=lambda item: order_labels_v3[item], label_visibility="collapsed", key="work_v3_selected")
                with detail_order_col_v3:
                    if st.button("Detay", use_container_width=True, key="work_v3_detail"):
                        st.session_state["work_v3_detail_open"] = True
                if st.session_state.get("work_v3_detail_open", False):
                    selected_work_v3 = visible_work_v3[visible_work_v3["id"] == selected_work_id_v3].iloc[0]
                    with st.expander("Seçili iş emri", expanded=True):
                        d1_v3, d2_v3, d3_v3, d4_v3 = st.columns(4)
                        d1_v3.metric("Makine", selected_work_v3["machine_code"] or "Kuyrukta")
                        d2_v3.metric("Üretim", f"{int(selected_work_v3['produced']):,} / {int(selected_work_v3['target']):,}")
                        d3_v3.metric("Termin", str(selected_work_v3["due_date"] or "—"))
                        d4_v3.metric("Durum", selected_work_v3["_durum"])
                        if st.button("Detayı kapat", key="work_v3_close_detail"):
                            st.session_state["work_v3_detail_open"] = False
                            st.rerun()

        chart_left_v3, chart_mid_v3, chart_right_v3 = st.columns([1, 1.15, 1.1], gap="small")
        with chart_left_v3:
            with st.container(border=True):
                st.markdown('<div class="work-v3-panel-title">◉ İş Emri Durum Dağılımı</div>', unsafe_allow_html=True)
                status_counts_v3 = visible_work_v3.groupby("_durum").size().reset_index(name="Kayıt") if not visible_work_v3.empty else pd.DataFrame({"_durum":["Kayıt yok"], "Kayıt":[1]})
                status_pie_v3 = px.pie(status_counts_v3, names="_durum", values="Kayıt", hole=.63, color="_durum", color_discrete_map={"Üretimde":"#18a563", "Sırada":"#f59e0b", "Bekliyor":"#6ea8c9", "Gecikmiş":"#ef4444", "Tamamlandı":"#1a9662", "Kayıt yok":"#d9e6df"})
                status_pie_v3.update_traces(textinfo="none")
                status_pie_v3.add_annotation(text=f"<b>{total_work_v3}</b><br><span style='font-size:9px'>Toplam</span>", x=.5, y=.5, showarrow=False, font=dict(size=15, color="#0b4935"))
                status_pie_v3.update_layout(height=190, margin=dict(l=0,r=0,t=0,b=0), showlegend=False, paper_bgcolor="rgba(0,0,0,0)")
                st.plotly_chart(status_pie_v3, use_container_width=True, config={"displayModeBar":False}, key="work_v3_status_pie")
        with chart_mid_v3:
            with st.container(border=True):
                st.markdown('<div class="work-v3-panel-title">▥ Makine Bazlı İş Emirleri</div>', unsafe_allow_html=True)
                machine_work_v3 = visible_work_v3[visible_work_v3["machine_code"].notna()].groupby("machine_code").size().reset_index(name="İş Emri") if not visible_work_v3.empty else pd.DataFrame(columns=["machine_code", "İş Emri"])
                if machine_work_v3.empty:
                    st.caption("Makineye atanmış iş emri bulunmuyor.")
                else:
                    machine_work_chart_v3 = px.bar(machine_work_v3, x="machine_code", y="İş Emri", text="İş Emri", color="İş Emri", color_continuous_scale=["#b6eed8", "#079957"])
                    machine_work_chart_v3.update_traces(textposition="outside")
                    machine_work_chart_v3.update_layout(height=190, margin=dict(l=0,r=0,t=5,b=0), showlegend=False, coloraxis_showscale=False, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", yaxis=dict(visible=False), xaxis_title=None, yaxis_title=None)
                    st.plotly_chart(machine_work_chart_v3, use_container_width=True, config={"displayModeBar":False}, key="work_v3_machine_chart")
        with chart_right_v3:
            with st.container(border=True):
                st.markdown('<div class="work-v3-panel-title">⌁ Hedef / Gerçekleşme</div>', unsafe_allow_html=True)
                trend_work_v3 = visible_work_v3.dropna(subset=["_termin"]).groupby("_termin", as_index=False).agg(Hedef=("target", "sum"), Gerçekleşen=("produced", "sum")) if not visible_work_v3.empty else pd.DataFrame(columns=["_termin", "Hedef", "Gerçekleşen"])
                if trend_work_v3.empty:
                    st.caption("Termin tarihli iş emri bulunmuyor.")
                else:
                    trend_chart_v3 = go.Figure()
                    trend_chart_v3.add_trace(go.Scatter(x=trend_work_v3["_termin"], y=trend_work_v3["Hedef"], mode="lines+markers", name="Hedef", line=dict(color="#9ac8b5", dash="dot")))
                    trend_chart_v3.add_trace(go.Scatter(x=trend_work_v3["_termin"], y=trend_work_v3["Gerçekleşen"], mode="lines+markers", name="Gerçekleşen", line=dict(color="#079957", width=3)))
                    trend_chart_v3.update_layout(height=190, margin=dict(l=0,r=0,t=4,b=0), legend=dict(orientation="h", y=1.1, font=dict(size=8)), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", xaxis=dict(title=None), yaxis=dict(visible=False))
                    st.plotly_chart(trend_chart_v3, use_container_width=True, config={"displayModeBar":False}, key="work_v3_trend_chart")

        with st.container(border=True):
            st.markdown('<div class="work-v3-panel-title">▣ İş Emri Analizi</div>', unsafe_allow_html=True)
            total_target_v3 = float(visible_work_v3["target"].sum())
            total_produced_v3 = float(visible_work_v3["produced"].sum())
            avg_completion_v3 = (total_produced_v3 / max(total_target_v3, 1) * 100)
            critical_work_v3 = int((visible_work_v3["priority"] == "Kritik").sum())
            overdue_note_v3 = "Termin riski" if delayed_work_v3 else "Terminler planlı"
            analysis_cols_v3 = st.columns(5)
            for analysis_col_v3, title_v3, value_v3, note_v3 in zip(analysis_cols_v3, ["Ortalama ilerleme", "Tamamlanan iş", "Geciken iş", "Toplam üretim", "Kritik iş emri"], [f"%{avg_completion_v3:.1f}", f"{completed_work_v3}", f"{delayed_work_v3}", f"{int(total_produced_v3):,}", f"{critical_work_v3}"], ["Seçili iş emirleri", "Kapatılan kayıt", overdue_note_v3, "Gerçekleşen adet", "Öncelik kontrolü"]):
                with analysis_col_v3:
                    st.metric(title_v3, value_v3, note_v3)

    with side_work_v3:
        with st.container(border=True):
            st.markdown('<div class="work-v3-panel-title">▤ Üretim Planı <span style="float:right;color:#0b9f61;font-size:.67rem">Tümünü Gör ›</span></div>', unsafe_allow_html=True)
            plan_v3 = work_v3[~work_v3["_tamamlandi"]].sort_values("_termin").head(5)
            if plan_v3.empty:
                st.caption("Planlanmış iş emri yok.")
            else:
                plan_view_v3 = plan_v3[["order_no", "product", "target", "due_date", "_durum"]].rename(columns={"order_no":"İş Emri", "product":"Ürün", "target":"Hedef", "due_date":"Termin", "_durum":"Durum"})
                st.dataframe(plan_view_v3, use_container_width=True, hide_index=True, height=195)
        with st.container(border=True):
            st.markdown('<div class="work-v3-panel-title">▣ Makine Durumu</div>', unsafe_allow_html=True)
            machine_state_v3 = df[["machine_code", "status", "production", "target"]].copy() if not df.empty else pd.DataFrame(columns=["machine_code", "status", "production", "target"])
            if machine_state_v3.empty:
                st.caption("Makine kaydı bulunmuyor.")
            else:
                machine_state_v3["Yük"] = (pd.to_numeric(machine_state_v3["production"], errors="coerce").fillna(0) / pd.to_numeric(machine_state_v3["target"], errors="coerce").replace(0, 1) * 100).clip(0, 100).round(0).astype(int).astype(str) + "%"
                machine_state_v3 = machine_state_v3.rename(columns={"machine_code":"Makine", "status":"Durum"})[["Makine", "Durum", "Yük"]]
                st.dataframe(machine_state_v3, use_container_width=True, hide_index=True, height=145)
        with st.container(border=True):
            st.markdown('<div class="work-v3-panel-title">◷ Geciken İş Emirleri</div>', unsafe_allow_html=True)
            delayed_view_v3 = work_v3[work_v3["_gecikmis"]][["order_no", "machine_code", "due_date", "_ilerleme"]].copy()
            if delayed_view_v3.empty:
                st.success("Gecikmiş iş emri bulunmuyor.")
            else:
                delayed_view_v3["_ilerleme"] = delayed_view_v3["_ilerleme"].map(lambda value: f"%{value:.0f}")
                delayed_view_v3 = delayed_view_v3.rename(columns={"order_no":"İş Emri", "machine_code":"Makine", "due_date":"Termin", "_ilerleme":"Gerçekleşme"})
                st.dataframe(delayed_view_v3, use_container_width=True, hide_index=True, height=130)
        with st.container(border=True):
            st.markdown('<div class="work-v3-panel-title">⚡ Hızlı İşlemler</div>', unsafe_allow_html=True)
            if st.button("⊕ Yeni İş Emri", use_container_width=True, type="primary", key="work_v3_quick_new"):
                st.session_state["work_v3_new_open"] = True
            if st.button("▦ Makine Ata", use_container_width=True, key="work_v3_quick_machine"):
                st.session_state["selected_module"] = "🏭 Makine"
                st.rerun()
            if st.button("▣ Rapor Al", use_container_width=True, key="work_v3_quick_report"):
                st.session_state["selected_module"] = "📄 Raporlar"
                st.rerun()

    work_order_result_v3 = st.session_state.pop("work_order_created_message", "")
    if work_order_result_v3:
        st.success(work_order_result_v3)

    @st.dialog("Yeni İş Emri", width="large")
    def work_order_dialog_v3():
        if st.button("Kapat", key="work_order_dialog_close"):
            st.session_state["work_v3_new_open"] = False
            st.rerun()
        st.caption("Makine müsait değilse iş emri otomatik olarak üretim kuyruğuna eklenir.")
        if not is_admin():
            st.info("Yeni iş emri oluşturmak için yönetici yetkisi gerekir.")
            return
        with st.form("work_v3_new_form"):
            new_a_v3, new_b_v3 = st.columns(2)
            with new_a_v3:
                new_no_v3 = st.text_input("İş emri no", f"WO-{datetime.now().strftime('%H%M%S')}")
                new_product_v3 = st.text_input("Ürün", "Yeni Ürün")
            with new_b_v3:
                new_target_v3 = st.number_input("Miktar", min_value=1, max_value=100000, value=500)
                new_priority_v3 = st.selectbox("Öncelik", ["Düşük", "Normal", "Yüksek", "Kritik"])
            new_due_v3 = st.date_input("Termin", date.today())
            if st.form_submit_button("İş emrini oluştur", type="primary", use_container_width=True):
                available_v3 = q("SELECT id,machine_code,production,target FROM machines WHERE status!='Arızalı' AND production>=target ORDER BY machine_code")
                try:
                    if available_v3.empty:
                        execute("INSERT INTO work_orders(order_no,machine_id,machine_code,product,target,produced,priority,status,due_date,start_production) VALUES(?,?,?,?,?,?,?,?,?,?)", (new_no_v3, None, None, new_product_v3, int(new_target_v3), 0, new_priority_v3, "Sırada", str(new_due_v3), 0))
                        audit_event("İş emrini kuyruğa ekledi", new_no_v3, new_product_v3)
                        result_message_v3 = f"{new_no_v3} üretim kuyruğuna eklendi."
                    else:
                        routed_v3 = available_v3.iloc[0]
                        assigned_target_v3 = int(routed_v3["production"]) + int(new_target_v3)
                        execute("INSERT INTO work_orders(order_no,machine_id,machine_code,product,target,produced,priority,status,due_date,start_production) VALUES(?,?,?,?,?,?,?,?,?,?)", (new_no_v3, int(routed_v3["id"]), routed_v3["machine_code"], new_product_v3, int(new_target_v3), 0, new_priority_v3, "Üretimde", str(new_due_v3), int(routed_v3["production"])))
                        execute("UPDATE machines SET target=?,product=?,status='Çalışıyor' WHERE id=?", (assigned_target_v3, new_product_v3, int(routed_v3["id"])))
                        audit_event("İş emrini makineye yönlendirdi", new_no_v3, routed_v3["machine_code"])
                        result_message_v3 = f"{new_no_v3}, {routed_v3['machine_code']} makinesine yönlendirildi."
                    st.session_state["work_v3_new_open"] = False
                    st.session_state["work_order_created_message"] = result_message_v3
                    st.rerun()
                except INTEGRITY_ERRORS:
                    st.error("Bu iş emri numarası zaten kullanılıyor.")

    if st.session_state.get("work_v3_new_open", False):
        work_order_dialog_v3()

    st.stop()


if selected_module == "📋 İş Emirleri":
    st.subheader("📋 İş Emirleri")
    work_orders=q("""SELECT id,order_no,machine_code,product,target,produced,priority,status,due_date FROM work_orders ORDER BY id DESC""")
    all_work_orders = work_orders.copy()
    work_date_col, work_all_col = st.columns([1, 2])
    with work_date_col:
        selected_work_range = auto_date_range_filter("Termin tarih aralığı", "work_order_day_filter")
    with work_all_col:
        show_all_work_orders = st.checkbox("Tüm tarihlerin iş emirlerini göster", value=False, key="show_all_work_orders")
    if not show_all_work_orders and not work_orders.empty:
        due_dates = pd.to_datetime(work_orders["due_date"], errors="coerce").dt.date
        work_start, work_end = date_range_bounds(selected_work_range)
        work_orders = work_orders[(due_dates >= work_start) & (due_dates <= work_end)].copy()
    queued_orders = all_work_orders[all_work_orders["status"] == "Sırada"].copy() if not all_work_orders.empty else all_work_orders
    if not queued_orders.empty:
        st.markdown("#### Üretim kuyruğu")
        st.caption("Müsait makine oluştuğunda öncelik ve termin tarihine göre otomatik yönlendirilir.")
        queue_view = queued_orders.rename(columns={"id":"#", "order_no":"İş Emri", "machine_code":"Makine", "product":"Ürün", "target":"Hedef", "produced":"Üretilen", "priority":"Öncelik", "status":"Durum", "due_date":"Termin"})
        st.dataframe(queue_view, use_container_width=True, hide_index=True, height=170)
    st.markdown("#### İş emri kayıtları")
    if work_orders.empty:
        st.info("Seçilen tarih aralığında iş emri yok.")
    else:
        work_order_view = work_orders.copy()
        work_order_view["İlerleme %"] = (work_order_view["produced"] / work_order_view["target"].replace(0, 1) * 100).clip(upper=100).round(1)
        work_order_view["machine_code"] = work_order_view["machine_code"].fillna("Kuyrukta")
        work_order_view = work_order_view.rename(columns={"id":"#", "order_no":"İş Emri", "machine_code":"Makine", "product":"Ürün", "target":"Hedef", "produced":"Üretilen", "priority":"Öncelik", "status":"Durum", "due_date":"Termin"})
        st.dataframe(work_order_view, use_container_width=True, hide_index=True, height=270)
    available_machines=q("""SELECT id,machine_code,production,target,status,product,next_maintenance FROM machines WHERE status!='Arızalı' AND production>=target ORDER BY machine_code""")
    if is_admin():
        st.markdown("#### Yeni iş emri")
        if available_machines.empty: st.info("Şu an müsait makine yok. İş emrini üretim kuyruğuna ekleyebilirsin.")
        with st.form("work_order_form"):
            order_no=st.text_input("İş emri no",f"WO-{datetime.now().strftime('%H%M%S')}"); product=st.text_input("Ürün","Yeni Ürün"); target=st.number_input("İş emri miktarı",min_value=1,max_value=100000,value=500); priority=st.selectbox("Öncelik",["Düşük","Normal","Yüksek","Kritik"]); due_date=st.date_input("Termin",date.today())
            submitted=st.form_submit_button("💾 Kuyruğa Ekle / Otomatik Yönlendir")
            if submitted:
                try:
                    c=conn()
                    if available_machines.empty:
                        c.execute("INSERT INTO work_orders(order_no,machine_id,machine_code,product,target,produced,priority,status,due_date,start_production) VALUES(?,?,?,?,?,?,?,?,?,?)",(order_no,None,None,product,int(target),0,priority,"Sırada",str(due_date),0))
                        c.commit(); c.close(); st.success(f"{order_no} üretim kuyruğuna eklendi."); st.rerun()
                    routed=available_machines.iloc[0]; machine_id=int(routed["id"]); code=routed["machine_code"]; start=int(routed["production"]); new_target=start+int(target)
                    c.execute("INSERT INTO work_orders(order_no,machine_id,machine_code,product,target,produced,priority,status,due_date,start_production) VALUES(?,?,?,?,?,?,?,?,?,?)",(order_no,machine_id,code,product,int(target),0,priority,"Üretimde",str(due_date),start)); c.execute("UPDATE machines SET target=?,product=?,status='Çalışıyor' WHERE id=?",(new_target,product,machine_id)); c.commit(); c.close(); st.success(f"{order_no}, {code} makinesine atandı."); st.rerun()
                except INTEGRITY_ERRORS: st.error("Bu iş emri numarası zaten var.")


if selected_module == "📡 Sensörler":
    # Canlı sensör merkezi: değer, makine sağlığı, trend ve anomali tek ekranda.
    st.markdown("""
    <style>
      .sensor-v3-title{display:flex;gap:9px;align-items:center;margin:0 0 .45rem}
      .sensor-v3-title h2{font-size:1.3rem;margin:0;color:#063d2b;font-weight:800}
      .sensor-v3-title p{font-size:.76rem;color:#6a8176;margin:0}
      .sensor-v3-stat{background:#fff;border:1px solid #dcefe6;border-radius:8px;min-height:73px;padding:9px 10px;box-shadow:0 3px 10px rgba(9,70,47,.055);position:relative;overflow:hidden}
      .sensor-v3-stat:before{content:'';position:absolute;left:0;top:0;bottom:0;width:4px;background:#12a265}
      .sensor-v3-stat span{width:25px;height:25px;display:inline-flex;align-items:center;justify-content:center;border-radius:50%;background:#0b8d58;color:#fff;font-size:.8rem}
      .sensor-v3-stat label{font-size:.65rem;color:#516b60;font-weight:800;margin-left:5px;vertical-align:top;line-height:25px}
      .sensor-v3-stat strong{display:block;font-size:1.24rem;margin:-1px 0 0 31px;color:#073e2b}
      .sensor-v3-stat small{display:block;margin-left:31px;color:#18a465;font-size:.58rem}
      .sensor-v3-panel-title{font-size:.88rem;color:#0a4934;font-weight:800;margin:0 0 .42rem}
      .sensor-v3-machine-name{font-size:.84rem;font-weight:800;color:#093f2d}
      .sensor-v3-status{font-size:.62rem;padding:3px 7px;border-radius:99px;background:#e4f8ed;color:#128553;font-weight:700;float:right}
      .sensor-v3-reading{font-size:.65rem;color:#61796e}.sensor-v3-reading b{font-size:.77rem;color:#0a4933;display:block;margin-top:2px}
      .sensor-v3-reading small{font-size:.56rem;color:#81958b}
      .loss-hunter{background:linear-gradient(125deg,#063f2d 0%,#087449 58%,#0c9860 100%);border:1px solid #087449;border-radius:11px;padding:13px 15px;color:#fff;min-height:138px;box-shadow:0 8px 22px rgba(4,71,45,.16)}
      .loss-hunter-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:9px}.loss-hunter-head b{font-size:.96rem}.loss-hunter-head span{font-size:.59rem;background:rgba(255,255,255,.16);padding:4px 8px;border-radius:99px}
      .loss-hunter-main{display:grid;grid-template-columns:1.1fr .72fr .72fr;gap:9px}.loss-hunter-main div{border-left:1px solid rgba(255,255,255,.22);padding-left:9px}.loss-hunter-main div:first-child{border-left:0;padding-left:0}
      .loss-hunter-main small{display:block;color:#bcebd5;font-size:.58rem;text-transform:uppercase;letter-spacing:.03em}.loss-hunter-main strong{display:block;font-size:1.08rem;margin-top:2px}.loss-hunter-note{font-size:.65rem;color:#e4fff2;margin-top:10px;line-height:1.35}
      .loss-rank{display:grid;grid-template-columns:33px 1fr 55px;align-items:center;gap:7px;padding:7px 1px;border-bottom:1px solid #e1efe8}.loss-rank:last-child{border-bottom:0}.loss-rank-index{width:25px;height:25px;border-radius:7px;background:#e8f8ef;color:#087449;font-size:.65rem;font-weight:800;display:flex;align-items:center;justify-content:center}.loss-rank b{font-size:.68rem;color:#153f31}.loss-rank small{display:block;font-size:.57rem;color:#789084}.loss-rank-value{text-align:right;font-size:.68rem;font-weight:800;color:#d06821}
      .loss-action{background:#fff8e8;border:1px solid #f5d79f;border-radius:9px;padding:10px 12px;min-height:138px}.loss-action-title{font-size:.72rem;color:#a9610d;font-weight:800;margin-bottom:5px}.loss-action strong{font-size:.79rem;color:#184b38}.loss-action p{font-size:.64rem;color:#5d7168;line-height:1.45;margin:6px 0 0}
    </style>
    """, unsafe_allow_html=True)
    st.markdown("""<div class="sensor-v3-title"><span style="font-size:1.2rem">◉</span><div><h2>Sensör İzleme Merkezi</h2><p>Makine sağlığı, canlı sensör değerleri ve anomali uyarıları.</p></div></div>""", unsafe_allow_html=True)

    sensors_v3 = q("""
        SELECT s.machine_id,s.machine_code,s.temperature,s.vibration,s.pressure,s.rpm,s.timestamp,
               COALESCE(m.status, 'Beklemede') AS machine_status,COALESCE(m.production, 0) AS production,
               COALESCE(m.target, 0) AS target,COALESCE(m.product, '—') AS product
        FROM sensors s LEFT JOIN machines m ON m.machine_code=s.machine_code
        ORDER BY s.machine_code
    """)
    history_v3 = q("SELECT machine_code,temperature,vibration,pressure,rpm,timestamp FROM sensor_history ORDER BY timestamp DESC LIMIT 1000")
    energy_v3 = q("SELECT COALESCE(SUM(energy_kwh),0) AS total_energy FROM energy_readings")
    sensor_thresholds_v3 = get_sensor_thresholds()
    for metric_v3 in ["temperature", "vibration", "pressure", "rpm"]:
        if metric_v3 not in sensors_v3.columns: sensors_v3[metric_v3] = 0
        sensors_v3[metric_v3] = pd.to_numeric(sensors_v3[metric_v3], errors="coerce").fillna(0)
    sensors_v3["_sensor_status"] = "Normal"
    sensors_v3.loc[(sensors_v3["temperature"] >= sensor_thresholds_v3["temperature_critical"]) | (sensors_v3["vibration"] >= sensor_thresholds_v3["vibration_critical"]) | (sensors_v3["pressure"] <= sensor_thresholds_v3["pressure_critical"]), "_sensor_status"] = "Kritik"
    sensors_v3.loc[(sensors_v3["_sensor_status"] == "Normal") & ((sensors_v3["temperature"] >= sensor_thresholds_v3["temperature_warning"]) | (sensors_v3["vibration"] >= sensor_thresholds_v3["vibration_warning"]) | (sensors_v3["pressure"] <= sensor_thresholds_v3["pressure_warning"])), "_sensor_status"] = "Uyarı"
    history_v3["_zaman"] = pd.to_datetime(history_v3["timestamp"], errors="coerce") if not history_v3.empty else pd.Series(dtype="datetime64[ns]")
    history_v3["_tarih"] = history_v3["_zaman"].dt.date if not history_v3.empty else pd.Series(dtype="object")

    sensor_filter_machine, sensor_filter_type, sensor_filter_status, sensor_filter_date, sensor_filter_search, sensor_refresh = st.columns([1.05, 1.05, 1.0, 1.45, 1.5, .88])
    with sensor_filter_machine:
        sensor_machine_filter_v3 = st.selectbox("Makine", ["Tümü"] + sorted(sensors_v3["machine_code"].dropna().astype(str).unique().tolist()), key="sensor_v3_machine_filter")
    with sensor_filter_type:
        sensor_type_v3 = st.selectbox("Sensör türü", ["Tümü", "Sıcaklık", "Titreşim", "Basınç", "RPM"], key="sensor_v3_type_filter")
    with sensor_filter_status:
        sensor_status_filter_v3 = st.selectbox("Durum", ["Tümü", "Normal", "Uyarı", "Kritik"], key="sensor_v3_status_filter")
    with sensor_filter_date:
        sensor_dates_v3 = st.date_input("Tarih aralığı", value=(date.today(), date.today()), key="sensor_v3_dates")
    with sensor_filter_search:
        sensor_search_v3 = st.text_input("Makine veya sensör ara", placeholder="CNC-01 veya sıcaklık...", key="sensor_v3_search")
    with sensor_refresh:
        st.write("")
        if st.button("◌ Canlı izle", type="primary", use_container_width=True, key="sensor_v3_refresh"):
            st.rerun()

    visible_sensors_v3 = sensors_v3.copy()
    if sensor_machine_filter_v3 != "Tümü": visible_sensors_v3 = visible_sensors_v3[visible_sensors_v3["machine_code"] == sensor_machine_filter_v3]
    if sensor_status_filter_v3 != "Tümü": visible_sensors_v3 = visible_sensors_v3[visible_sensors_v3["_sensor_status"] == sensor_status_filter_v3]
    if sensor_search_v3.strip():
        sensor_needle_v3 = sensor_search_v3.strip().lower()
        sensor_search_text_v3 = visible_sensors_v3["machine_code"].fillna("").astype(str) + " " + visible_sensors_v3["_sensor_status"].astype(str)
        visible_sensors_v3 = visible_sensors_v3[sensor_search_text_v3.str.lower().str.contains(sensor_needle_v3, na=False)]
    avg_temperature_v3 = float(visible_sensors_v3["temperature"].mean()) if not visible_sensors_v3.empty else 0.0
    avg_vibration_v3 = float(visible_sensors_v3["vibration"].mean()) if not visible_sensors_v3.empty else 0.0
    avg_pressure_v3 = float(visible_sensors_v3["pressure"].mean()) if not visible_sensors_v3.empty else 0.0
    avg_rpm_v3 = float(visible_sensors_v3["rpm"].mean()) if not visible_sensors_v3.empty else 0.0
    total_energy_v3 = float(energy_v3.iloc[0]["total_energy"]) if not energy_v3.empty else 0.0
    sensor_stats_v3 = [("♨", "Ortalama Sıcaklık", f"{avg_temperature_v3:.1f} °C", f'Kritik: {sensor_thresholds_v3["temperature_critical"]:.0f}°C'), ("⌁", "Ortalama Titreşim", f"{avg_vibration_v3:.1f} mm/s", f'Kritik: {sensor_thresholds_v3["vibration_critical"]:.1f} mm/s'), ("◉", "Ortalama Basınç", f"{avg_pressure_v3:.1f} bar", f'Alt limit: {sensor_thresholds_v3["pressure_warning"]:.1f} bar'), ("↻", "Ortalama RPM", f"{avg_rpm_v3:.0f}", "Nominal devir"), ("ϟ", "Toplam Enerji Tüketimi", f"{total_energy_v3:.1f} kWh", "Kayıtlı toplam")]
    sensor_stat_cols_v3 = st.columns(5)
    for sensor_stat_col_v3, (sensor_icon_v3, sensor_label_v3, sensor_value_v3, sensor_hint_v3) in zip(sensor_stat_cols_v3, sensor_stats_v3):
        with sensor_stat_col_v3:
            st.markdown(f'<div class="sensor-v3-stat"><span>{sensor_icon_v3}</span><label>{sensor_label_v3}</label><strong>{sensor_value_v3}</strong><small>{sensor_hint_v3}</small></div>', unsafe_allow_html=True)

    if isinstance(sensor_dates_v3, (tuple, list)) and len(sensor_dates_v3) == 2:
        sensor_start_v3, sensor_end_v3 = sensor_dates_v3
    else:
        sensor_start_v3 = sensor_end_v3 = sensor_dates_v3
    visible_history_v3 = history_v3.copy()
    if sensor_start_v3 and sensor_end_v3 and not visible_history_v3.empty:
        visible_history_v3 = visible_history_v3[(visible_history_v3["_tarih"] >= sensor_start_v3) & (visible_history_v3["_tarih"] <= sensor_end_v3)]
    if sensor_machine_filter_v3 != "Tümü" and not visible_history_v3.empty:
        visible_history_v3 = visible_history_v3[visible_history_v3["machine_code"] == sensor_machine_filter_v3]

    st.markdown('<div class="sensor-v3-panel-title" style="margin-top:.65rem">▣ Makine Sensör Durumları <span style="float:right;font-size:.66rem;color:#639183">Anlık değerler</span></div>', unsafe_allow_html=True)
    if visible_sensors_v3.empty:
        st.info("Seçili filtrede sensör kaydı bulunmuyor.")
    else:
        sensor_machine_cols_v3 = st.columns(min(3, len(visible_sensors_v3)))
        for sensor_col_v3, (_, sensor_row_v3) in zip(sensor_machine_cols_v3, visible_sensors_v3.iterrows()):
            with sensor_col_v3:
                with st.container(border=True):
                    status_color_v3 = {"Normal":"#e4f8ed", "Uyarı":"#fff3d9", "Kritik":"#ffe6e7"}.get(sensor_row_v3["_sensor_status"], "#e4f8ed")
                    status_text_v3 = {"Normal":"#128553", "Uyarı":"#b66e00", "Kritik":"#c73840"}.get(sensor_row_v3["_sensor_status"], "#128553")
                    st.markdown(f'<div class="sensor-v3-machine-name">▣ {sensor_row_v3["machine_code"]}<span class="sensor-v3-status" style="background:{status_color_v3};color:{status_text_v3}">{sensor_row_v3["_sensor_status"]}</span></div>', unsafe_allow_html=True)
                    sensor_kpi_a_v3, sensor_kpi_b_v3, sensor_kpi_c_v3, sensor_kpi_d_v3 = st.columns(4)
                    for sensor_kpi_col_v3, sensor_metric_name_v3, sensor_metric_value_v3, sensor_metric_limit_v3 in zip([sensor_kpi_a_v3, sensor_kpi_b_v3, sensor_kpi_c_v3, sensor_kpi_d_v3], ["Sıcaklık", "Titreşim", "Basınç", "RPM"], [f'{sensor_row_v3["temperature"]:.1f}°C', f'{sensor_row_v3["vibration"]:.1f}', f'{sensor_row_v3["pressure"]:.1f}', f'{sensor_row_v3["rpm"]:.0f}'], [f'{sensor_thresholds_v3["temperature_critical"]:.0f}°C', f'{sensor_thresholds_v3["vibration_critical"]:.1f}', f'{sensor_thresholds_v3["pressure_warning"]:.1f}', "1800"]):
                        with sensor_kpi_col_v3:
                            st.markdown(f'<div class="sensor-v3-reading">{sensor_metric_name_v3}<b>{sensor_metric_value_v3}</b><small>Limit: {sensor_metric_limit_v3}</small></div>', unsafe_allow_html=True)
                    local_history_v3 = visible_history_v3[visible_history_v3["machine_code"] == sensor_row_v3["machine_code"]].sort_values("_zaman").tail(24) if not visible_history_v3.empty else pd.DataFrame()
                    if not local_history_v3.empty:
                        mini_metric_v3 = {"Sıcaklık":"temperature", "Titreşim":"vibration", "Basınç":"pressure", "RPM":"rpm"}.get(sensor_type_v3, "temperature")
                        mini_colour_v3 = "#e6585d" if sensor_row_v3["_sensor_status"] == "Kritik" else "#0b9b5e"
                        mini_chart_v3 = go.Figure(go.Scatter(x=local_history_v3["_zaman"], y=local_history_v3[mini_metric_v3], mode="lines", line=dict(color=mini_colour_v3, width=2), fill="tozeroy", fillcolor="rgba(11,155,94,.08)"))
                        mini_chart_v3.update_layout(height=96, margin=dict(l=0,r=0,t=8,b=0), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", xaxis=dict(visible=False), yaxis=dict(visible=False))
                        st.plotly_chart(mini_chart_v3, use_container_width=True, config={"displayModeBar":False}, key=f'sensor_v3_mini_{sensor_row_v3["machine_code"]}')

    # Kayıp Avcısı: duruş sürelerini çevrim hızı ve canlı sensör durumu ile birleştirir.
    loss_records_v3 = q("""
        SELECT d.id,d.machine_code,d.reason,d.duration,d.event_at,
               COALESCE(NULLIF(m.ideal_cycle,0),1) AS ideal_cycle
        FROM downtime d LEFT JOIN machines m ON m.machine_code=d.machine_code
        ORDER BY d.id DESC
    """)
    loss_scope_note_v3 = "Seçili tarih aralığı"
    if not loss_records_v3.empty:
        loss_records_v3["duration"] = pd.to_numeric(loss_records_v3["duration"], errors="coerce").fillna(0).clip(lower=0)
        loss_records_v3["ideal_cycle"] = pd.to_numeric(loss_records_v3["ideal_cycle"], errors="coerce").replace(0, 1).fillna(1)
        loss_records_v3["_event_time"] = pd.to_datetime(loss_records_v3["event_at"], errors="coerce")
        if sensor_machine_filter_v3 != "Tümü":
            loss_records_v3 = loss_records_v3[loss_records_v3["machine_code"] == sensor_machine_filter_v3]
        dated_loss_v3 = loss_records_v3[loss_records_v3["_event_time"].notna()].copy()
        if sensor_start_v3 and sensor_end_v3 and not dated_loss_v3.empty:
            dated_loss_v3 = dated_loss_v3[
                (dated_loss_v3["_event_time"].dt.date >= sensor_start_v3)
                & (dated_loss_v3["_event_time"].dt.date <= sensor_end_v3)
            ]
        if not dated_loss_v3.empty:
            loss_records_v3 = dated_loss_v3
        else:
            loss_records_v3 = loss_records_v3.head(50)
            loss_scope_note_v3 = "Seçili tarihte kayıt yok · son kayıtlar"
        loss_records_v3["estimated_units"] = (loss_records_v3["duration"] / loss_records_v3["ideal_cycle"]).round(0)

    if loss_records_v3.empty:
        loss_summary_v3 = pd.DataFrame(columns=["machine_code", "reason", "duration", "estimated_units"])
    else:
        loss_summary_v3 = (
            loss_records_v3.groupby(["machine_code", "reason"], as_index=False)
            .agg(duration=("duration", "sum"), estimated_units=("estimated_units", "sum"))
            .sort_values(["duration", "estimated_units"], ascending=False)
        )

    st.markdown('<div class="sensor-v3-panel-title" style="margin-top:.72rem">🔎 Kayıp Avcısı <span style="float:right;font-size:.64rem;color:#639183">Duruş + çevrim + sensör analizi</span></div>', unsafe_allow_html=True)
    loss_main_col_v3, loss_rank_col_v3, loss_action_col_v3 = st.columns([1.5, 1.05, 1.05], gap="small")
    if loss_summary_v3.empty:
        with loss_main_col_v3:
            st.info("Analiz edilebilecek duruş kaydı bulunmuyor. Duruş kaydı eklendiğinde Kayıp Avcısı otomatik çalışır.")
        with loss_rank_col_v3:
            st.caption("Kayıp sıralaması için veri bekleniyor.")
        with loss_action_col_v3:
            st.caption("Sensör ve duruş kayıtlarından aksiyon önerisi üretilecek.")
    else:
        top_loss_v3 = loss_summary_v3.iloc[0]
        total_loss_minutes_v3 = float(loss_summary_v3["duration"].sum())
        total_loss_units_v3 = int(round(float(loss_summary_v3["estimated_units"].sum())))
        top_loss_share_v3 = (float(top_loss_v3["duration"]) / total_loss_minutes_v3 * 100) if total_loss_minutes_v3 else 0
        top_machine_v3 = str(top_loss_v3["machine_code"])
        top_reason_v3 = str(top_loss_v3["reason"] or "Belirtilmedi")
        safe_machine_v3 = html.escape(top_machine_v3)
        safe_reason_v3 = html.escape(top_reason_v3)

        current_loss_sensor_v3 = sensors_v3[sensors_v3["machine_code"] == top_machine_v3]
        sensor_finding_v3 = "Canlı sensör değerleri normal aralıkta."
        sensor_action_v3 = "Duruş nedenini, operatör notunu ve tekrar sıklığını kontrol et."
        if not current_loss_sensor_v3.empty:
            loss_sensor_row_v3 = current_loss_sensor_v3.iloc[0]
            if float(loss_sensor_row_v3["temperature"]) >= sensor_thresholds_v3["temperature_warning"]:
                sensor_finding_v3 = f'Sıcaklık {float(loss_sensor_row_v3["temperature"]):.1f}°C ile yüksek.'
                sensor_action_v3 = "Soğutma hattı, fanlar ve takım yükünü kontrol et; bakım görevi aç."
            elif float(loss_sensor_row_v3["vibration"]) >= sensor_thresholds_v3["vibration_warning"]:
                sensor_finding_v3 = f'Titreşim {float(loss_sensor_row_v3["vibration"]):.1f} mm/s ile yüksek.'
                sensor_action_v3 = "Rulman, balans ve takım tutucuyu kontrol et; titreşim trendini izle."
            elif float(loss_sensor_row_v3["pressure"]) <= sensor_thresholds_v3["pressure_warning"]:
                sensor_finding_v3 = f'Basınç {float(loss_sensor_row_v3["pressure"]):.1f} bar ile düşük.'
                sensor_action_v3 = "Hava/hidrolik hattını, filtreyi ve olası kaçakları kontrol et."
            elif "malzeme" in top_reason_v3.lower():
                sensor_action_v3 = "Malzeme besleme ve kritik stok seviyelerini üretim planıyla eşleştir."
            elif "operatör" in top_reason_v3.lower():
                sensor_action_v3 = "Vardiya atamasını ve operatör yanıt süresini kontrol et."
            elif "bakım" in top_reason_v3.lower() or "arıza" in top_reason_v3.lower():
                sensor_action_v3 = "Tekrarlayan arıza kaydını incele ve önleyici bakım görevi oluştur."

        with loss_main_col_v3:
            st.markdown(f'''<div class="loss-hunter"><div class="loss-hunter-head"><b>🎯 En büyük kayıp bulundu</b><span>{html.escape(loss_scope_note_v3)}</span></div><div class="loss-hunter-main"><div><small>Makine · Neden</small><strong>{safe_machine_v3} · {safe_reason_v3}</strong></div><div><small>Toplam kayıp</small><strong>{float(top_loss_v3["duration"]):.0f} dk</strong></div><div><small>Tahmini üretim</small><strong>{int(round(float(top_loss_v3["estimated_units"]))):,} adet</strong></div></div><div class="loss-hunter-note">Bu neden analiz edilen kayıp süresinin <b>%{top_loss_share_v3:.1f}</b>'ini oluşturuyor. Toplam görünür kayıp: <b>{total_loss_minutes_v3:.0f} dk / yaklaşık {total_loss_units_v3:,} adet</b>.</div></div>''', unsafe_allow_html=True)
        with loss_rank_col_v3:
            with st.container(border=True):
                st.markdown('<div class="sensor-v3-panel-title">En Büyük 3 Kayıp</div>', unsafe_allow_html=True)
                for loss_rank_index_v3, (_, loss_rank_row_v3) in enumerate(loss_summary_v3.head(3).iterrows(), 1):
                    st.markdown(f'''<div class="loss-rank"><span class="loss-rank-index">{loss_rank_index_v3}</span><div><b>{html.escape(str(loss_rank_row_v3["machine_code"]))} · {html.escape(str(loss_rank_row_v3["reason"]))}</b><small>≈ {int(round(float(loss_rank_row_v3["estimated_units"]))):,} adet</small></div><span class="loss-rank-value">{float(loss_rank_row_v3["duration"]):.0f} dk</span></div>''', unsafe_allow_html=True)
        with loss_action_col_v3:
            st.markdown(f'''<div class="loss-action"><div class="loss-action-title">⚡ Önerilen ilk aksiyon</div><strong>{html.escape(sensor_finding_v3)}</strong><p>{html.escape(sensor_action_v3)}</p></div>''', unsafe_allow_html=True)
            if st.button("Duruş kayıtlarını incele →", use_container_width=True, key="loss_hunter_open_downtime"):
                st.session_state["selected_module"] = "⏱️ Duruşlar"
                st.rerun()

    sensor_bottom_left_v3, sensor_bottom_mid_v3, sensor_bottom_right_v3 = st.columns([1.05, 1.5, 1.02], gap="small")
    with sensor_bottom_left_v3:
        with st.container(border=True):
            st.markdown('<div class="sensor-v3-panel-title">▧ Sensör Detayları</div>', unsafe_allow_html=True)
            detail_sensors_v3 = visible_sensors_v3[["machine_code", "temperature", "vibration", "pressure", "rpm", "_sensor_status"]].copy()
            detail_sensors_v3 = detail_sensors_v3.rename(columns={"machine_code":"Makine", "temperature":"Sıcaklık", "vibration":"Titreşim", "pressure":"Basınç", "rpm":"RPM", "_sensor_status":"Durum"})
            detail_sensors_v3["Sıcaklık"] = detail_sensors_v3["Sıcaklık"].map(lambda v: f"{v:.1f} °C")
            detail_sensors_v3["Titreşim"] = detail_sensors_v3["Titreşim"].map(lambda v: f"{v:.1f} mm/s")
            detail_sensors_v3["Basınç"] = detail_sensors_v3["Basınç"].map(lambda v: f"{v:.1f} bar")
            st.dataframe(detail_sensors_v3, use_container_width=True, hide_index=True, height=190)
    with sensor_bottom_mid_v3:
        with st.container(border=True):
            sensor_trend_title_v3 = {"Sıcaklık":"Sıcaklık (°C)", "Titreşim":"Titreşim (mm/s)", "Basınç":"Basınç (bar)", "RPM":"Devir (RPM)"}.get(sensor_type_v3, "Sıcaklık (°C)")
            sensor_trend_metric_v3 = {"Sıcaklık":"temperature", "Titreşim":"vibration", "Basınç":"pressure", "RPM":"rpm"}.get(sensor_type_v3, "temperature")
            st.markdown(f'<div class="sensor-v3-panel-title">⌁ Sensör Trendleri <span style="float:right;font-size:.66rem;color:#639183">{sensor_trend_title_v3}</span></div>', unsafe_allow_html=True)
            if visible_history_v3.empty:
                st.caption("Tarih aralığında trend verisi bulunmuyor.")
            else:
                trend_sensor_v3 = px.line(visible_history_v3.sort_values("_zaman"), x="_zaman", y=sensor_trend_metric_v3, color="machine_code", markers=True, color_discrete_sequence=["#0b9b5e", "#e9a23a", "#e6585d"])
                trend_sensor_v3.update_layout(height=217, margin=dict(l=0,r=0,t=5,b=0), legend=dict(orientation="h", y=1.12, font=dict(size=8)), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", xaxis=dict(title=None, tickfont=dict(size=8)), yaxis=dict(title=None, tickfont=dict(size=8)))
                st.plotly_chart(trend_sensor_v3, use_container_width=True, config={"displayModeBar":False}, key="sensor_v3_main_trend")
    with sensor_bottom_right_v3:
        with st.container(border=True):
            st.markdown('<div class="sensor-v3-panel-title">⚠ Anomali Uyarıları <span style="float:right;font-size:.65rem;color:#0b9f61">Tümü ›</span></div>', unsafe_allow_html=True)
            anomalies_v3 = visible_sensors_v3[visible_sensors_v3["_sensor_status"] != "Normal"].copy()
            if anomalies_v3.empty:
                st.success("Aktif sensör anomalisi yok.")
            else:
                for _, anomaly_v3 in anomalies_v3.iterrows():
                    anomaly_colour_v3 = "#e6585d" if anomaly_v3["_sensor_status"] == "Kritik" else "#e9a23a"
                    issue_v3 = "Sıcaklık yüksek" if anomaly_v3["temperature"] >= sensor_thresholds_v3["temperature_warning"] else ("Titreşim yüksek" if anomaly_v3["vibration"] >= sensor_thresholds_v3["vibration_warning"] else "Basınç düşük")
                    st.markdown(f'<div style="border-left:3px solid {anomaly_colour_v3};padding:5px 7px;margin:2px 0;background:#fff;border-radius:4px"><b style="font-size:.7rem;color:#174b39">{anomaly_v3["machine_code"]} · {issue_v3}</b><br><span style="font-size:.62rem;color:#71877b">{anomaly_v3["_sensor_status"]} · {anomaly_v3["timestamp"]}</span></div>', unsafe_allow_html=True)
        with st.container(border=True):
            st.markdown('<div class="sensor-v3-panel-title">⚡ Hızlı İşlemler</div>', unsafe_allow_html=True)
            if st.button("⌁ Sensör Geçmişi", use_container_width=True, key="sensor_v3_history_action"):
                st.info("Trend grafiğini tarih ve makine filtresiyle inceleyebilirsin.")
            if st.button("▣ Rapor Al", use_container_width=True, key="sensor_v3_report_action"):
                st.session_state["selected_module"] = "📄 Raporlar"
                st.rerun()
            if st.button("⚙ Alarm Ayarları", use_container_width=True, key="sensor_v3_alarm_action"):
                st.session_state["selected_module"] = "🚨 Alarmlar"
                st.rerun()

    st.stop()


if selected_module == "📡 Sensörler":
    st.subheader("📡 Sensörler")
    sensors=q("SELECT machine_code,temperature,vibration,pressure,rpm,timestamp FROM sensors ORDER BY machine_code")
    st.dataframe(sensors,use_container_width=True,hide_index=True)
    sensor_history=q("SELECT machine_code,temperature,vibration,pressure,rpm,timestamp FROM sensor_history ORDER BY timestamp")
    if sensor_history.empty:
        st.info("Sensör geçmişi için simülasyonu çalıştırın.")
    else:
        selected_sensor_machine=st.selectbox("Makine",sorted(sensor_history["machine_code"].unique()),key="sensor_history_machine")
        sh=sensor_history[sensor_history["machine_code"]==selected_sensor_machine].copy(); sh["timestamp"]=pd.to_datetime(sh["timestamp"],errors="coerce")
        sensor_specs=[("temperature","Sıcaklık (°C)"),("vibration","Titreşim (mm/s)"),("pressure","Basınç (bar)"),("rpm","Devir (RPM)")]
        cols=st.columns(2)
        for i,(field,label) in enumerate(sensor_specs):
            with cols[i%2]:
                fig=px.line(sh,x="timestamp",y=field,markers=True,title=f"{selected_sensor_machine} · {label} Geçmişi",labels={field:label,"timestamp":"Zaman"})
                fig.update_layout(height=300,margin=dict(l=20,r=15,t=45,b=25),hovermode="x unified")
                st.plotly_chart(fig,use_container_width=True,key=f"sensor_{field}_{selected_sensor_machine}",config={"displayModeBar":False})


if selected_module == "⏱️ Duruşlar":
    from downtime_panel import render_downtime_panel
    render_downtime_panel(q, df)
    machine_codes = df["machine_code"].tolist()
    if st.session_state.pop("downtime_record_created", False):
        st.success("Duruş kaydı oluşturuldu ve analizlere eklendi.")

    @st.dialog("Yeni Duruş Kaydı", width="large")
    def downtime_record_dialog():
        if st.button("Kapat", key="downtime_dialog_close"):
            st.session_state["dt_new"] = False
            st.rerun()
        if not has_role("admin", "maintenance"):
            st.info("Duruş kaydı oluşturmak için bakım veya yönetici yetkisi gerekir.")
            return
        if not machine_codes:
            st.warning("Duruş kaydı oluşturulabilecek makine bulunmuyor.")
            return
        with st.form("downtime_form"):
            machine=st.selectbox("Makine",machine_codes,key="downtime_machine"); reason=st.selectbox("Neden",["Arıza","Bakım","Malzeme Bekleme","Operatör Bekleme","Kalite Problemi","Planlı Duruş","Diğer"]); duration=st.number_input("Süre (dk)",min_value=1.0,value=10.0); note=st.text_input("Not")
            if st.form_submit_button("Duruş Kaydet", type="primary", use_container_width=True):
                machine_id=int(q("SELECT id FROM machines WHERE machine_code=?",(machine,)).iloc[0]["id"]); execute("INSERT INTO downtime(machine_id,machine_code,reason,duration,start_time,notes,event_at) VALUES(?,?,?,?,?,?,?)",(machine_id,machine,reason,duration,datetime.now().strftime("%H:%M"),note,now())); execute("UPDATE machines SET downtime=downtime+? WHERE machine_code=?",(duration,machine)); audit_event("Duruş kaydı oluşturdu", machine, f"{reason} · {duration:.1f} dk")
                st.session_state["dt_new"] = False
                st.session_state["downtime_record_created"] = True
                st.rerun()

    if st.session_state.get("dt_new", False):
        downtime_record_dialog()


if selected_module == "👷 Vardiya":
    from shift_panel import render_shift_panel
    render_shift_panel(q, SHIFT_SCHEDULE)
    active_shift = active_shift_name()

    if is_admin():
        st.markdown("#### Makineye operatör ata")
        operator_names = q("SELECT name FROM operators WHERE active=1 AND department='Üretim' ORDER BY name")["name"].tolist()
        with st.form("shift_machine_assignment_form", clear_on_submit=False):
            assign_shift, assign_machine, assign_operator = st.columns(3)
            with assign_shift: selected_shift = st.selectbox("Vardiya", ["Sabah", "Akşam", "Gece"])
            with assign_machine: selected_machine = st.selectbox("Makine", df["machine_code"].tolist())
            with assign_operator: selected_operator = st.selectbox("Operatör", operator_names)
            if st.form_submit_button("Atamayı Kaydet", use_container_width=True):
                execute("""
                    INSERT INTO shift_machine_assignments(shift_name,machine_code,operator_name,active)
                    VALUES(?,?,?,1)
                    ON CONFLICT(shift_name,machine_code) DO UPDATE SET operator_name=excluded.operator_name,active=1
                """, (selected_shift, selected_machine, selected_operator))
                if selected_shift == active_shift:
                    execute("UPDATE machines SET operator=?,shift=? WHERE machine_code=?", (selected_operator, selected_shift, selected_machine))
                audit_event("Vardiya makine atamasını güncelledi", selected_machine, f"{selected_shift} · {selected_operator}")
                st.success("Vardiya ataması kaydedildi.")
                st.rerun()

    st.markdown("#### Vardiya tanımları")
    st.dataframe(q("SELECT shift_name,start_time,end_time,supervisor,status FROM shifts ORDER BY id"),use_container_width=True,hide_index=True)


if selected_module == "✅ Kalite":
    # Streamlit Cloud dosyaları ardışık commitlerde güncellerken eski importu
    # bellekte tutabilir. Modülü burada yenilemek imza uyuşmazlığını önler.
    import importlib
    import quality_panel
    quality_panel = importlib.reload(quality_panel)
    quality_panel.render_quality_panel(q, execute, df, has_role)
    if st.session_state.pop("quality_record_created", False):
        st.success("Kalite kaydı oluşturuldu ve analizlere eklendi.")

    @st.dialog("Yeni Kalite Kaydı", width="large")
    def quality_record_dialog():
        if st.button("Kapat", key="quality_dialog_close"):
            st.session_state["quality_new_open"] = False
            st.rerun()
        if not has_role("admin", "quality"):
            st.info("Kalite kaydı oluşturmak için kalite veya yönetici yetkisi gerekir.")
            return
        if df.empty:
            st.warning("Kalite kaydı oluşturulabilecek makine bulunmuyor.")
            return
        with st.form("quality_new_record_form"):
            quality_machine = st.selectbox("Makine", df["machine_code"].tolist(), key="quality_new_machine")
            selected_quality_machine = df[df["machine_code"] == quality_machine].iloc[0]
            quality_product = st.text_input("Ürün", str(selected_quality_machine["product"]), key="quality_new_product")
            quality_produced = st.number_input("Kontrol edilen miktar", min_value=1, value=100, key="quality_new_produced")
            quality_defective = st.number_input("Hatalı miktar", min_value=0, max_value=int(quality_produced), value=0, key="quality_new_defective")
            quality_reason = st.selectbox("Hata türü", ["Ölçü Hatası", "Yüzey Hatası", "Çapak", "Çatlak", "Diğer", "Hata yok"], key="quality_new_reason")
            if st.form_submit_button("Kalite kaydını oluştur", type="primary", use_container_width=True):
                machine_id = int(selected_quality_machine["id"])
                execute("INSERT INTO quality(machine_id,machine_code,product,produced,defective,defect_reason,timestamp) VALUES(?,?,?,?,?,?,?)", (machine_id, quality_machine, quality_product, int(quality_produced), int(quality_defective), quality_reason if quality_defective else "Hata yok", now()))
                if quality_defective:
                    execute("UPDATE machines SET defective=defective+? WHERE id=?", (int(quality_defective), machine_id))
                audit_event("Kalite kaydı oluşturdu", quality_machine, f"{quality_product} · {quality_defective}/{quality_produced}")
                st.session_state["quality_new_open"] = False
                st.session_state["quality_record_created"] = True
                st.rerun()

    if st.session_state.get("quality_new_open", False):
        quality_record_dialog()


if selected_module == "🌐 Dijital Olgunluk":
    import importlib
    import maturity_panel
    maturity_panel = importlib.reload(maturity_panel)
    maturity_panel.render_maturity_panel(q, execute, using_postgres=USING_POSTGRES, api_configured=bool(API_URL))
    st.stop()


if selected_module == "🔧 Bakım":
    from maintenance_panel import render_maintenance_panel

    if st.session_state.pop("maintenance_record_created", False):
        st.success("Bakım kaydı oluşturuldu.")
    if st.session_state.pop("maintenance_record_updated", False):
        st.success("Bakım durumu güncellendi.")
    maintenance_records = render_maintenance_panel(q)

    @st.dialog("Yeni Bakım Kaydı", width="large")
    def maintenance_new_dialog():
        if not has_role("admin", "maintenance"):
            st.info("Bakım kaydı oluşturmak için Bakım veya Admin yetkisi gerekir.")
            return
        if df.empty:
            st.warning("Bakım kaydı oluşturulabilecek makine bulunmuyor.")
            return
        with st.form("maintenance_new_dialog_form"):
            machine = st.selectbox("Makine", df["machine_code"].tolist(), key="maintenance_new_machine_v3")
            maintenance_type = st.selectbox("Bakım tipi", ["Periyodik Bakım", "Arıza Bakımı", "Önleyici Bakım", "Revizyon"], key="maintenance_new_type_v3")
            description = st.text_input("Açıklama", placeholder="Yapılacak bakım işlemini yazın", key="maintenance_new_description_v3")
            form_cols = st.columns(2)
            maintenance_date = form_cols[0].date_input("Bakım tarihi", date.today(), key="maintenance_new_date_v3")
            next_date = form_cols[1].date_input("Sonraki bakım", date.today() + timedelta(days=30), key="maintenance_new_next_v3")
            technician_names = q("SELECT full_name FROM users WHERE role IN ('admin','maintenance') AND is_active=1 ORDER BY full_name")["full_name"].dropna().tolist()
            technician = st.selectbox("Teknisyen", technician_names or ["Teknik Servis"], key="maintenance_new_technician_v3")
            left, right = st.columns(2)
            submitted = left.form_submit_button("💾 Bakımı Kaydet", type="primary", use_container_width=True)
            cancelled = right.form_submit_button("Vazgeç", use_container_width=True)
        if cancelled:
            st.session_state["maintenance_new_open"] = False
            st.rerun()
        if submitted:
            machine_id = int(df[df["machine_code"] == machine].iloc[0]["id"])
            execute("""INSERT INTO maintenance(machine_id,machine_code,maintenance_type,description,maintenance_date,next_date,technician,status)
                       VALUES(?,?,?,?,?,?,?,?)""", (machine_id, machine, maintenance_type, description, str(maintenance_date), str(next_date), technician, "Planlandı"))
            audit_event("Bakım kaydı oluşturdu", machine, f"{maintenance_type} · {technician}")
            st.session_state["maintenance_new_open"] = False
            st.session_state["maintenance_record_created"] = True
            st.rerun()

    @st.dialog("Bakım Durumunu Güncelle", width="large")
    def maintenance_action_dialog():
        if not has_role("admin", "maintenance"):
            st.info("Bu işlem için Bakım veya Admin yetkisi gerekir.")
            return
        active_records = maintenance_records[maintenance_records["status"] != "Tamamlandı"].copy()
        if active_records.empty:
            st.success("Güncellenecek aktif bakım kaydı yok.")
            if st.button("Kapat", use_container_width=True, key="maintenance_action_empty_close"):
                st.session_state["maintenance_action_open"] = False
                st.rerun()
            return
        labels = {int(row["id"]): f"{row['machine_code']} · {row['maintenance_type']} · {row['status']}" for _, row in active_records.iterrows()}
        technicians = q("SELECT full_name FROM users WHERE role IN ('admin','maintenance') AND is_active=1 ORDER BY full_name")["full_name"].dropna().tolist()
        with st.form("maintenance_action_dialog_form"):
            record_id = st.selectbox("Bakım kaydı", list(labels), format_func=lambda item: labels[item], key="maintenance_action_record_v3")
            action = st.selectbox("İşlem", ["Bakımı başlat", "Bakımı tamamla"], key="maintenance_action_type_v3")
            technician = st.selectbox("Teknisyen", technicians or ["Teknik Servis"], key="maintenance_action_technician_v3")
            next_service = st.date_input("Sonraki bakım tarihi", date.today() + timedelta(days=30), key="maintenance_action_next_v3")
            return_to_service = st.checkbox("Tamamlanınca makineyi çalışır duruma al", value=True, key="maintenance_return_service_v3")
            left, right = st.columns(2)
            submitted = left.form_submit_button("Durumu Güncelle", type="primary", use_container_width=True)
            cancelled = right.form_submit_button("Vazgeç", use_container_width=True)
        if cancelled:
            st.session_state["maintenance_action_open"] = False
            st.rerun()
        if submitted:
            selected = active_records[active_records["id"] == record_id].iloc[0]
            previous_maintenance = q("SELECT status,technician,maintenance_date,next_date FROM maintenance WHERE id=?", (int(record_id),)).iloc[0]
            previous_machine = q("SELECT status,last_maintenance,next_maintenance FROM machines WHERE machine_code=?", (selected["machine_code"],)).iloc[0]
            if action == "Bakımı başlat":
                execute("UPDATE maintenance SET status=?,technician=? WHERE id=?", ("Bakımda", technician, int(record_id)))
                execute("UPDATE machines SET status='Arızalı' WHERE machine_code=?", (selected["machine_code"],))
                audit_event("Bakımı başlattı", selected["machine_code"], technician)
            else:
                completed_date = str(date.today())
                machine_status = "Çalışıyor" if return_to_service else "Beklemede"
                execute("UPDATE maintenance SET status=?,technician=?,maintenance_date=?,next_date=? WHERE id=?", ("Tamamlandı", technician, completed_date, str(next_service), int(record_id)))
                execute("UPDATE machines SET status=?,last_maintenance=?,next_maintenance=? WHERE machine_code=?", (machine_status, completed_date, str(next_service), selected["machine_code"]))
                audit_event("Bakımı tamamladı", selected["machine_code"], f"{technician} · {machine_status}")
            register_undo(
                f'{selected["machine_code"]} bakım durumu güncellendi',
                [
                    ("UPDATE maintenance SET status=?,technician=?,maintenance_date=?,next_date=? WHERE id=?", (
                        previous_maintenance["status"], previous_maintenance["technician"],
                        previous_maintenance["maintenance_date"], previous_maintenance["next_date"], int(record_id)
                    )),
                    ("UPDATE machines SET status=?,last_maintenance=?,next_maintenance=? WHERE machine_code=?", (
                        previous_machine["status"], previous_machine["last_maintenance"],
                        previous_machine["next_maintenance"], selected["machine_code"]
                    )),
                ],
            )
            st.session_state["maintenance_action_open"] = False
            st.session_state["maintenance_record_updated"] = True
            st.rerun()

    if st.session_state.get("maintenance_new_open", False):
        maintenance_new_dialog()
    elif st.session_state.get("maintenance_action_open", False):
        maintenance_action_dialog()


if False and selected_module == "🔧 Bakım":
    st.subheader("🔧 Bakım Yönetimi")

    maintenance = q("""
        SELECT *
        FROM maintenance
        ORDER BY next_date
    """)

    maintenance_date_col, maintenance_all_col = st.columns([1, 2])
    with maintenance_date_col:
        selected_maintenance_range = auto_date_range_filter("Bakım tarih aralığı", "maintenance_day_filter")
    with maintenance_all_col:
        show_all_maintenance = st.checkbox("Tüm tarihlerin bakımlarını göster", value=False, key="show_all_maintenance")
    if not show_all_maintenance and not maintenance.empty:
        maintenance_dates = pd.to_datetime(maintenance["maintenance_date"], errors="coerce").dt.date
        maintenance_start, maintenance_end = date_range_bounds(selected_maintenance_range)
        maintenance = maintenance[(maintenance_dates >= maintenance_start) & (maintenance_dates <= maintenance_end)].copy()

    maintenance["next_date"] = pd.to_datetime(maintenance["next_date"], errors="coerce")
    maintenance["Bakım Durumu"] = maintenance.apply(
        lambda row: "🔴 Gecikmiş"
        if pd.notna(row["next_date"]) and row["next_date"].date() < date.today() and row["status"] != "Tamamlandı"
        else ("🟡 Yaklaşıyor"
        if pd.notna(row["next_date"]) and (row["next_date"].date() - date.today()).days <= 7 and row["status"] != "Tamamlandı"
        else "🟢 Planlı"),
        axis=1
    )
    maintenance["next_date"] = maintenance["next_date"].dt.strftime("%Y-%m-%d")
    st.markdown("#### Bakım kayıtları")
    maintenance_view = maintenance[[
        "id", "machine_id", "machine_code", "maintenance_type", "description",
        "maintenance_date", "next_date", "technician", "status", "Bakım Durumu"
    ]].copy()
    maintenance_view["description"] = maintenance_view["description"].fillna("").replace("", "—")
    maintenance_view["technician"] = maintenance_view["technician"].fillna("Atanmadı")
    maintenance_view = maintenance_view.rename(columns={
        "id": "#",
        "machine_id": "Makine ID",
        "machine_code": "Makine",
        "maintenance_type": "Bakım Tipi",
        "description": "Açıklama",
        "maintenance_date": "Bakım Tarihi",
        "next_date": "Sonraki Tarih",
        "technician": "Teknisyen",
        "status": "Durum",
    })
    render_management_table(maintenance_view, "Bakım Listesi")

    if has_role("admin", "maintenance"):
        active_maintenance = maintenance[maintenance["status"] != "Tamamlandı"].copy()
        if not active_maintenance.empty:
            st.markdown("#### Planlı bakım işlemi")
            maintenance_labels = {
                int(row["id"]): f"{row['machine_code']} · {row['maintenance_type']} · {row['status']}"
                for _, row in active_maintenance.iterrows()
            }
            technician_names = q("SELECT full_name FROM users WHERE role IN ('admin','maintenance') AND is_active=1 ORDER BY full_name")["full_name"].dropna().tolist()
            with st.form("maintenance_status_form"):
                maintenance_id = st.selectbox("Bakım kaydı", list(maintenance_labels), format_func=lambda item: maintenance_labels[item])
                maintenance_action = st.selectbox("İşlem", ["Bakımı başlat", "Bakımı tamamla"])
                assigned_technician = st.selectbox("Teknisyen", technician_names or ["Teknik Servis"])
                next_maintenance_date = st.date_input("Sonraki bakım tarihi", date.today() + timedelta(days=30))
                return_to_service = st.checkbox("Tamamlanınca makineyi çalışır duruma al", value=False)
                if st.form_submit_button("Bakım durumunu güncelle"):
                    selected_record = active_maintenance[active_maintenance["id"] == maintenance_id].iloc[0]
                    if maintenance_action == "Bakımı başlat":
                        execute("UPDATE maintenance SET status=?, technician=? WHERE id=?", ("Bakımda", assigned_technician, int(maintenance_id)))
                        execute("UPDATE machines SET status='Arızalı' WHERE machine_code=?", (selected_record["machine_code"],))
                        audit_event("Bakımı başlattı", selected_record["machine_code"], assigned_technician)
                        st.success("Bakım başladı; makine bakımda olarak işaretlendi.")
                    else:
                        completed_at = str(date.today())
                        machine_status = "Çalışıyor" if return_to_service else "Beklemede"
                        execute("UPDATE maintenance SET status=?, technician=?, maintenance_date=?, next_date=? WHERE id=?", ("Tamamlandı", assigned_technician, completed_at, str(next_maintenance_date), int(maintenance_id)))
                        execute("UPDATE machines SET status=?, last_maintenance=?, next_maintenance=? WHERE machine_code=?", (machine_status, completed_at, str(next_maintenance_date), selected_record["machine_code"]))
                        audit_event("Bakımı tamamladı", selected_record["machine_code"], f"{assigned_technician} · Makine: {machine_status}")
                        st.success(f"Bakım tamamlandı; {selected_record['machine_code']} {machine_status.lower()} durumuna alındı.")
                    st.rerun()
        with st.form("maintenance_form"):
            machine = st.selectbox(
                "Makine",
                df["machine_code"].tolist(),
                key="maintenance_machine"
            )

            maintenance_type = st.selectbox(
                "Bakım Tipi",
                [
                    "Periyodik Bakım",
                    "Arıza Bakımı",
                    "Önleyici Bakım",
                    "Revizyon"
                ]
            )

            description = st.text_input(
                "Açıklama"
            )

            maintenance_date = st.date_input(
                "Bakım tarihi",
                date.today()
            )

            next_date = st.date_input(
                "Sonraki bakım",
                date.today()
            )

            technician = st.text_input(
                "Teknisyen",
                "Teknik Servis"
            )

            submitted = st.form_submit_button(
                "💾 Bakım Kaydet"
            )

            if submitted:
                machine_id = int(
                    q(
                        "SELECT id FROM machines WHERE machine_code=?",
                        (machine,)
                    ).iloc[0]["id"]
                )

                execute("""
                    INSERT INTO maintenance(
                        machine_id,machine_code,maintenance_type,
                        description,maintenance_date,next_date,
                        technician,status
                    )
                    VALUES(?,?,?,?,?,?,?,?)
                """, (
                    machine_id,
                    machine,
                    maintenance_type,
                    description,
                    str(maintenance_date),
                    str(next_date),
                    technician,
                    "Planlandı"
                ))

                st.success("Bakım kaydedildi.")
                st.rerun()
    else:
        st.info("Bu işlem için Bakım veya Admin rolü gereklidir.")


    # =========================================================
    # STOK
    # =========================================================


if selected_module == "📦 Stok":
    from stock_panel import render_stock_panel

    if st.session_state.pop("stock_record_created", False):
        st.success("Stok hareketi kaydedildi.")
    products = render_stock_panel(q)

    @st.dialog("Yeni Stok Hareketi", width="large")
    def stock_movement_dialog():
        if not is_admin():
            st.info("Stok hareketi eklemek için Admin yetkisi gerekir.")
            if st.button("Kapat", use_container_width=True, key="stock_close_noauth"):
                st.session_state["stock_new_open"] = False
                st.rerun()
            return
        if products.empty:
            st.warning("Stok hareketi girilebilecek ürün bulunamadı.")
            return
        default_type = st.session_state.get("stock_movement_default", "Giriş")
        with st.form("stock_dialog_form"):
            product_code = st.selectbox("Ürün", products["product_code"].tolist())
            movement_type = st.selectbox("Hareket", ["Giriş", "Çıkış"], index=0 if default_type == "Giriş" else 1)
            quantity = st.number_input("Miktar", min_value=1, max_value=100000, value=10)
            reason = st.text_input("Açıklama", placeholder="Stok hareketinin nedenini yazın")
            left, right = st.columns(2)
            submitted = left.form_submit_button("💾 Stok Kaydet", type="primary", use_container_width=True)
            cancelled = right.form_submit_button("Vazgeç", use_container_width=True)
        if cancelled:
            st.session_state["stock_new_open"] = False
            st.rerun()
        if submitted:
            row = products[products["product_code"] == product_code].iloc[0]
            product_id = int(row["id"])
            current_stock = float(row["stock"])
            if movement_type == "Çıkış" and quantity > current_stock:
                st.error(f"Yetersiz stok. Mevcut miktar: {current_stock:g}")
            else:
                operator = "+" if movement_type == "Giriş" else "-"
                execute(f"UPDATE products SET stock=stock{operator}? WHERE id=?", (quantity, product_id))
                execute("""
                    INSERT INTO stock(product_id,product_code,movement_type,quantity,reason,timestamp)
                    VALUES(?,?,?,?,?,?)
                """, (product_id, product_code, movement_type, quantity, reason, now()))
                audit_event("Stok hareketi", product_code, f"{movement_type}: {quantity} - {reason}")
                st.session_state["stock_new_open"] = False
                st.session_state["stock_record_created"] = True
                st.rerun()

    if st.session_state.get("stock_new_open", False):
        stock_movement_dialog()


if selected_module == "🧠 Akıllı Analiz":
    from smart_analysis_panel import render_smart_analysis
    render_smart_analysis(
        q,
        execute,
        current_user=st.session_state.get("full_name") or st.session_state.get("username", ""),
        can_manage=has_role("admin", "maintenance", "quality"),
        notifier=create_notification,
    )


    # =========================================================
    # DETAY
    # =========================================================


if selected_module == "🔎 Detay":
    st.subheader("🔎 Makine Detay Merkezi")
    detail_machine_v2 = st.selectbox("Makine", df["machine_code"].tolist(), key="detail_machine_v2")
    detail_row_v2 = df[df["machine_code"] == detail_machine_v2].iloc[0]
    detail_order_v2 = q("SELECT order_no,product,target,produced,priority,status,due_date FROM work_orders WHERE machine_code=? AND status != 'Tamamlandı' ORDER BY id DESC LIMIT 1", (detail_machine_v2,))
    detail_alarm_v2 = q("SELECT alarm,level,time FROM alarms WHERE machine_code=? AND acknowledged=0 ORDER BY id DESC LIMIT 1", (detail_machine_v2,))
    detail_tabs = st.tabs(["Genel", "Üretim", "OEE", "Duruş", "Sensör", "Kalite", "Bakım", "Enerji"])

    with detail_tabs[0]:
        general_left, general_right = st.columns([1, 1.2])
        with general_left:
            av_v2, perf_v2, qual_v2, oee_v2 = oee(detail_row_v2)
            overview_kpis = st.columns(4)
            overview_kpis[0].metric("OEE", f"%{oee_v2 * 100:.1f}")
            overview_kpis[1].metric("Üretim", f"{int(detail_row_v2['production']):,}/{int(detail_row_v2['target']):,}")
            overview_kpis[2].metric("Duruş", f"{float(detail_row_v2['downtime']):.1f} dk")
            overview_kpis[3].metric("Aktif Alarm", 0 if detail_alarm_v2.empty else 1)
            st.markdown(f"""<div class='trex-section'><div class='trex-section-title'>{detail_machine_v2} · {status_badge(detail_row_v2['status'])}</div>
            <div class='trex-small'>Ürün: <b>{detail_row_v2['product']}</b><br>Operatör: <b>{detail_row_v2['operator']}</b><br>Vardiya: <b>{detail_row_v2['shift']}</b><br>Son bakım: <b>{detail_row_v2['last_maintenance']}</b><br>Sonraki bakım: <b>{detail_row_v2['next_maintenance']}</b></div></div>""", unsafe_allow_html=True)
        with general_right:
            if detail_order_v2.empty:
                st.info("Bu makine için aktif iş emri yok.")
            else:
                order_v2 = detail_order_v2.iloc[0]
                order_progress_v2 = int(order_v2["produced"] / max(order_v2["target"], 1) * 100)
                st.markdown(f"""<div class='trex-section'><div class='trex-section-title'>📋 Aktif İş Emri · {order_v2['order_no']}</div>
                <div class='trex-small'>Ürün: <b>{order_v2['product']}</b> · Öncelik: <b>{order_v2['priority']}</b><br>Üretim: <b>{int(order_v2['produced']):,} / {int(order_v2['target']):,}</b> · Termin: <b>{order_v2['due_date']}</b></div><div class='home-v2-track'><i style='width:{min(order_progress_v2,100)}%'></i></div></div>""", unsafe_allow_html=True)
            if not detail_alarm_v2.empty:
                alarm_v2 = detail_alarm_v2.iloc[0]
                st.warning(f"{alarm_v2['level']} alarm · {alarm_v2['alarm']} · {alarm_v2['time']}")

    detail_production_history = q("SELECT quantity,timestamp,shift FROM production_history WHERE machine_code=? ORDER BY timestamp DESC LIMIT 500", (detail_machine_v2,))
    with detail_tabs[1]:
        if detail_production_history.empty:
            st.info("Üretim trendi için simülasyon veya API kaydı bekleniyor.")
        else:
            detail_production_history["timestamp"] = pd.to_datetime(detail_production_history["timestamp"], errors="coerce")
            production_history_v2 = detail_production_history.dropna(subset=["timestamp"]).sort_values("timestamp")
            production_history_v2["Üretim Artışı"] = production_history_v2["quantity"].diff().fillna(0).clip(lower=0)
            speed_v2 = production_history_v2["Üretim Artışı"].tail(10).mean() * 60 if len(production_history_v2) > 1 else 0
            production_metrics_v2 = st.columns(4)
            production_metrics_v2[0].metric("Günlük üretim", int(production_history_v2["Üretim Artışı"].sum()))
            production_metrics_v2[1].metric("Hedef", int(detail_row_v2["target"]))
            production_metrics_v2[2].metric("Gerçekleşme", f"%{int(detail_row_v2['production']) / max(int(detail_row_v2['target']), 1) * 100:.1f}")
            production_metrics_v2[3].metric("Üretim hızı", f"{speed_v2:.1f} adet/saat")
            production_chart_v2 = px.bar(production_history_v2, x="timestamp", y="Üretim Artışı", color="shift", title=f"{detail_machine_v2} · Üretim Geçmişi")
            production_chart_v2.update_layout(height=330, xaxis_title=None, yaxis_title="Adet")
            st.plotly_chart(production_chart_v2, use_container_width=True, config={"displayModeBar": False}, key=f"detail_production_{detail_machine_v2}")

    with detail_tabs[2]:
        oee_components_v2 = pd.DataFrame({"Bileşen":["Kullanılabilirlik", "Performans", "Kalite", "OEE"], "Yüzde":[av_v2*100, perf_v2*100, qual_v2*100, oee_v2*100]})
        component_chart_v2 = px.bar(oee_components_v2, x="Bileşen", y="Yüzde", text="Yüzde", color="Bileşen", title="OEE Bileşenleri", color_discrete_sequence=["#2bb673", "#0d9760", "#56cb89", "#0a6e47"])
        component_chart_v2.update_traces(texttemplate="%{text:.1f}%", textposition="outside")
        component_chart_v2.update_layout(height=300, yaxis_range=[0, 100], showlegend=False)
        st.plotly_chart(component_chart_v2, use_container_width=True, config={"displayModeBar": False}, key=f"detail_oee_{detail_machine_v2}")
        st.caption("OEE; mevcut planlanan süre, duruş, ideal çevrim ve kalite kayıtları ile hesaplanır.")

    detail_downtime_v2 = q("SELECT reason,duration,start_time,end_time,notes,event_at FROM downtime WHERE machine_code=? ORDER BY id DESC", (detail_machine_v2,))
    with detail_tabs[3]:
        if detail_downtime_v2.empty:
            st.info("Bu makine için duruş kaydı yok.")
        else:
            downtime_summary_v2 = detail_downtime_v2.groupby("reason", as_index=False)["duration"].sum().sort_values("duration", ascending=False)
            detail_downtime_kpis = st.columns(4)
            detail_downtime_kpis[0].metric("Toplam duruş", f"{detail_downtime_v2['duration'].sum():.1f} dk")
            detail_downtime_kpis[1].metric("Duruş sayısı", len(detail_downtime_v2))
            detail_downtime_kpis[2].metric("En uzun duruş", f"{detail_downtime_v2['duration'].max():.1f} dk")
            detail_downtime_kpis[3].metric("En sık neden", str(detail_downtime_v2['reason'].mode().iloc[0]))
            downtime_chart_v2 = px.bar(downtime_summary_v2, x="reason", y="duration", title="Duruş Pareto Analizi", text="duration")
            downtime_chart_v2.update_layout(height=280, xaxis_title="Neden", yaxis_title="Dakika")
            st.plotly_chart(downtime_chart_v2, use_container_width=True, config={"displayModeBar": False}, key=f"detail_downtime_{detail_machine_v2}")
            st.dataframe(detail_downtime_v2.rename(columns={"reason":"Neden", "duration":"Süre (dk)", "start_time":"Başlangıç", "end_time":"Bitiş", "notes":"Açıklama"}), use_container_width=True, hide_index=True, height=190)

    detail_sensor_v2 = q("SELECT timestamp,temperature,vibration,pressure,rpm FROM sensor_history WHERE machine_code=? ORDER BY timestamp DESC LIMIT 200", (detail_machine_v2,))
    with detail_tabs[4]:
        current_sensor_v2 = q("SELECT temperature,vibration,pressure,rpm,timestamp FROM sensors WHERE machine_code=?", (detail_machine_v2,))
        if not current_sensor_v2.empty:
            sensor_v2 = current_sensor_v2.iloc[0]
            sensor_kpis_v2 = st.columns(4)
            sensor_kpis_v2[0].metric("Sıcaklık", f"{float(sensor_v2['temperature']):.1f} °C")
            sensor_kpis_v2[1].metric("Titreşim", f"{float(sensor_v2['vibration']):.2f} mm/s")
            sensor_kpis_v2[2].metric("Basınç", f"{float(sensor_v2['pressure']):.1f} bar")
            sensor_kpis_v2[3].metric("RPM", int(sensor_v2["rpm"]))
        if detail_sensor_v2.empty:
            st.info("Sensör trendi için simülasyon veya API kaydı bekleniyor.")
        else:
            detail_sensor_v2["timestamp"] = pd.to_datetime(detail_sensor_v2["timestamp"], errors="coerce")
            detail_sensor_v2 = detail_sensor_v2.dropna(subset=["timestamp"]).sort_values("timestamp")
            sensor_chart_v2 = px.line(detail_sensor_v2, x="timestamp", y=["temperature", "vibration", "pressure", "rpm"], title="Sensör Geçmişi")
            sensor_chart_v2.update_layout(height=330, legend_title_text="")
            st.plotly_chart(sensor_chart_v2, use_container_width=True, config={"displayModeBar": False}, key=f"detail_sensor_{detail_machine_v2}")

    with detail_tabs[5]:
        detail_quality_v2 = q("SELECT product,produced,defective,defect_reason,timestamp FROM quality WHERE machine_code=? ORDER BY id DESC LIMIT 100", (detail_machine_v2,))
        if detail_quality_v2.empty:
            st.info("Kalite kaydı bulunmuyor.")
        else:
            quality_produced_v2 = int(detail_quality_v2["produced"].sum())
            quality_defective_v2 = int(detail_quality_v2["defective"].sum())
            quality_kpis_v2 = st.columns(4)
            quality_kpis_v2[0].metric("Toplam üretim", quality_produced_v2)
            quality_kpis_v2[1].metric("Sağlam üretim", max(quality_produced_v2-quality_defective_v2, 0))
            quality_kpis_v2[2].metric("Hatalı üretim", quality_defective_v2)
            quality_kpis_v2[3].metric("Kalite", f"%{max(quality_produced_v2-quality_defective_v2, 0) / max(quality_produced_v2, 1) * 100:.1f}")
            defect_chart_v2 = px.bar(detail_quality_v2.groupby("defect_reason", as_index=False)["defective"].sum(), x="defect_reason", y="defective", title="Hata Türleri")
            defect_chart_v2.update_layout(height=280, xaxis_title="Hata türü", yaxis_title="Hatalı adet")
            st.plotly_chart(defect_chart_v2, use_container_width=True, config={"displayModeBar": False}, key=f"detail_quality_{detail_machine_v2}")

    with detail_tabs[6]:
        detail_maintenance_v2 = q("SELECT maintenance_type,description,maintenance_date,next_date,technician,status FROM maintenance WHERE machine_code=? ORDER BY next_date DESC", (detail_machine_v2,))
        detail_requests_v2 = q("SELECT request_type,priority,requested_by,requested_at,assigned_to,status FROM maintenance_requests WHERE machine_code=? ORDER BY id DESC LIMIT 20", (detail_machine_v2,))
        maintenance_left_v2, maintenance_right_v2 = st.columns(2)
        with maintenance_left_v2:
            st.markdown("##### Bakım geçmişi")
            st.dataframe(detail_maintenance_v2.rename(columns={"maintenance_type":"Bakım Tipi", "description":"Açıklama", "maintenance_date":"Bakım Tarihi", "next_date":"Sonraki Tarih", "technician":"Teknisyen", "status":"Durum"}), use_container_width=True, hide_index=True, height=260)
        with maintenance_right_v2:
            st.markdown("##### Bakım talepleri")
            st.dataframe(detail_requests_v2.rename(columns={"request_type":"Talep", "priority":"Öncelik", "requested_by":"Talep Eden", "requested_at":"Tarih", "assigned_to":"Sorumlu", "status":"Durum"}), use_container_width=True, hide_index=True, height=260)

    with detail_tabs[7]:
        detail_energy_v2 = q("SELECT power_kw,energy_kwh,timestamp FROM energy_readings WHERE machine_code=? ORDER BY timestamp DESC LIMIT 200", (detail_machine_v2,))
        if detail_energy_v2.empty:
            st.info("Enerji kaydı için simülasyon veya API verisi bekleniyor.")
        else:
            detail_energy_v2["timestamp"] = pd.to_datetime(detail_energy_v2["timestamp"], errors="coerce")
            detail_energy_v2 = detail_energy_v2.dropna(subset=["timestamp"]).sort_values("timestamp")
            energy_kpis_v2 = st.columns(3)
            energy_kpis_v2[0].metric("Anlık güç", f"{float(detail_energy_v2['power_kw'].iloc[-1]):.2f} kW")
            energy_kpis_v2[1].metric("Toplam tüketim", f"{float(detail_energy_v2['energy_kwh'].sum()):.2f} kWh")
            energy_kpis_v2[2].metric("Ürün başına enerji", f"{float(detail_energy_v2['energy_kwh'].sum()) / max(int(detail_row_v2['production']), 1):.3f} kWh/adet")
            energy_chart_v2 = px.line(detail_energy_v2, x="timestamp", y=["power_kw", "energy_kwh"], title="Enerji Geçmişi")
            energy_chart_v2.update_layout(height=320, legend_title_text="")
            st.plotly_chart(energy_chart_v2, use_container_width=True, config={"displayModeBar": False}, key=f"detail_energy_{detail_machine_v2}")
    st.stop()

if selected_module == "🔎 Detay":
    st.subheader("🔎 Makine Detayı")

    machine = st.selectbox(
        "Makine seç",
        df["machine_code"].tolist(),
        key="detail_machine"
    )

    row = df[
        df["machine_code"] == machine
    ].iloc[0]

    detail_order = q("""
        SELECT order_no, product, target, produced, status, due_date
        FROM work_orders WHERE machine_code=? AND status != 'Tamamlandı'
        ORDER BY id DESC LIMIT 1
    """, (machine,))
    detail_maintenance = q("""
        SELECT maintenance_type, next_date, status FROM maintenance
        WHERE machine_code=? ORDER BY next_date LIMIT 1
    """, (machine,))
    detail_top = st.columns(2)
    with detail_top[0]:
        if detail_order.empty:
            st.info("Aktif iş emri bulunmuyor.")
        else:
            order = detail_order.iloc[0]
            st.info(f"Aktif iş emri: **{order['order_no']}** · {order['product']} · {int(order['produced']):,}/{int(order['target']):,} adet")
    with detail_top[1]:
        if detail_maintenance.empty:
            st.info("Planlı bakım bulunmuyor.")
        else:
            maintenance_item = detail_maintenance.iloc[0]
            st.info(f"Sonraki bakım: **{maintenance_item['next_date']}** · {maintenance_item['maintenance_type']}")

    av, pe, qu, oe = oee(row)

    a, b, c, d = st.columns(4)

    a.metric(
        "OEE",
        f"{oe * 100:.1f}%"
    )

    b.metric(
        "Kullanılabilirlik",
        f"{av * 100:.1f}%"
    )

    c.metric(
        "Performans",
        f"{pe * 100:.1f}%"
    )

    d.metric(
        "Kalite",
        f"{qu * 100:.1f}%"
    )

    info_col, sensor_col = st.columns([1, 1.7])
    with info_col:
        st.markdown(f"**Ürün:** {row['product']}")
        st.markdown(f"**Operatör:** {row['operator']}")
        st.markdown(f"**Durum:** {status_badge(row['status'])}")
        st.markdown(f"**Üretim:** {row['production']}/{row['target']}")
        st.markdown(f"**Duruş:** {row['downtime']:.1f} dk")
        st.markdown(f"**Hatalı:** {row['defective']}")
    with sensor_col:
        sensor_history_detail = q("""
            SELECT timestamp, temperature, vibration, pressure, rpm
            FROM sensor_history
            WHERE machine_code=?
            ORDER BY timestamp DESC
            LIMIT 40
        """, (machine,))
        if not sensor_history_detail.empty:
            sensor_history_detail["timestamp"] = pd.to_datetime(sensor_history_detail["timestamp"])
            sensor_history_detail = sensor_history_detail.sort_values("timestamp")
            sensor_chart = px.line(
                sensor_history_detail, x="timestamp", y=["temperature", "vibration"],
                markers=True, title="Sensör Trendi"
            )
            sensor_chart.update_layout(height=260, legend_title_text="")
            st.plotly_chart(sensor_chart, use_container_width=True)
        else:
            st.info("Sensör trendi için simülasyon çalıştırın.")

    downtime_detail = q("""
        SELECT reason, SUM(duration) AS duration
        FROM downtime WHERE machine_code=?
        GROUP BY reason ORDER BY duration DESC
    """, (machine,))
    if not downtime_detail.empty:
        downtime_chart = px.bar(downtime_detail, x="reason", y="duration", title="Duruş Nedenleri")
        downtime_chart.update_layout(height=240)
        st.plotly_chart(downtime_chart, use_container_width=True)


    # =========================================================
    # VERİTABANI
    # =========================================================


if selected_module == "⚡ Enerji Takibi":
    st.subheader("⚡ Enerji Takibi")
    st.caption("Enerji değerleri yerel simülasyonda makine durumuna göre üretilir.")
    energy = q("""
        SELECT e.machine_code, e.power_kw, e.energy_kwh, e.timestamp, m.production
        FROM energy_readings e JOIN machines m ON m.id=e.machine_id
        WHERE e.id IN (SELECT MAX(id) FROM energy_readings GROUP BY machine_id)
        ORDER BY e.machine_code
    """)
    if energy.empty:
        st.info("Enerji verisi için bir kez Yerel Simülasyon çalıştırın.")
    else:
        total_power = energy["power_kw"].sum()
        total_energy = q("SELECT COALESCE(SUM(energy_kwh), 0) AS total FROM energy_readings").iloc[0]["total"]
        energy_per_unit = total_energy / max(int(df["production"].sum()), 1)
        ec1, ec2, ec3 = st.columns(3)
        ec1.metric("Anlık güç", f"{total_power:.1f} kW")
        ec2.metric("Toplam tüketim", f"{total_energy:.1f} kWh")
        ec3.metric("Ürün başına enerji", f"{energy_per_unit:.3f} kWh/adet")
        energy_view = energy.rename(columns={"machine_code":"Makine", "power_kw":"Anlık Güç (kW)", "energy_kwh":"Son Ölçüm (kWh)", "timestamp":"Zaman", "production":"Üretim"})
        st.dataframe(energy_view, use_container_width=True, hide_index=True)
        energy_chart = px.bar(energy, x="machine_code", y="power_kw", color="machine_code", title="Makine bazında anlık güç")
        st.plotly_chart(energy_chart, use_container_width=True)


if selected_module == "📺 Andon Ekranı":
    st.markdown("## 📺 ANDON · Canlı Fabrika Panosu")
    st.caption("TV ekranı için sade, uzaktan okunabilir canlı operasyon görünümü.")
    andon_options = ["Toplam hedef", "Aktif makine", "Açık alarm", "Ortalama OEE", "Toplam üretim", "Aktif vardiya", "Açık iş emri", "Güncelleme saati"]
    selected_andon_options = st.multiselect(
        "Andon özetinde gösterilecek bilgiler",
        andon_options,
        default=["Toplam hedef", "Aktif makine", "Açık alarm", "Güncelleme saati"],
        key="andon_summary_options",
        help="Seçtikleriniz büyük ekranın üst özet alanına anında yansır.",
    )
    live = df[["machine_code", "status", "production", "target", "oee"]].copy()
    open_alarm_count = int(q("SELECT COUNT(*) AS n FROM alarms WHERE acknowledged=0").iloc[0]["n"])
    total_progress = int(df["production"].sum() / max(int(df["target"].sum()), 1) * 100)
    active_machine_count = int((live["status"] == "Çalışıyor").sum())
    average_oee = float(live["oee"].mean() * 100) if not live.empty else 0.0
    open_work_order_count = int(q("SELECT COUNT(*) AS n FROM work_orders WHERE status != 'Tamamlandı'").iloc[0]["n"])
    andon_values = {
        "Toplam hedef": ("TOPLAM HEDEF", f"%{total_progress}"),
        "Aktif makine": ("AKTİF MAKİNE", str(active_machine_count)),
        "Açık alarm": ("AÇIK ALARM", str(open_alarm_count)),
        "Ortalama OEE": ("ORTALAMA OEE", f"%{average_oee:.1f}"),
        "Toplam üretim": ("TOPLAM ÜRETİM", f"{int(live['production'].sum()):,}"),
        "Aktif vardiya": ("AKTİF VARDİYA", active_shift_name()),
        "Açık iş emri": ("AÇIK İŞ EMRİ", str(open_work_order_count)),
        "Güncelleme saati": ("GÜNCELLEME", f"{datetime.now():%H:%M}"),
    }
    andon_summary = "".join(
        f"<div><small>{andon_values[item][0]}</small><b>{andon_values[item][1]}</b></div>"
        for item in selected_andon_options
    ) or "<div><small>ANDON ÖZETİ</small><b>Seçim yapın</b></div>"
    andon_cards = ""
    for _, item in live.iterrows():
        status_class = "fault" if item["status"] == "Arızalı" else ("wait" if item["status"] == "Beklemede" else "")
        status_icon = "🔴" if item["status"] == "Arızalı" else ("🟡" if item["status"] == "Beklemede" else "🟢")
        progress = min(int(item["production"] / max(int(item["target"]), 1) * 100), 100)
        andon_cards += f"""<div class="andon-machine {status_class}">
          <span class="andon-status">{status_icon} {item['status']}</span><div class="andon-name">{item['machine_code']}</div>
          <div class="andon-detail">Üretim: <b>{int(item['production']):,} / {int(item['target']):,}</b> adet &nbsp; · &nbsp; OEE: <b>%{item['oee'] * 100:.1f}</b></div>
          <div class="andon-progress"><span style="width:{progress}%"></span></div></div>"""
    st.markdown(f"""
    <style>
    .andon-board{{background:#081d12;border-radius:22px;padding:26px;color:#f5fff7;box-shadow:0 18px 45px rgba(2,24,12,.25)}}
    .andon-summary{{display:flex;gap:14px;justify-content:space-between;margin-bottom:22px}}
    .andon-summary div{{flex:1;background:#102d1c;border:1px solid #2f6841;border-radius:14px;padding:14px;text-align:center}}
    .andon-summary small{{display:block;color:#b7d8c0;font-weight:700;letter-spacing:.08em}} .andon-summary b{{font-size:2rem}}
    .andon-machine{{border-radius:16px;padding:19px;margin:10px 0;background:#102d1c;border-left:8px solid #57c777}}
    .andon-machine.wait{{border-left-color:#f4c34d}} .andon-machine.fault{{border-left-color:#f05a5a}}
    .andon-name{{font-size:1.55rem;font-weight:850}} .andon-status{{float:right;font-size:1.05rem;font-weight:800}}
    .andon-progress{{height:12px;background:#234633;border-radius:999px;overflow:hidden;margin-top:12px}} .andon-progress span{{display:block;height:100%;background:#50ca76}}
    .andon-detail{{color:#c5dfcc;margin-top:9px;font-size:1rem}}
    </style>
    <div class="andon-board"><div class="andon-summary">{andon_summary}</div>{andon_cards}</div>""", unsafe_allow_html=True)
    critical = q("SELECT machine_code, alarm FROM alarms WHERE acknowledged=0 ORDER BY id DESC LIMIT 1")
    if not critical.empty:
        st.error(f"⚠️ ALARM · {critical.iloc[0]['machine_code']}: {critical.iloc[0]['alarm']}")


if selected_module == "🔳 QR Makine":
    st.subheader("🔳 QR Kodlu Makine Ekranı")
    st.caption("QR kodu okutan cihaz, girişten sonra seçilen makinenin detay ekranına yönlenir.")
    qr_machine = st.selectbox("Makine", df["machine_code"].tolist(), key="qr_machine")
    qr_url = machine_qr_url(qr_machine)
    qr_image = generate_qr_image(qr_url)
    q1, q2 = st.columns([1, 2])
    with q1:
        if qr_image:
            st.image(qr_image, caption=f"{qr_machine} QR Kodu", width=260)
        else:
            st.warning("QR üretim paketi yüklü değil. Uygulama bağımlılıklarını kurduktan sonra QR kodu yerel olarak oluşturulur.")
    with q2:
        machine_row = df[df["machine_code"] == qr_machine].iloc[0]
        st.markdown(f"### {qr_machine}")
        st.write(f"**Durum:** {machine_row['status']}")
        st.write(f"**Ürün:** {machine_row['product']}")
        st.write(f"**Üretim:** {int(machine_row['production']):,} / {int(machine_row['target']):,} adet")
        st.code(qr_url, language=None)


if selected_module == "🧰 Bakım Talebi":
    from maintenance_request_panel_v2 import render_request_panel
    requests_table = render_request_panel(q)
    if st.session_state.pop("maintenance_request_created", False):
        st.success("Bakım talebi oluşturuldu ve listeye eklendi.")

    @st.dialog("Yeni Bakım Talebi", width="large")
    def maintenance_request_dialog():
        if st.button("Kapat", key="maintenance_request_dialog_close"):
            st.session_state["mr_new"] = False
            st.rerun()
        if df.empty:
            st.warning("Bakım talebi açılabilecek makine bulunmuyor.")
            return
        with st.form("maintenance_request_form"):
            request_machine = st.selectbox("Makine", df["machine_code"].tolist(), key="request_machine")
            request_type = st.selectbox("Talep türü", ["Arıza", "Önleyici bakım", "Kontrol", "Operatör desteği"])
            request_priority = st.selectbox("Öncelik", ["Düşük", "Normal", "Yüksek", "Kritik"], index=1)
            request_description = st.text_area("Açıklama", placeholder="Gözlemlenen sorunu veya talebi yazın.")
            if st.form_submit_button("Bakım Talebi Oluştur", type="primary", use_container_width=True):
                execute("""INSERT INTO maintenance_requests(machine_code,request_type,description,priority,requested_by,requested_at,status)
                           VALUES(?,?,?,?,?,?,?)""", (request_machine, request_type, request_description, request_priority, st.session_state.get("full_name", "Kullanıcı"), now(), "Açık"))
                if request_type == "Arıza":
                    execute("UPDATE machines SET status='Arızalı' WHERE machine_code=?", (request_machine,))
                    alarm_level = "Kritik" if request_priority == "Kritik" else "Uyarı"
                    execute("INSERT INTO alarms(machine_id,machine_code,alarm,level,time) SELECT id,machine_code,?,?,? FROM machines WHERE machine_code=?", ("Bakım talebi ile bildirilen arıza", alarm_level, now(), request_machine))
                audit_event("Bakım talebi oluşturdu", request_machine, f"{request_type} · {request_priority}")
                st.session_state["mr_new"] = False
                st.session_state["maintenance_request_created"] = True
                st.rerun()

    if st.session_state.get("mr_new", False):
        maintenance_request_dialog()
    if has_role("admin", "maintenance") and not requests_table.empty:
        open_requests = requests_table[requests_table["status"] != "Tamamlandı"]
        if not open_requests.empty:
            with st.form("maintenance_request_action"):
                request_labels = {int(row["id"]): f"{row['machine_code']} · {row['request_type']} · {row['priority']}" for _, row in open_requests.iterrows()}
                request_ids = list(request_labels)
                focused_request = st.session_state.get("mr_focus")
                request_id = st.selectbox("Talep", request_ids, index=request_ids.index(focused_request) if focused_request in request_ids else 0, format_func=lambda item: request_labels[item])
                technician = st.selectbox("Teknisyen", q("SELECT full_name FROM users WHERE role IN ('admin','maintenance') AND is_active=1")["full_name"].tolist())
                request_action = st.selectbox("İşlem", ["Teknisyene ata", "Bakımı başlat", "Tamir tamamlandı"])
                next_service_date = st.date_input("Sonraki bakım tarihi", date.today() + timedelta(days=30), key="request_next_service_date")
                restart_machine = st.checkbox("Tamirden sonra makineyi çalıştır", value=False)
                if st.form_submit_button("💾 Ata / Güncelle"):
                    selected_request = open_requests[open_requests["id"] == request_id].iloc[0]
                    if request_action == "Teknisyene ata":
                        status = "Atandı"
                        execute("UPDATE maintenance_requests SET assigned_to=?,status=?,completed_at=? WHERE id=?", (technician, status, None, int(request_id)))
                    elif request_action == "Bakımı başlat":
                        status = "Bakımda"
                        execute("UPDATE maintenance_requests SET assigned_to=?,status=?,completed_at=? WHERE id=?", (technician, status, None, int(request_id)))
                        execute("UPDATE machines SET status='Arızalı' WHERE machine_code=?", (selected_request["machine_code"],))
                    else:
                        status = "Tamamlandı"
                        completed_date = str(date.today())
                        execute("UPDATE maintenance_requests SET assigned_to=?,status=?,completed_at=? WHERE id=?", (technician, status, now(), int(request_id)))
                        execute("""INSERT INTO maintenance(machine_id,machine_code,maintenance_type,description,maintenance_date,next_date,technician,status)
                                   SELECT id,machine_code,?,?,?, ?,?, 'Tamamlandı' FROM machines WHERE machine_code=?""", ("Arıza Bakımı" if selected_request["request_type"] == "Arıza" else "Önleyici Bakım", selected_request["description"] or selected_request["request_type"], completed_date, str(next_service_date), technician, selected_request["machine_code"]))
                        if selected_request["request_type"] in ("Arıza", "Önleyici bakım"):
                            machine_status = "Çalışıyor" if restart_machine else "Beklemede"
                            execute("UPDATE machines SET status=?,last_maintenance=?,next_maintenance=? WHERE machine_code=?", (machine_status, completed_date, str(next_service_date), selected_request["machine_code"]))
                    audit_event("Bakım talebi güncelledi", f"Talep #{request_id}", f"{technician} · {status}")
                    st.success(f"Talep durumu güncellendi: {status}")
                    st.rerun()


if selected_module == "👥 Kullanıcı Yönetimi":
    st.subheader("👥 Kullanıcı Yönetimi")
    st.caption("Yeni hesap oluşturun, rol/hesap durumunu yönetin ve kullanıcı bazlı modül yetkileri atayın.")
    if not is_admin():
        st.error("Bu ekran yalnızca Admin rolü içindir.")
    else:
        role_options = ["admin", "operator", "maintenance", "quality"]
        role_names = {"admin":"ADMIN", "operator":"OPERATÖR", "maintenance":"BAKIM", "quality":"KALİTE"}
        permission_modules = [
            "🏠 Ana Sayfa", "🏭 Makine", "⚡ Enerji Takibi", "📈 Üretim", "🧮 Manuel OEE", "👥 OLE", "🌐 Dijital Olgunluk",
            "🚨 Alarmlar", "📋 İş Emirleri", "📡 Sensörler", "⏱️ Duruşlar", "👷 Vardiya",
            "🧰 Bakım Talebi", "✅ Kalite", "🔧 Bakım", "📦 Stok", "🔎 Detay",
            "📺 Andon Ekranı", "🔳 QR Makine"
        ]

        users_table = q('SELECT username AS "Kullanıcı", full_name AS "Ad Soyad", role AS "Rol", is_active AS "Aktif", created_at AS "Oluşturulma" FROM users ORDER BY username')
        display_users = users_table.copy()
        if not display_users.empty:
            display_users["Rol"] = display_users["Rol"].map(role_names).fillna(display_users["Rol"])
            display_users["Aktif"] = display_users["Aktif"].map({1:"✅ Aktif", 0:"⛔ Pasif"})
        st.dataframe(display_users, use_container_width=True, hide_index=True)

        tab_create, tab_account, tab_permissions, tab_password = st.tabs([
            "➕ Yeni Kullanıcı", "👤 Rol / Durum", "🔐 Modül Yetkileri", "🔑 Şifre Değiştir"
        ])

        with tab_create:
            st.markdown("#### Yeni hesap oluştur")
            with st.form("create_user_form", clear_on_submit=True):
                new_full_name = st.text_input("Ad Soyad", placeholder="Örn. Ahmet Yılmaz")
                new_username = st.text_input("Kullanıcı Adı", placeholder="Örn. ahmet.yilmaz")
                new_password = st.text_input("Şifre", type="password", autocomplete="new-password")
                new_password_again = st.text_input("Şifre Tekrar", type="password", autocomplete="new-password")
                new_user_role = st.selectbox("Rol", role_options, format_func=lambda x: role_names[x], index=1)
                new_active = st.checkbox("Hesap aktif", value=True)
                create_submit = st.form_submit_button("➕ Hesap Oluştur", use_container_width=True)
            if create_submit:
                clean_username = new_username.strip()
                clean_full_name = new_full_name.strip()
                if not clean_full_name or not clean_username or not new_password:
                    st.error("Ad soyad, kullanıcı adı ve şifre zorunludur.")
                elif " " in clean_username:
                    st.error("Kullanıcı adında boşluk kullanmayın.")
                elif len(new_password) < 8:
                    st.error("Şifre en az 8 karakter olmalıdır.")
                elif new_password != new_password_again:
                    st.error("Şifreler eşleşmiyor.")
                elif not q("SELECT 1 FROM users WHERE username=?", (clean_username,)).empty:
                    st.error("Bu kullanıcı adı zaten kullanılıyor.")
                else:
                    execute("INSERT INTO users(username,password_hash,role,full_name,is_active,created_at) VALUES(?,?,?,?,?,?)",
                            (clean_username, password_hash(new_password), new_user_role, clean_full_name, int(new_active), now()))
                    audit_event("Yeni kullanıcı oluşturdu", clean_username, f"Rol: {new_user_role} · Aktif: {new_active}")
                    st.success(f"{clean_username} hesabı oluşturuldu.")
                    st.rerun()

        user_records = q("SELECT username,full_name,role,is_active FROM users ORDER BY username")
        usernames = user_records["username"].tolist() if not user_records.empty else []

        with tab_account:
            st.markdown("#### Kullanıcı rolü ve hesap durumu")
            if usernames:
                target_username = st.selectbox("Kullanıcı", usernames, key="account_user")
                selected_user = user_records[user_records["username"] == target_username].iloc[0]
                with st.form("user_role_form"):
                    new_role = st.selectbox("Rol", role_options, index=role_options.index(selected_user["role"]), format_func=lambda x: role_names[x])
                    active_state = st.checkbox("Kullanıcı aktif", value=bool(selected_user["is_active"]))
                    role_submit = st.form_submit_button("💾 Rol / Durum Kaydet", use_container_width=True)
                if role_submit:
                    removing_admin = selected_user["role"] == "admin" and (new_role != "admin" or not active_state)
                    active_admins = int(q("SELECT COUNT(*) AS n FROM users WHERE role='admin' AND is_active=1").iloc[0]["n"])
                    if target_username == st.session_state.get("username") and not active_state:
                        st.error("Giriş yaptığınız kendi hesabınızı pasif yapamazsınız.")
                    elif removing_admin and active_admins <= 1:
                        st.error("Sistemde en az bir aktif Admin hesabı kalmalıdır.")
                    else:
                        execute("UPDATE users SET role=?,is_active=? WHERE username=?", (new_role, int(active_state), target_username))
                        if new_role == "admin":
                            execute("DELETE FROM user_permissions WHERE username=?", (target_username,))
                        audit_event("Kullanıcı rolü/durumu güncelledi", target_username, f"Rol: {new_role} · Aktif: {active_state}")
                        st.success("Rol ve hesap durumu güncellendi.")
                        st.rerun()
            else:
                st.info("Henüz kullanıcı bulunmuyor.")

        with tab_permissions:
            st.markdown("#### Kullanıcı bazlı modül yetkileri")
            st.caption("Özel yetki kaydedilmezse kullanıcının rol varsayılanları uygulanır.")
            non_admin_users = user_records[user_records["role"] != "admin"]
            permission_usernames = non_admin_users["username"].tolist()
            if permission_usernames:
                permission_username = st.selectbox("Yetki verilecek kullanıcı", permission_usernames, key="permission_user")
                permission_row = non_admin_users[non_admin_users["username"] == permission_username].iloc[0]
                permission_role = permission_row["role"]
                saved_permission_df = q("SELECT module FROM user_permissions WHERE username=?", (permission_username,))
                saved_modules = saved_permission_df["module"].tolist() if not saved_permission_df.empty else []
                has_custom_permissions = "__CUSTOM__" in saved_modules
                if has_custom_permissions:
                    default_selected = [m for m in saved_modules if m != "__CUSTOM__"]
                    st.info("Bu kullanıcı için özel yetki seti aktif.")
                else:
                    default_selected = [m for m in permission_modules if m in ROLE_MODULES.get(permission_role, set())]
                    st.info(f"Şu anda {role_names.get(permission_role, permission_role)} rolünün varsayılan yetkileri kullanılıyor.")
                selected_permissions = st.multiselect("Erişebileceği modüller", permission_modules, default=default_selected,
                                                     format_func=lambda x: NAV_LABELS.get(x, x), key=f"module_permissions_{permission_username}")
                save_col, reset_col = st.columns(2)
                with save_col:
                    if st.button("💾 Özel Yetkileri Kaydet", use_container_width=True, key="save_permissions"):
                        execute("DELETE FROM user_permissions WHERE username=?", (permission_username,))
                        execute("INSERT INTO user_permissions(username,module) VALUES(?,?)", (permission_username, "__CUSTOM__"))
                        for module in selected_permissions:
                            execute("INSERT INTO user_permissions(username,module) VALUES(?,?)", (permission_username, module))
                        audit_event("Kullanıcı modül yetkilerini güncelledi", permission_username,
                                    ", ".join(NAV_LABELS.get(m, m) for m in selected_permissions) or "Modül erişimi yok")
                        st.success("Özel modül yetkileri kaydedildi.")
                        st.rerun()
                with reset_col:
                    if st.button("↩️ Rol Varsayılanına Dön", use_container_width=True, key="reset_permissions"):
                        execute("DELETE FROM user_permissions WHERE username=?", (permission_username,))
                        audit_event("Kullanıcı yetkilerini rol varsayılanına döndürdü", permission_username, permission_role)
                        st.success("Özel yetkiler kaldırıldı; rol varsayılanları yeniden aktif.")
                        st.rerun()
            else:
                st.info("Yetki atanabilecek Admin dışı kullanıcı bulunmuyor.")

        with tab_password:
            st.markdown("#### Kullanıcı şifresini değiştir")
            if usernames:
                with st.form("admin_password_reset_form", clear_on_submit=True):
                    password_username = st.selectbox("Kullanıcı", usernames, key="password_user")
                    reset_password = st.text_input("Yeni Şifre", type="password", autocomplete="new-password")
                    reset_password_again = st.text_input("Yeni Şifre Tekrar", type="password", autocomplete="new-password")
                    password_submit = st.form_submit_button("🔑 Şifreyi Değiştir", use_container_width=True)
                if password_submit:
                    if len(reset_password) < 8:
                        st.error("Yeni şifre en az 8 karakter olmalıdır.")
                    elif reset_password != reset_password_again:
                        st.error("Şifreler eşleşmiyor.")
                    else:
                        execute("UPDATE users SET password_hash=? WHERE username=?", (password_hash(reset_password), password_username))
                        audit_event("Kullanıcı şifresini değiştirdi", password_username, "Admin tarafından sıfırlandı")
                        st.success(f"{password_username} kullanıcısının şifresi değiştirildi.")


if selected_module == "📜 Denetim Kaydı":
    st.subheader("📜 Denetim Kaydı ve Yedekleme")
    st.caption("Kullanıcı işlemlerini izleyin ve SQLite veritabanının yedeğini indirin.")
    audit_date_col, audit_all_col = st.columns([1, 2])
    with audit_date_col:
        selected_audit_range = auto_date_range_filter("İşlem tarih aralığı", "audit_day_filter")
    with audit_all_col:
        show_all_audit = st.checkbox("Tüm tarihlerin denetim kayıtlarını göster", value=False, key="show_all_audit")
    backup_col, log_col = st.columns([1, 2])
    with backup_col:
        if st.button("💾 Yedek Hazırla", use_container_width=True):
            with open(DB, "rb") as database_file:
                st.session_state["mes_backup_data"] = database_file.read()
            audit_event("Veritabanı yedeği hazırladı", "mes.db")
            st.success("Yedek indirilmeye hazır.")
        if st.session_state.get("mes_backup_data"):
            st.download_button("⬇️ SQLite yedeğini indir", st.session_state["mes_backup_data"], file_name=f"trex_mes_yedek_{datetime.now():%Y%m%d_%H%M}.db", mime="application/octet-stream", use_container_width=True)
    with log_col:
        audit_table = q('SELECT timestamp AS "Zaman",username AS "Kullanıcı",action AS "İşlem",entity AS "Kayıt",details AS "Ayrıntı" FROM audit_log ORDER BY id DESC LIMIT 100')
        if not show_all_audit and not audit_table.empty:
            audit_dates = pd.to_datetime(audit_table["Zaman"], errors="coerce").dt.date
            audit_start, audit_end = date_range_bounds(selected_audit_range)
            audit_table = audit_table[(audit_dates >= audit_start) & (audit_dates <= audit_end)].copy()
        st.dataframe(audit_table, use_container_width=True, hide_index=True, height=270)


if selected_module == "📄 Raporlar":
    st.subheader("📄 Operasyon Raporları")
    st.caption("Güncel makine, alarm, iş emri, bakım, kalite ve hedef tahmin verilerini indirin.")
    report_col1, report_col2, report_col3 = st.columns(3)
    report_col1.metric("Toplam Üretim", f"{int(df['production'].sum()):,} adet")
    report_col2.metric("Açık Alarm", int(q("SELECT COUNT(*) AS n FROM alarms WHERE acknowledged=0").iloc[0]["n"]))
    report_col3.metric("Hedef Riski", int((target_forecast(df)["Durum"] == "Riskte").sum()))

    file_col1, file_col2 = st.columns(2)
    with file_col1:
        st.download_button(
            "📥 Excel raporunu indir",
            data=make_excel_report(),
            file_name=f"trex_mes_rapor_{date.today():%Y%m%d}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True
        )
    with file_col2:
        st.download_button(
            "📥 PDF özetini indir",
            data=make_pdf_report(),
            file_name=f"trex_mes_ozet_{date.today():%Y%m%d}.pdf",
            mime="application/pdf",
            use_container_width=True
        )

    st.markdown("#### Vardiya sonu hedef tahmini")
    st.dataframe(target_forecast(df), use_container_width=True, hide_index=True)


if selected_module == "📊 Veritabanı":
    st.subheader("Veri ve Raporlama")
    st.caption("Sistemdeki tüm operasyon verilerini inceleyin; Excel tam veri paketi ve PDF operasyon özetini indirin.")
    report_col1, report_col2, report_col3 = st.columns(3)
    report_col1.metric("Toplam Üretim", f"{int(df['production'].sum()):,} adet")
    report_col2.metric("Açık Alarm", int(q("SELECT COUNT(*) AS n FROM alarms WHERE acknowledged=0").iloc[0]["n"]))
    report_col3.metric("Hedef Riski", int((target_forecast(df)["Durum"] == "Riskte").sum()))
    export_excel, export_pdf = st.columns(2)
    with export_excel:
        st.download_button(
            "Excel operasyon raporunu indir", make_excel_report(),
            file_name=f"trex_mes_rapor_{date.today():%Y%m%d}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True
        )
    with export_pdf:
        st.download_button(
            "PDF operasyon özetini indir", make_pdf_report(),
            file_name=f"trex_mes_ozet_{date.today():%Y%m%d}.pdf",
            mime="application/pdf", use_container_width=True
        )
    st.divider()

    tables = [
        "machines",
        "production_history",
        "downtime",
        "alarms",
        "work_orders",
        "sensors",
        "sensor_history",
        "quality",
        "maintenance",
        "operators",
        "shifts",
        "products",
        "stock",
        "energy_readings",
        "maintenance_requests",
        "alarm_actions",
        "audit_log",
        "users",
        "user_permissions"
    ]
    table_descriptions = {
        "machines": "Makine anlık durumu ve OEE girdileri",
        "production_history": "Üretim simülasyon geçmişi",
        "downtime": "Duruş kayıtları ve nedenleri",
        "alarms": "Alarm olayları ve onay durumu",
        "work_orders": "İş emirleri ve ilerleme bilgileri",
        "sensors": "Anlık sensör değerleri",
        "sensor_history": "Sensör zaman serisi",
        "quality": "Kalite kontrol kayıtları",
        "maintenance": "Bakım planı ve geçmişi",
        "operators": "Operatör listesi",
        "shifts": "Vardiya tanımları",
        "products": "Ürün ve minimum stok seviyeleri",
        "stock": "Stok hareketleri",
        "energy_readings": "Makine enerji ölçüm geçmişi",
        "maintenance_requests": "Operatör ve bakım talepleri",
        "alarm_actions": "Alarm atama ve müdahale kayıtları",
        "audit_log": "Kullanıcı işlem denetim kayıtları",
        "users": "Kullanıcı hesapları (şifre özetleri güvenlik için görünür olabilir)",
        "user_permissions": "Kullanıcıya özel modül yetkileri",
    }

    selected_table = st.selectbox(
        "SQLite tablosu",
        tables
    )

    selected_table_data = q(f"SELECT * FROM {selected_table}")
    if selected_table == "users" and "password_hash" in selected_table_data.columns:
        selected_table_data = selected_table_data.drop(columns=["password_hash"])
    db_metrics = st.columns(2)
    db_metrics[0].metric("Kayıt Sayısı", len(selected_table_data))
    db_metrics[1].caption(table_descriptions[selected_table])
    db_search = st.text_input("Tabloda ara", placeholder="Metin girin", key="database_search")
    if db_search:
        search_mask = selected_table_data.astype(str).apply(
            lambda column: column.str.contains(db_search, case=False, na=False)
        ).any(axis=1)
        selected_table_data = selected_table_data[search_mask]

    st.dataframe(
        selected_table_data,
        use_container_width=True,
        hide_index=True
    )

    output = io.BytesIO()

    with pd.ExcelWriter(
        output,
        engine="openpyxl"
    ) as writer:

        for table_name in tables:
            export_table = q(f"SELECT * FROM {table_name}")
            if table_name == "users" and "password_hash" in export_table.columns:
                export_table = export_table.drop(columns=["password_hash"])
            export_table.to_excel(
                writer,
                sheet_name=table_name[:31],
                index=False
            )

    st.download_button(
        "📥 Tüm Veritabanını Excel indir",
        output.getvalue(),
        f"trex_mes_{datetime.now():%Y%m%d_%H%M}.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


    st.divider()

    st.caption(
    f"trex MES • FastAPI: {API_URL} • SQLite: {DB} • "
    f"{datetime.now():%d.%m.%Y %H:%M:%S}"
    )
# =========================================================
# TREX ALT BİLGİ
# =========================================================

st.markdown("""
<div class="trex-footer">
    <strong>🏭 trex Sanal Fabrika</strong><br>
    Daha akıllı üretim • Daha verimli süreçler • Daha güçlü gelecek
</div>
""", unsafe_allow_html=True)

