import random
import sqlite3
import statistics
import uuid
from datetime import datetime

from fastapi import FastAPI

DB = "mes.db"

app = FastAPI(
    title="TREX MES API",
    version="1.0"
)


def conn():
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


@app.get("/")
def root():
    return {
        "message": "TREX MES API aktif",
        "docs": "/docs"
    }


@app.get("/machines")
def get_machines():
    c = conn()

    rows = c.execute("""
        SELECT *
        FROM machines
        ORDER BY machine_code
    """).fetchall()

    c.close()

    return [
        dict(row)
        for row in rows
    ]


@app.get("/sensors")
def get_sensors():
    c = conn()

    rows = c.execute("""
        SELECT *
        FROM sensors
        ORDER BY machine_code
    """).fetchall()

    c.close()

    return [
        dict(row)
        for row in rows
    ]


@app.get("/alarms")
def get_alarms():
    c = conn()

    rows = c.execute("""
        SELECT *
        FROM alarms
        ORDER BY id DESC
        LIMIT 100
    """).fetchall()

    c.close()

    return [
        dict(row)
        for row in rows
    ]


@app.post("/simulate")
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
                    AND time >= datetime('now','-5 minutes')
                """, (
                    machine["id"],
                    alarm_text
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

