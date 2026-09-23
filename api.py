import random
import sqlite3
import statistics
import uuid
import os
import hmac
from datetime import datetime, timedelta
from pathlib import Path
try:
    import tomllib
except ImportError:
    tomllib = None

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None
    dict_row = None

DB = "mes.db"


def runtime_setting(name, default=""):
    value = os.environ.get(name, "").strip()
    if value:
        return value
    secrets_path = Path(__file__).with_name(".streamlit") / "secrets.toml"
    if tomllib and secrets_path.exists():
        try:
            with secrets_path.open("rb") as secrets_file:
                return str(tomllib.load(secrets_file).get(name, default)).strip()
        except Exception:
            pass
    return default


DATABASE_URL = runtime_setting("DATABASE_URL")
TREX_API_KEY = runtime_setting("TREX_API_KEY")
API_CORS_ORIGINS = [item.strip() for item in runtime_setting("API_CORS_ORIGINS", "*").split(",") if item.strip()]


def _postgres_sql(sql):
    return "%s".join(part.replace("%", "%%") for part in sql.split("?"))


class PostgresConnection:
    def __init__(self):
        if psycopg is None:
            raise RuntimeError("PostgreSQL için psycopg kurulu değil.")
        self.connection = psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=False, connect_timeout=10)

    def execute(self, sql, params=()):
        return self.connection.execute(_postgres_sql(sql) if params else sql, params)

    def commit(self):
        self.connection.commit()

    def close(self):
        self.connection.close()

