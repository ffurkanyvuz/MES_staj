import uuid
from datetime import datetime


def _id():
    return int(uuid.uuid4().int % 2_000_000_000) or 1


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _value(row, key, index=0, default=None):
    if row is None:
        return default
    try:
        value = row[key]
    except (TypeError, KeyError, IndexError):
        try:
            value = row[index]
        except (TypeError, IndexError):
            return default
    return default if value is None else value


def ensure_process_data_schema(connection_factory):
    """Etiket, veri kaynağı, lisans ve okuma tablolarını SQLite/PostgreSQL'de kurar."""
    connection = connection_factory()
    connection.execute("""
        CREATE TABLE IF NOT EXISTS process_data_licenses(
            id INTEGER PRIMARY KEY,
            license_name TEXT NOT NULL,
            tag_limit INTEGER NOT NULL,
            status TEXT NOT NULL,
            valid_until TEXT,
            created_at TEXT NOT NULL
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS process_data_sources(
            id INTEGER PRIMARY KEY,
            source_code TEXT UNIQUE NOT NULL,
            source_name TEXT NOT NULL,
            source_type TEXT NOT NULL,
            connection_address TEXT,
            status TEXT NOT NULL,
            enabled INTEGER DEFAULT 1,
            last_seen_at TEXT,
            created_at TEXT NOT NULL
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS process_tags(
            id INTEGER PRIMARY KEY,
            tag_code TEXT UNIQUE NOT NULL,
            tag_name TEXT NOT NULL,
            machine_code TEXT,
            category TEXT NOT NULL,
            unit TEXT,
            data_type TEXT NOT NULL,
            source_id INTEGER,
            source_type TEXT NOT NULL,
            source_address TEXT,
            sample_interval_seconds INTEGER DEFAULT 10,
            warning_min REAL,
            warning_max REAL,
            critical_min REAL,
            critical_max REAL,
            scale_factor REAL DEFAULT 1,
            scale_offset REAL DEFAULT 0,
            active INTEGER DEFAULT 1,
            current_value REAL,
            value_quality TEXT DEFAULT 'İyi',
            last_seen_at TEXT,
            created_by TEXT,
            created_at TEXT NOT NULL
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS process_tag_readings(
            id INTEGER PRIMARY KEY,
            tag_id INTEGER NOT NULL,
            tag_code TEXT NOT NULL,
            machine_code TEXT,
            numeric_value REAL,
            text_value TEXT,
            quality TEXT NOT NULL,
            source_timestamp TEXT NOT NULL,
            received_at TEXT NOT NULL
        )
    """)
    connection.execute("CREATE INDEX IF NOT EXISTS idx_process_tags_machine ON process_tags(machine_code,active)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_process_tags_source ON process_tags(source_id,source_type)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_tag_readings_code_time ON process_tag_readings(tag_code,source_timestamp)")

    if not connection.execute("SELECT id FROM process_data_licenses LIMIT 1").fetchone():
        connection.execute(
            "INSERT INTO process_data_licenses(id,license_name,tag_limit,status,valid_until,created_at) VALUES(?,?,?,?,?,?)",
            (_id(), "TREX Etiket Demo", 100, "Aktif", None, _now()),
        )

    source_samples = [
        ("SIM-LOCAL", "TREX Yerel Simülasyon", "Simülasyon", "local://simulation", "Bağlı", 1, _now()),
        ("API-GATEWAY", "TREX Veri Kabul API'si", "API", "/api/v1/tags/readings", "Hazır", 1, None),
        ("PLC-OPCUA-01", "Örnek OPC UA Bağlantısı", "OPC UA", "opc.tcp://192.168.1.10:4840", "Yapılandırılmadı", 0, None),
    ]
    for code, name, source_type, address, status, enabled, last_seen in source_samples:
        if not connection.execute("SELECT id FROM process_data_sources WHERE source_code=?", (code,)).fetchone():
            connection.execute(
                """INSERT INTO process_data_sources(
                    id,source_code,source_name,source_type,connection_address,status,enabled,last_seen_at,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (_id(), code, name, source_type, address, status, enabled, last_seen, _now()),
            )

    sim_source = connection.execute("SELECT id FROM process_data_sources WHERE source_code='SIM-LOCAL'").fetchone()
    sim_source_id = int(_value(sim_source, "id", 0, 0))
    machines = connection.execute("SELECT machine_code FROM machines ORDER BY machine_code").fetchall()
    specs = [
        ("TEMPERATURE", "Motor Sıcaklığı", "Sıcaklık", "temperature", "°C", "Float", None, 75.0, None, 85.0),
        ("VIBRATION", "Gövde Titreşimi", "Titreşim", "vibration", "mm/s", "Float", None, 4.0, None, 5.0),
        ("PRESSURE", "Hat Basıncı", "Basınç", "pressure", "bar", "Float", 4.5, None, 3.5, None),
        ("RPM", "Fener Mili Devri", "Devir", "rpm", "rpm", "Integer", None, 1800.0, None, 2000.0),
    ]
    for machine_row in machines:
        machine_code = str(_value(machine_row, "machine_code", 0, ""))
        sensor = connection.execute(
            "SELECT temperature,vibration,pressure,rpm,timestamp FROM sensors WHERE machine_code=? LIMIT 1",
            (machine_code,),
        ).fetchone()
        for suffix, tag_name, category, field, unit, data_type, warning_min, warning_max, critical_min, critical_max in specs:
            tag_code = f"{machine_code.replace('-', '')}_{suffix}"
            if connection.execute("SELECT id FROM process_tags WHERE tag_code=?", (tag_code,)).fetchone():
                continue
            field_index = {"temperature": 0, "vibration": 1, "pressure": 2, "rpm": 3}[field]
            current_value = float(_value(sensor, field, field_index, 0) or 0)
            sensor_time = str(_value(sensor, "timestamp", 4, _now()))
            tag_id = _id()
            connection.execute("""INSERT INTO process_tags(
                id,tag_code,tag_name,machine_code,category,unit,data_type,source_id,source_type,source_address,
                sample_interval_seconds,warning_min,warning_max,critical_min,critical_max,scale_factor,scale_offset,
                active,current_value,value_quality,last_seen_at,created_by,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (tag_id, tag_code, tag_name, machine_code, category, unit, data_type, sim_source_id, "Simülasyon", field,
                 10, warning_min, warning_max, critical_min, critical_max, 1.0, 0.0, 1, current_value, "İyi",
                 sensor_time, "Sistem", _now()))
            connection.execute("""INSERT INTO process_tag_readings(
                id,tag_id,tag_code,machine_code,numeric_value,text_value,quality,source_timestamp,received_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (_id(), tag_id, tag_code, machine_code, current_value, None, "İyi", sensor_time, _now()))
    connection.commit()
    connection.close()


def record_tag_reading(connection_factory, tag_code, value, quality="İyi", source_timestamp=None):
    """API okumasını kaydeder ve sabit MES sensör alanıyla eşleşiyorsa mevcut ekranları da günceller."""
    connection = connection_factory()
    tag = connection.execute(
        "SELECT id,machine_code,source_address,scale_factor,scale_offset,active FROM process_tags WHERE tag_code=? LIMIT 1",
        (tag_code,),
    ).fetchone()
    if not tag:
        connection.close()
        raise LookupError("Etiket bulunamadı")
    if not int(_value(tag, "active", 5, 0)):
        connection.close()
        raise ValueError("Etiket pasif")
    numeric_value = float(value)
    scaled_value = numeric_value * float(_value(tag, "scale_factor", 3, 1) or 1) + float(_value(tag, "scale_offset", 4, 0) or 0)
    observed_at = source_timestamp or _now()
    received_at = _now()
    tag_id = int(_value(tag, "id", 0, 0))
    machine_code = str(_value(tag, "machine_code", 1, ""))
    source_address = str(_value(tag, "source_address", 2, ""))
    connection.execute("""INSERT INTO process_tag_readings(
        id,tag_id,tag_code,machine_code,numeric_value,text_value,quality,source_timestamp,received_at
        ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (_id(), tag_id, tag_code, machine_code, scaled_value, None, quality, observed_at, received_at))
    connection.execute("""UPDATE process_tags SET current_value=?,value_quality=?,last_seen_at=?,source_type='API'
        WHERE id=?""", (scaled_value, quality, observed_at, tag_id))
    if source_address in {"temperature", "vibration", "pressure", "rpm"} and machine_code:
        connection.execute(
            f"UPDATE sensors SET {source_address}=?,timestamp=? WHERE machine_code=?",
            (scaled_value, observed_at, machine_code),
        )
        current = connection.execute(
            "SELECT machine_id,temperature,vibration,pressure,rpm FROM sensors WHERE machine_code=? LIMIT 1",
            (machine_code,),
        ).fetchone()
        if current:
            connection.execute("""INSERT INTO sensor_history(
                machine_id,machine_code,temperature,vibration,pressure,rpm,timestamp,shift
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (int(_value(current, "machine_id", 0, 0)), machine_code,
                 float(_value(current, "temperature", 1, 0)), float(_value(current, "vibration", 2, 0)),
                 float(_value(current, "pressure", 3, 0)), int(float(_value(current, "rpm", 4, 0))),
                 observed_at, "API"))
    connection.execute("UPDATE process_data_sources SET status='Bağlı',last_seen_at=? WHERE source_code='API-GATEWAY'", (received_at,))
    connection.commit()
    connection.close()
    return {"tag_code": tag_code, "value": scaled_value, "quality": quality, "timestamp": observed_at}