app = FastAPI(
    title="TREX MES API",
    version="2.0.0",
    description="TREX MES için Neon/SQLite uyumlu üretim veri servisi.",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=API_CORS_ORIGINS or ["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def conn():
    if DATABASE_URL:
        return PostgresConnection()
    c = sqlite3.connect(
        DB,
        check_same_thread=False
    )
    c.row_factory = sqlite3.Row
    return c


def now():
    return datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def require_api_key(x_api_key: str | None = Header(default=None)):
    """TREX_API_KEY ayarlıysa veri uçlarını anahtarla korur."""
    if TREX_API_KEY and not (x_api_key and hmac.compare_digest(x_api_key, TREX_API_KEY)):
        raise HTTPException(status_code=401, detail="Geçerli X-API-Key başlığı gerekli.")


def rows(sql, params=()):
    connection = conn()
    try:
        return [dict(item) for item in connection.execute(sql, params).fetchall()]
    finally:
        connection.close()


def scalar(sql, params=(), default=0):
    result = rows(sql, params)
    if not result:
        return default
    return next(iter(result[0].values()), default)


@app.get("/")
def root():
    return {
        "message": "TREX MES API aktif",
        "version": app.version,
        "database": "Neon PostgreSQL" if DATABASE_URL else "SQLite",
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/health", tags=["Sistem"])
def health():
    try:
        scalar("SELECT 1 AS ok")
        return {"status": "healthy", "database": "postgresql" if DATABASE_URL else "sqlite", "timestamp": now()}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Veritabanı bağlantısı başarısız: {type(exc).__name__}") from exc


@app.get("/api/v1/summary", dependencies=[Depends(require_api_key)], tags=["Yönetim"])
def factory_summary():
    machines = rows("SELECT machine_code,status,production,target,downtime,defective FROM machines ORDER BY machine_code")
    production = sum(int(item.get("production") or 0) for item in machines)
    target = sum(int(item.get("target") or 0) for item in machines)
    return {
        "timestamp": now(),
        "production": production,
        "target": target,
        "target_attainment_pct": round(production / target * 100, 1) if target else 0,
        "machines": len(machines),
        "machine_statuses": {status: sum(1 for item in machines if item.get("status") == status) for status in sorted({str(item.get("status")) for item in machines})},
        "open_alarms": int(scalar("SELECT COUNT(*) AS total FROM alarms WHERE acknowledged=0")),
        "open_work_orders": int(scalar("SELECT COUNT(*) AS total FROM work_orders WHERE status!='Tamamlandı'")),
    }


@app.get("/machines", dependencies=[Depends(require_api_key)], tags=["Makineler"])
@app.get("/api/v1/machines", dependencies=[Depends(require_api_key)], tags=["Makineler"])
def get_machines():
    return rows("SELECT * FROM machines ORDER BY machine_code")


@app.get("/api/v1/machines/{machine_code}", dependencies=[Depends(require_api_key)], tags=["Makineler"])
def get_machine(machine_code: str):
    machine = rows("SELECT * FROM machines WHERE machine_code=? LIMIT 1", (machine_code,))
    if not machine:
        raise HTTPException(status_code=404, detail="Makine bulunamadı.")
    payload = machine[0]
    payload["sensor"] = rows("SELECT temperature,vibration,pressure,rpm,timestamp FROM sensors WHERE machine_code=? LIMIT 1", (machine_code,))
    payload["active_alarms"] = rows("SELECT id,alarm,level,time FROM alarms WHERE machine_code=? AND acknowledged=0 ORDER BY id DESC LIMIT 10", (machine_code,))
    payload["active_work_orders"] = rows("SELECT order_no,product,target,produced,priority,status,due_date FROM work_orders WHERE machine_code=? AND status!='Tamamlandı' ORDER BY id DESC LIMIT 10", (machine_code,))
    return payload


@app.get("/sensors", dependencies=[Depends(require_api_key)], tags=["Canlı Veri"])
@app.get("/api/v1/sensors", dependencies=[Depends(require_api_key)], tags=["Canlı Veri"])
def get_sensors():
    return rows("SELECT * FROM sensors ORDER BY machine_code")


@app.get("/alarms", dependencies=[Depends(require_api_key)], tags=["Operasyon"])
@app.get("/api/v1/alarms", dependencies=[Depends(require_api_key)], tags=["Operasyon"])
def get_alarms(active_only: bool = Query(default=False)):
    where = "WHERE acknowledged=0" if active_only else ""
    return rows(f"SELECT * FROM alarms {where} ORDER BY id DESC LIMIT 100")


@app.get("/api/v1/work-orders", dependencies=[Depends(require_api_key)], tags=["Operasyon"])
def get_work_orders(open_only: bool = Query(default=True)):
    where = "WHERE status!='Tamamlandı'" if open_only else ""
    return rows(f"SELECT * FROM work_orders {where} ORDER BY id DESC LIMIT 200")


@app.get("/api/v1/actions", dependencies=[Depends(require_api_key)], tags=["Operasyon"])
def get_actions(open_only: bool = Query(default=True)):
    where = "WHERE status NOT IN ('Tamamlandı','Doğrulandı','İptal')" if open_only else ""
    try:
        return rows(f"SELECT * FROM operational_actions {where} ORDER BY id DESC LIMIT 200")
    except Exception:
        return []


@app.post("/simulate", dependencies=[Depends(require_api_key)], tags=["Simülasyon"])
@app.post("/api/v1/simulate", dependencies=[Depends(require_api_key)], tags=["Simülasyon"])
def simulate_factory():
    c = conn()
    c.execute("""
        CREATE TABLE IF NOT EXISTS spc_measurements(
            id INTEGER PRIMARY KEY,machine_id INTEGER,machine_code TEXT NOT NULL,
            product TEXT NOT NULL,measurement_name TEXT NOT NULL,
            measurement_value REAL NOT NULL,unit TEXT NOT NULL,timestamp TEXT NOT NULL,
            spec_low REAL,spec_high REAL
        )
    """)

    machines = c.execute("""
        SELECT *
        FROM machines
        ORDER BY machine_code
    """).fetchall()

    changed = []

    for machine in machines:

        production = int(
            machine["production"]
        )

        target = int(
            machine["target"]
        )

        planned_time = float(
            machine["planned_time"]
        )

        downtime = float(
            machine["downtime"]
        )

        defective = int(
            machine["defective"]
        )

        status = machine["status"]

        new_status = status
        spc_alarm = None

        # ---------------------------------------------
        # ÜRETİM
        # ---------------------------------------------

        if status == "Çalışıyor":

            production = min(
                production + random.randint(2, 12),
                target
            )

            planned_time += random.uniform(
                0.5,
                2.0
            )

            chance = random.random()

            if chance < 0.05:

                new_status = "Beklemede"

                downtime += random.uniform(
                    1,
                    3
                )

            elif chance < 0.08:

                new_status = "Arızalı"

                downtime += random.uniform(
                    1,
                    4
                )

        elif status == "Beklemede":

            planned_time += random.uniform(
                0.5,
                2.0
            )

            if random.random() < 0.20:
                new_status = "Çalışıyor"

        elif status == "Arızalı":

            planned_time += random.uniform(
                0.5,
                2.0
            )

            downtime += random.uniform(
                0.5,
                1.5
            )

            if random.random() < 0.15:
                new_status = "Çalışıyor"

        # ---------------------------------------------
        # KALİTE
        # ---------------------------------------------

        if (
            production > int(machine["production"])
            and random.random() < 0.20
        ):
            defective = min(
                defective + random.choice([0, 0, 1]),
                production
            )

        if production > int(machine["production"]) and new_status == "Çalışıyor":
            recent_rows = c.execute("""
                SELECT measurement_value FROM spc_measurements
                WHERE machine_code=? AND measurement_name='Çap'
                ORDER BY timestamp DESC LIMIT 20
            """, (machine["machine_code"],)).fetchall()
            recent_values = [float(item["measurement_value"]) for item in recent_rows]
            drift = .018 if machine["machine_code"] == "CNC-02" and random.random() < .35 else 0
            spc_value = round(25 + random.uniform(-.035, .035) + drift, 3)
            c.execute("""
                INSERT INTO spc_measurements(
                    id,machine_id,machine_code,product,measurement_name,
                    measurement_value,unit,timestamp,spec_low,spec_high
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """, (int(uuid.uuid4().int % 2_000_000_000) or 1, machine["id"], machine["machine_code"],
                  machine["product"] or "Tanımsız Ürün", "Çap", spc_value, "mm", now(), 24.90, 25.10))
            if len(recent_values) >= 5:
                center = statistics.mean(recent_values)
                deviation = statistics.stdev(recent_values)
                outside_control = deviation > 0 and abs(spc_value - center) > 3 * deviation
                outside_spec = spc_value < 24.90 or spc_value > 25.10
                if outside_control or outside_spec:
                    spc_alarm = (f"SPC: Çap ölçümü kontrol limitini aştı ({spc_value:.3f} mm)", "Kritik" if outside_spec else "Uyarı")

        planned_time = min(
            planned_time,
            1440
        )

        # ---------------------------------------------
        # MAKİNE GÜNCELLE
        # ---------------------------------------------

        c.execute("""
            UPDATE machines
            SET status=?,
                production=?,
                planned_time=?,
                downtime=?,
                defective=?
            WHERE id=?
        """, (
            new_status,
            production,
            planned_time,
            downtime,
            defective,
            machine["id"]
        ))

        timestamp = now()

        # ---------------------------------------------
        # ÜRETİM GEÇMİŞİ
        # ---------------------------------------------

        c.execute("""
            INSERT INTO production_history(
                machine_id,
                machine_code,
                quantity,
                timestamp
            )
            VALUES(?,?,?,?)
        """, (
            machine["id"],
            machine["machine_code"],
            production,
            timestamp
        ))

        # ---------------------------------------------
        # SENSÖR
        # ---------------------------------------------

        sensor = c.execute("""
            SELECT *
            FROM sensors
            WHERE machine_id=?
        """, (
            machine["id"],
        )).fetchone()

        if sensor:

            temperature = float(
                sensor["temperature"]
            )

            vibration = float(
                sensor["vibration"]
            )

            pressure = float(
                sensor["pressure"]
            )

            rpm = int(
                sensor["rpm"]
            )

            if new_status == "Çalışıyor":

                temperature += random.uniform(
                    -1.0,
                    1.5
                )

                vibration += random.uniform(
                    -0.15,
                    0.20
                )

                pressure += random.uniform(
                    -0.10,
                    0.10
                )

                rpm += random.randint(
                    -30,
                    30
                )

            elif new_status == "Beklemede":

                temperature += random.uniform(
                    -0.5,
                    0.3
                )

                vibration += random.uniform(
                    -0.05,
                    0.05
                )

                rpm = 0

            else:

                temperature += random.uniform(
                    0.5,
                    2.0
                )

                vibration += random.uniform(
                    0.20,
                    0.60
                )

                pressure += random.uniform(
                    -0.20,
                    0.10
                )

                rpm = 0

            temperature = max(
                20,
                min(temperature, 100)
            )

            vibration = max(
                0,
                min(vibration, 12)
            )

            pressure = max(
                0,
                min(pressure, 8)
            )

            rpm = max(
                0,
                min(rpm, 1800)
            )

            # Sensör aşırı değerdeyse makine arızalı.
            if (
                temperature >= 95
                or vibration >= 10
            ):
                new_status = "Arızalı"

                c.execute("""
                    UPDATE machines
                    SET status='Arızalı'
                    WHERE id=?
                """, (
                    machine["id"],
                ))

            c.execute("""
                UPDATE sensors
                SET temperature=?,
                    vibration=?,
                    pressure=?,
                    rpm=?,
                    timestamp=?
                WHERE machine_id=?
            """, (
                temperature,
                vibration,
                pressure,
                rpm,
                timestamp,
                machine["id"]
            ))

            c.execute("""
                INSERT INTO sensor_history(
                    machine_id,
                    machine_code,
                    temperature,
                    vibration,
                    pressure,
                    rpm,
                    timestamp
                )
                VALUES(?,?,?,?,?,?,?)
            """, (
                machine["id"],
                machine["machine_code"],
                temperature,
                vibration,
                pressure,
                rpm,
                timestamp
            ))

            # -----------------------------------------
            # ALARM
            # -----------------------------------------

            alarm_list = []
            if spc_alarm:
                alarm_list.append(spc_alarm)

            if temperature >= 85:
                alarm_list.append(
                    (
                        "Motor sıcaklığı kritik seviyede",
                        "Kritik"
                    )
                )
            elif temperature >= 75:
                alarm_list.append(
                    (
                        "Motor sıcaklığı yükseldi",
                        "Uyarı"
                    )
                )

            if vibration >= 7:
                alarm_list.append(
                    (
                        "Titreşim seviyesi kritik",
                        "Kritik"
                    )
                )
            elif vibration >= 4:
                alarm_list.append(
                    (
                        "Titreşim seviyesi yükseldi",
                        "Uyarı"
                    )
                )

            if pressure < 4:
                alarm_list.append(
                    (
                        "Basınç seviyesi düşük",
                        "Kritik"
                    )
                )
            elif pressure < 5:
                alarm_list.append(
                    (
                        "Basınç seviyesi düşüyor",
                        "Uyarı"
                    )
                )

            for alarm_text, level in alarm_list:

                recent = c.execute("""
                    SELECT 1
                    FROM alarms
                    WHERE machine_id=?
                    AND alarm=?
                    AND time >= ?
                """, (
                    machine["id"],
                    alarm_text,
                    (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
                )).fetchone()

                if not recent:

                    c.execute("""
                        INSERT INTO alarms(
                            machine_id,
                            machine_code,
                            alarm,
                            level,
                            time
                        )
                        VALUES(?,?,?,?,?)
                    """, (
                        machine["id"],
                        machine["machine_code"],
                        alarm_text,
                        level,
                        timestamp
                    ))

        changed.append({
            "machine_code": machine["machine_code"],
            "status": new_status,
            "production": production,
            "target": target,
            "planned_time": planned_time,
            "downtime": downtime,
            "ideal_cycle": machine["ideal_cycle"],
            "defective": defective,
            "product": machine["product"],
            "operator": machine["operator"],
            "shift": machine["shift"],
            "last_maintenance": machine["last_maintenance"],
            "next_maintenance": machine["next_maintenance"]
        })

    c.commit()
    c.close()

    return {
        "success": True,
        "timestamp": now(),
        "machines": changed
    }

