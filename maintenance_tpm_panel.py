"""TREX MES TPM merkezi: güvenilirlik, otonom ve kestirimci bakım iş akışları."""
from datetime import date, datetime, timedelta
import html
import uuid

import pandas as pd
import plotly.graph_objects as go
import streamlit as st


GREEN, RED, AMBER, BLUE = "#0b9257", "#e65258", "#eea629", "#349bc7"


def _id():
    return int(uuid.uuid4().int % 2_000_000_000) or 1


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe(value):
    return html.escape(str(value or "—"))


def _db_value(row, key, index):
    try:
        return row[key]
    except (TypeError, KeyError, IndexError):
        return row[index]


@st.cache_resource(show_spinner=False)
def ensure_tpm_schema(database_identity, _connection_factory, schema_version="tpm-v1"):
    connection = _connection_factory()
    connection.execute("""
        CREATE TABLE IF NOT EXISTS maintenance_work_orders(
            id INTEGER PRIMARY KEY,
            wo_code TEXT UNIQUE NOT NULL,
            source_request_id INTEGER,
            machine_code TEXT NOT NULL,
            maintenance_type TEXT NOT NULL,
            priority TEXT NOT NULL,
            description TEXT,
            status TEXT NOT NULL,
            technician TEXT,
            failure_detected_at TEXT,
            acknowledged_at TEXT,
            work_started_at TEXT,
            repair_completed_at TEXT,
            returned_to_service_at TEXT,
            root_cause TEXT,
            corrective_action TEXT,
            verification_status TEXT DEFAULT 'Bekliyor',
            verification_note TEXT,
            pre_oee REAL,
            pre_temperature REAL,
            pre_vibration REAL,
            post_oee REAL,
            post_temperature REAL,
            post_vibration REAL,
            five_why_analysis_id INTEGER,
            created_by TEXT,
            created_at TEXT NOT NULL
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS autonomous_maintenance_checks(
            id INTEGER PRIMARY KEY,
            check_code TEXT UNIQUE NOT NULL,
            machine_code TEXT NOT NULL,
            shift_name TEXT,
            operator_name TEXT,
            check_date TEXT NOT NULL,
            cleaning_ok INTEGER,
            lubrication_ok INTEGER,
            leak_ok INTEGER,
            noise_ok INTEGER,
            vibration_ok INTEGER,
            safety_ok INTEGER,
            status TEXT NOT NULL,
            abnormality_note TEXT,
            escalated_request_id INTEGER,
            created_at TEXT NOT NULL
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS preventive_maintenance_plans(
            id INTEGER PRIMARY KEY,
            plan_code TEXT UNIQUE NOT NULL,
            machine_code TEXT NOT NULL,
            plan_name TEXT NOT NULL,
            trigger_type TEXT NOT NULL,
            interval_value REAL NOT NULL,
            last_value REAL DEFAULT 0,
            next_value REAL NOT NULL,
            checklist TEXT,
            estimated_minutes INTEGER DEFAULT 60,
            priority TEXT DEFAULT 'Normal',
            active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS maintenance_parts_usage(
            id INTEGER PRIMARY KEY,
            work_order_id INTEGER NOT NULL,
            product_id INTEGER,
            product_code TEXT,
            quantity REAL NOT NULL,
            unit TEXT,
            recorded_by TEXT,
            recorded_at TEXT NOT NULL
        )
    """)
    connection.execute("CREATE INDEX IF NOT EXISTS idx_tpm_wo_status ON maintenance_work_orders(status,created_at)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_tpm_wo_machine ON maintenance_work_orders(machine_code,failure_detected_at)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_autonomous_machine_date ON autonomous_maintenance_checks(machine_code,check_date)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_pm_plan_machine ON preventive_maintenance_plans(machine_code,active)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_parts_work_order ON maintenance_parts_usage(work_order_id)")
    try:
        connection.execute("ALTER TABLE maintenance_work_orders ADD COLUMN five_why_analysis_id INTEGER")
    except Exception:
        pass

    machines = connection.execute("SELECT machine_code,product FROM machines ORDER BY id LIMIT 3").fetchall()
    machine_codes = [str(_db_value(row, "machine_code", 0)) for row in machines] or ["CNC-01", "CNC-02", "CNC-03"]
    while len(machine_codes) < 3:
        machine_codes.append(f"CNC-0{len(machine_codes) + 1}")
    plan_samples = [
        ("PB-DEMO-001", machine_codes[0], "Aylık mekanik kontrol", "Takvim Günü", 30, 30, "Yağlama; kayış; kaplin; emniyet", 75, "Normal"),
        ("PB-DEMO-002", machine_codes[1], "Rulman titreşim kontrolü", "Çalışma Saati", 500, 500, "Titreşim; sıcaklık; rulman sesi", 45, "Yüksek"),
        ("PB-DEMO-003", machine_codes[2], "Takım ve soğutma kontrolü", "Üretim Adedi", 10000, 10000, "Filtre; debi; takım tutucu", 60, "Normal"),
    ]
    for sample in plan_samples:
        if not connection.execute("SELECT id FROM preventive_maintenance_plans WHERE plan_code=?", (sample[0],)).fetchone():
            connection.execute("""INSERT INTO preventive_maintenance_plans(
                id,plan_code,machine_code,plan_name,trigger_type,interval_value,last_value,next_value,checklist,
                estimated_minutes,priority,active,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (_id(), *sample[:5], 0, *sample[5:], 1, _now()))

    if not connection.execute("SELECT id FROM maintenance_work_orders WHERE wo_code='BE-DEMO-001'").fetchone():
        connection.execute("""INSERT INTO maintenance_work_orders(
            id,wo_code,machine_code,maintenance_type,priority,description,status,technician,failure_detected_at,
            acknowledged_at,work_started_at,root_cause,corrective_action,verification_status,pre_oee,
            pre_temperature,pre_vibration,created_by,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (_id(), "BE-DEMO-001", machine_codes[1], "Arıza Bakımı", "Kritik", "Rulman bölgesinde artan titreşim",
             "Devam Ediyor", "Mehmet Kaya", (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),
             (datetime.now() - timedelta(hours=1, minutes=45)).strftime("%Y-%m-%d %H:%M:%S"),
             (datetime.now() - timedelta(hours=1, minutes=30)).strftime("%Y-%m-%d %H:%M:%S"),
             "İnceleniyor", "Rulman ve kaplin kontrolü", "Bekliyor", 72.1, 78.0, 5.8, "Örnek Veri", _now()))
    if not connection.execute("SELECT id FROM autonomous_maintenance_checks WHERE check_code='OB-DEMO-001'").fetchone():
        connection.execute("""INSERT INTO autonomous_maintenance_checks(
            id,check_code,machine_code,shift_name,operator_name,check_date,cleaning_ok,lubrication_ok,leak_ok,
            noise_ok,vibration_ok,safety_ok,status,abnormality_note,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (_id(), "OB-DEMO-001", machine_codes[0], "Sabah", "Ahmet Yılmaz", date.today().isoformat(),
             1, 1, 1, 0, 1, 1, "Anormallik", "Fener mili bölgesinde olağan dışı ses", _now()))
    connection.commit()
    connection.close()
    return True


def _machine_oee(row):
    if "oee" in row and pd.notna(row.get("oee")):
        value = float(row["oee"])
        return value * 100 if value <= 1 else value
    planned = max(float(row.get("planned_time") or 0), 1)
    availability = max(planned - float(row.get("downtime") or 0), 0) / planned
    target = max(float(row.get("target") or 0), 1)
    performance = min(float(row.get("production") or 0) / target, 1)
    quality = max(float(row.get("production") or 0) - float(row.get("defective") or 0), 0) / max(float(row.get("production") or 0), 1)
    return availability * performance * quality * 100


def _reliability(failures, machine_codes):
    rows = []
    for code in machine_codes:
        machine_failures = failures[failures["machine_code"] == code].copy() if not failures.empty else pd.DataFrame()
        if not machine_failures.empty:
            machine_failures["event_at"] = pd.to_datetime(machine_failures["event_at"], errors="coerce")
            machine_failures["duration"] = pd.to_numeric(machine_failures["duration"], errors="coerce").fillna(0)
            dated = machine_failures.dropna(subset=["event_at"]).sort_values("event_at")
            intervals = dated["event_at"].diff().dt.total_seconds().div(3600).dropna()
            mtbf = float(intervals.mean()) if not intervals.empty else None
            mttr = float(machine_failures["duration"].mean()) / 60
        else:
            mtbf = mttr = None
        rows.append({"Makine": code, "Arıza Sayısı": len(machine_failures), "MTBF (saat)": mtbf, "MTTR (saat)": mttr})
    return pd.DataFrame(rows)


def _health_scores(machines, sensors, history, failures):
    sensor_map = sensors.set_index("machine_code").to_dict("index") if not sensors.empty else {}
    results = []
    for _, machine in machines.iterrows():
        code = str(machine["machine_code"])
        current = sensor_map.get(code, {})
        local_history = history[history["machine_code"] == code].copy() if not history.empty else pd.DataFrame()
        risk, reasons = 0.0, []
        temperature = float(current.get("temperature") or 0)
        vibration = float(current.get("vibration") or 0)
        pressure = float(current.get("pressure") or 0)
        if temperature >= 85: risk += 30; reasons.append(f"Sıcaklık {temperature:.1f}°C")
        elif temperature >= 75: risk += 17; reasons.append("Sıcaklık uyarı bölgesinde")
        if vibration >= 5: risk += 30; reasons.append(f"Titreşim {vibration:.1f} mm/s")
        elif vibration >= 4: risk += 17; reasons.append("Titreşim yükseliyor")
        if pressure and pressure <= 3.5: risk += 15; reasons.append("Basınç düşük")
        if len(local_history) >= 4:
            local_history = local_history.sort_values("timestamp").tail(12)
            temp_change = float(local_history["temperature"].iloc[-1] - local_history["temperature"].iloc[0])
            vibration_change = float(local_history["vibration"].iloc[-1] - local_history["vibration"].iloc[0])
            if temp_change >= 5: risk += 15; reasons.append(f"Sıcaklık trendi +{temp_change:.1f}°C")
            if vibration_change >= 1: risk += 15; reasons.append(f"Titreşim trendi +{vibration_change:.1f}")
        failure_count = len(failures[failures["machine_code"] == code]) if not failures.empty else 0
        if failure_count >= 2: risk += min(20, failure_count * 5); reasons.append(f"{failure_count} arıza kaydı")
        if str(machine.get("status")) == "Arızalı": risk += 30; reasons.append("Makine arızalı")
        risk = min(round(risk), 100)
        level = "Kritik" if risk >= 70 else ("Yüksek" if risk >= 45 else ("İzle" if risk >= 20 else "Normal"))
        results.append({"Makine": code, "Sağlık Puanı": 100-risk, "Risk": risk, "Seviye": level,
                        "Sıcaklık": temperature, "Titreşim": vibration, "Neden": ", ".join(reasons) or "Belirgin risk yok"})
    return pd.DataFrame(results).sort_values("Risk", ascending=False)


def _metric_cards(items):
    columns = st.columns(len(items), gap="small")
    for column, (label, value, note, colour) in zip(columns, items):
        column.markdown(f'<div class="tpm-stat" style="--c:{colour}"><small>{label}</small><b>{value}</b><span>{note}</span></div>', unsafe_allow_html=True)


def render_tpm_center(query, execute, machines, current_user="", can_manage=False, can_operate=False, notifier=None):
    work_orders = query("SELECT * FROM maintenance_work_orders ORDER BY id DESC")
    autonomous = query("SELECT * FROM autonomous_maintenance_checks ORDER BY id DESC")
    plans = query("SELECT * FROM preventive_maintenance_plans ORDER BY active DESC,id DESC")
    failures = query("SELECT machine_code,reason,duration,event_at FROM downtime WHERE reason LIKE '%Arıza%' ORDER BY event_at")
    sensors = query("SELECT machine_code,temperature,vibration,pressure,rpm,timestamp FROM sensors ORDER BY machine_code")
    history = query("SELECT machine_code,temperature,vibration,pressure,rpm,timestamp FROM sensor_history ORDER BY timestamp DESC LIMIT 1000")
    products = query("SELECT id,product_code,product_name,unit,stock,min_stock FROM products ORDER BY product_name")
    history["timestamp"] = pd.to_datetime(history["timestamp"], errors="coerce") if not history.empty else pd.Series(dtype="datetime64[ns]")
    reliability = _reliability(failures, machines["machine_code"].astype(str).tolist())
    health = _health_scores(machines, sensors, history, failures)

    st.markdown("""
    <style>
      .tpm-head{margin:14px 0 8px;background:linear-gradient(120deg,#073e2d,#0d8f58);border-radius:13px;padding:15px 18px;color:#fff;display:flex;justify-content:space-between;align-items:center}.tpm-head h3{color:#fff!important;margin:0!important;font-size:1rem!important}.tpm-head p{margin:4px 0 0;color:#d9f5e7;font-size:.68rem}.tpm-head span{font-size:.58rem;border:1px solid rgba(255,255,255,.3);border-radius:99px;padding:5px 9px}
      .tpm-stat{min-height:80px;border:1px solid #d5e9df;border-left:4px solid var(--c);border-radius:9px;padding:9px 11px;background:linear-gradient(125deg,#fff,#eff9f4)}.tpm-stat small{display:block;color:#527064;font-size:.61rem;font-weight:800}.tpm-stat b{display:block;color:#0a5139;font-size:1.35rem;line-height:1.8rem}.tpm-stat span{font-size:.55rem;color:#71887d}.tpm-section{font-size:.79rem;font-weight:850;color:#10573e;margin:4px 0 8px}
    </style>
    <div class="tpm-head"><div><h3>TPM · Toplam Üretken Bakım Merkezi</h3><p>Arızayı kayıttan kök nedene, sensör riskinden doğrulanmış iyileştirmeye kadar tek döngüde yönetin.</p></div><span>ÖLÇ → ANALİZ ET → İYİLEŞTİR</span></div>
    """, unsafe_allow_html=True)

    open_wo = int((~work_orders["status"].isin(["Tamamlandı", "İptal"])).sum()) if not work_orders.empty else 0
    critical_risk = int((health["Risk"] >= 70).sum()) if not health.empty else 0
    avg_mtbf = reliability["MTBF (saat)"].dropna().mean()
    avg_mttr = reliability["MTTR (saat)"].dropna().mean()
    _metric_cards([
        ("Açık Bakım Emri", str(open_wo), "Yaşam döngüsü devam eden", AMBER if open_wo else GREEN),
        ("Ortalama MTBF", f"{avg_mtbf:.1f} sa" if pd.notna(avg_mtbf) else "—", "Arızalar arası gerçek zaman", GREEN),
        ("Ortalama MTTR", f"{avg_mttr:.1f} sa" if pd.notna(avg_mttr) else "—", "Ortalama onarım süresi", BLUE),
        ("Kritik Bakım Riski", str(critical_risk), "Sensör + trend + arıza", RED if critical_risk else GREEN),
        ("Otonom Kontrol", str(len(autonomous)), "Operatör kayıtları", GREEN),
    ])

    lifecycle_tab, autonomous_tab, predictive_tab, plan_tab, loss_tab, verify_tab = st.tabs([
        "Bakım Yaşam Döngüsü", "Otonom Bakım", "Kestirimci Bakım", "Periyodik Planlar",
        "8 Büyük Kayıp", "Sonuç Doğrulama",
    ])

    with lifecycle_tab:
        st.markdown('<div class="tpm-section">Bakım İş Emirleri ve Gerçek MTBF / MTTR</div>', unsafe_allow_html=True)
        left, right = st.columns([1.35, 1])
        with left:
            if work_orders.empty:
                st.info("Henüz TPM bakım iş emri yok.")
            else:
                view = work_orders[["wo_code", "machine_code", "maintenance_type", "priority", "status", "technician", "failure_detected_at", "work_started_at", "repair_completed_at", "verification_status"]].copy()
                view.columns = ["İş Emri", "Makine", "Tür", "Öncelik", "Durum", "Teknisyen", "Arıza", "Başlangıç", "Onarım Bitişi", "Doğrulama"]
                st.dataframe(view, hide_index=True, use_container_width=True, height=300)
        with right:
            st.dataframe(reliability.round(2), hide_index=True, use_container_width=True, height=300)

        if can_manage:
            with st.expander("Yeni TPM bakım iş emri"):
                with st.form("tpm_new_wo", clear_on_submit=True):
                    a, b, c = st.columns(3)
                    machine_code = a.selectbox("Makine", machines["machine_code"].astype(str).tolist(), key="tpm_wo_machine")
                    maintenance_type = b.selectbox("Bakım türü", ["Arıza Bakımı", "Önleyici Bakım", "Kestirimci Bakım", "Periyodik Bakım"])
                    priority = c.selectbox("Öncelik", ["Kritik", "Yüksek", "Normal", "Düşük"], index=2)
                    description = st.text_area("İş tanımı *")
                    create = st.form_submit_button("Bakım İş Emrini Aç", type="primary", use_container_width=True)
                if create:
                    if not description.strip():
                        st.error("İş tanımı zorunludur.")
                    else:
                        wo_id = _id(); code = f"BE-{wo_id % 100000:05d}"
                        machine = machines[machines["machine_code"] == machine_code].iloc[0]
                        sensor = sensors[sensors["machine_code"] == machine_code]
                        sensor_row = sensor.iloc[0] if not sensor.empty else {}
                        execute("""INSERT INTO maintenance_work_orders(
                            id,wo_code,machine_code,maintenance_type,priority,description,status,failure_detected_at,
                            verification_status,pre_oee,pre_temperature,pre_vibration,created_by,created_at)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (wo_id, code, machine_code, maintenance_type, priority, description.strip(), "Açık", _now(),
                             "Bekliyor", _machine_oee(machine), float(sensor_row.get("temperature") or 0),
                             float(sensor_row.get("vibration") or 0), current_user, _now()))
                        st.success(f"{code} açıldı."); st.rerun()

            active = work_orders[~work_orders["status"].isin(["Tamamlandı", "İptal"])] if not work_orders.empty else pd.DataFrame()
            if not active.empty:
                labels = {int(row["id"]): f'{row["wo_code"]} · {row["machine_code"]} · {row["status"]}' for _, row in active.iterrows()}
                if st.session_state.get("tpm_selected_wo") not in labels:
                    st.session_state.pop("tpm_selected_wo", None)
                selected_id = st.selectbox("İş emri aksiyonu", list(labels), format_func=lambda value: labels[value], key="tpm_selected_wo")
                selected = active[active["id"] == selected_id].iloc[0]
                with st.form("tpm_wo_action"):
                    action = st.selectbox("İşlem", ["Kabul Et", "Bakımı Başlat", "Onarımı Tamamla ve Kaliteye Gönder"])
                    technician = st.text_input("Teknisyen", value=str(selected.get("technician") or current_user))
                    root_cause = st.text_area("Kök neden", value=str(selected.get("root_cause") or ""))
                    corrective = st.text_area("Yapılan işlem", value=str(selected.get("corrective_action") or ""))
                    st.caption("Tekrarlayan ve kritik arızalarda en az iki neden girerek 5 Why analizini iş emrine bağlayın.")
                    why_columns = st.columns(5)
                    why_answers = [why_columns[index].text_input(f"{index + 1}. Neden", key=f"tpm_why_{selected_id}_{index}") for index in range(5)]
                    part_options = ["Kullanılmadı"] + (products["product_code"].astype(str).tolist() if not products.empty else [])
                    p1, p2 = st.columns(2)
                    part_code = p1.selectbox("Kullanılan yedek parça", part_options)
                    part_qty = p2.number_input("Miktar", min_value=0.0, value=1.0 if part_code != "Kullanılmadı" else 0.0)
                    submit = st.form_submit_button("İşlemi Kaydet", type="primary", use_container_width=True)
                if submit:
                    if action == "Kabul Et":
                        execute("UPDATE maintenance_work_orders SET status='Atandı',technician=?,acknowledged_at=? WHERE id=?", (technician, _now(), int(selected_id)))
                    elif action == "Bakımı Başlat":
                        execute("UPDATE maintenance_work_orders SET status='Devam Ediyor',technician=?,work_started_at=? WHERE id=?", (technician, _now(), int(selected_id)))
                        execute("UPDATE machines SET status='Arızalı' WHERE machine_code=?", (selected["machine_code"],))
                    else:
                        if not root_cause.strip() or not corrective.strip():
                            st.error("Onarım tamamlanırken kök neden ve yapılan işlem zorunludur."); st.stop()
                        if part_code != "Kullanılmadı":
                            part = products[products["product_code"] == part_code].iloc[0]
                            if float(part["stock"]) < float(part_qty):
                                st.error(f"Yetersiz stok: {part_code} için {float(part['stock']):g} mevcut."); st.stop()
                            execute("UPDATE products SET stock=stock-? WHERE id=?", (float(part_qty), int(part["id"])))
                            execute("INSERT INTO maintenance_parts_usage(id,work_order_id,product_id,product_code,quantity,unit,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?)",
                                    (_id(), int(selected_id), int(part["id"]), part_code, float(part_qty), part["unit"], current_user, _now()))
                            execute("INSERT INTO stock(product_id,product_code,movement_type,quantity,reason,timestamp) VALUES(?,?,?,?,?,?)",
                                    (int(part["id"]), part_code, "Çıkış", float(part_qty), f'Bakım iş emri {selected["wo_code"]}', _now()))
                        execute("""UPDATE maintenance_work_orders SET status='Kalite Onayı Bekliyor',technician=?,repair_completed_at=?,
                            root_cause=?,corrective_action=? WHERE id=?""", (technician, _now(), root_cause.strip(), corrective.strip(), int(selected_id)))
                        filled_whys = [answer.strip() for answer in why_answers if answer.strip()]
                        if len(filled_whys) >= 2 and not selected.get("five_why_analysis_id"):
                            analysis_id = _id()
                            execute("""INSERT INTO five_why_analyses(id,title,event_type,source_type,source_id,machine_code,
                                event_description,priority,status,owner,due_date,root_cause,containment_action,
                                corrective_action,verification_method,created_by,created_at,completed_at)
                                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                (analysis_id, f'{selected["wo_code"]} · {selected["machine_code"]} kök neden analizi',
                                 "Bakım Arızası", "Bakım İş Emri", int(selected_id), selected["machine_code"],
                                 selected["description"], selected["priority"], "Aksiyon Açık", technician,
                                 (date.today() + timedelta(days=7)).isoformat(), root_cause.strip(), "Makine kalite onayına kadar bekletildi",
                                 corrective.strip(), "Bakım sonrası kalite, sensör ve OEE doğrulaması", current_user, _now(), None))
                            prompts = ["Problem neden oluştu?", "Bu durum neden gerçekleşti?", "Önceki neden neden ortaya çıktı?", "Sistem neden önleyemedi?", "Kök neden neden kalıcılaştı?"]
                            for step_no, answer in enumerate(why_answers, 1):
                                if answer.strip():
                                    execute("INSERT INTO five_why_steps(id,analysis_id,step_no,question,answer) VALUES(?,?,?,?,?)",
                                            (_id(), analysis_id, step_no, prompts[step_no - 1], answer.strip()))
                            execute("UPDATE maintenance_work_orders SET five_why_analysis_id=? WHERE id=?", (analysis_id, int(selected_id)))
                        execute("UPDATE machines SET status='Beklemede' WHERE machine_code=?", (selected["machine_code"],))
                        source_key = f"maintenance-work-order:{int(selected_id)}"
                        if query("SELECT id FROM quality_inspection_tasks WHERE source_key=?", (source_key,)).empty:
                            task_id = _id()
                            execute("""INSERT INTO quality_inspection_tasks(id,task_code,source_key,plan_id,work_order,machine_code,product,
                                inspection_stage,trigger_reason,due_quantity,status,result,assigned_role,created_at)
                                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                (task_id, f"KK-{task_id % 100000:05d}", source_key, None, selected["wo_code"], selected["machine_code"],
                                 str(machines[machines["machine_code"] == selected["machine_code"]].iloc[0].get("product") or "Bakım Sonrası Ürün"),
                                 "Bakım Sonrası", "Bakım tamamlandı; devreye alma onayı", 0, "Bekliyor", None, "quality", _now()))
                            if notifier:
                                notifier(recipient_role="quality", notification_type="Bakım Sonrası Kalite", priority="Kritik",
                                         title=f'{selected["machine_code"]} devreye alma onayı', message=f'{selected["wo_code"]} sonrası ilk ürün kontrolü bekliyor',
                                         machine_code=selected["machine_code"], target_module="✅ Kalite", entity_type="quality_inspection", entity_id=task_id)
                    st.success("Bakım iş emri güncellendi."); st.rerun()

    with autonomous_tab:
        st.markdown('<div class="tpm-section">Operatör Otonom Bakım Kontrolü</div>', unsafe_allow_html=True)
        if can_operate:
            with st.form("autonomous_check_form", clear_on_submit=True):
                a, b, c = st.columns(3)
                machine_code = a.selectbox("Makine", machines["machine_code"].astype(str).tolist(), key="aut_machine")
                shift = b.selectbox("Vardiya", ["Sabah", "Akşam", "Gece"])
                operator = c.text_input("Operatör", value=current_user)
                checks = st.columns(6)
                labels = ["Temizlik", "Yağlama", "Sızıntı", "Anormal ses", "Titreşim", "Emniyet"]
                values = [checks[i].checkbox(label, value=True, key=f"aut_{i}") for i, label in enumerate(labels)]
                note = st.text_area("Anormallik açıklaması", placeholder="Uygun olmayan kontrolde gözlemi yazın")
                save = st.form_submit_button("Vardiya Kontrolünü Kaydet", type="primary", use_container_width=True)
            if save:
                abnormal = not all(values)
                if abnormal and not note.strip():
                    st.error("Uygun olmayan kontrol için açıklama girin.")
                else:
                    check_id = _id(); request_id = None
                    if abnormal:
                        request_id = _id()
                        execute("""INSERT INTO maintenance_requests(id,machine_code,request_type,description,priority,requested_by,requested_at,status)
                            VALUES(?,?,?,?,?,?,?,?)""", (request_id, machine_code, "Otonom Bakım Bulgusu", note.strip(), "Yüksek", operator, _now(), "Açık"))
                        if notifier:
                            notifier(recipient_role="maintenance", notification_type="Otonom Bakım", priority="Uyarı",
                                     title=f"{machine_code} operatör anormalliği", message=note.strip(), machine_code=machine_code,
                                     target_module="🧰 Bakım Talebi", entity_type="maintenance_request", entity_id=request_id)
                    execute("""INSERT INTO autonomous_maintenance_checks(id,check_code,machine_code,shift_name,operator_name,check_date,
                        cleaning_ok,lubrication_ok,leak_ok,noise_ok,vibration_ok,safety_ok,status,abnormality_note,escalated_request_id,created_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (check_id, f"OB-{check_id % 100000:05d}", machine_code, shift, operator, date.today().isoformat(),
                         *[1 if value else 0 for value in values], "Anormallik" if abnormal else "Uygun", note.strip(), request_id, _now()))
                    st.success("Kontrol kaydedildi." + (" Bakım talebi otomatik açıldı." if abnormal else "")); st.rerun()
        if autonomous.empty:
            st.info("Otonom bakım kontrolü bulunmuyor.")
        else:
            view = autonomous[["check_code", "machine_code", "shift_name", "operator_name", "check_date", "status", "abnormality_note", "escalated_request_id"]].copy()
            view.columns = ["Kontrol", "Makine", "Vardiya", "Operatör", "Tarih", "Durum", "Anormallik", "Bakım Talebi"]
            st.dataframe(view, hide_index=True, use_container_width=True, height=330)

    with predictive_tab:
        st.markdown('<div class="tpm-section">Makine Sağlığı ve Kestirimci Bakım</div>', unsafe_allow_html=True)
        left, right = st.columns([1.2, 1])
        with left:
            st.dataframe(health, hide_index=True, use_container_width=True, height=320)
        with right:
            fig = go.Figure(go.Bar(x=health["Risk"], y=health["Makine"], orientation="h", marker_color=[RED if value >= 70 else AMBER if value >= 45 else GREEN for value in health["Risk"]], text=health["Risk"], textposition="outside"))
            fig.update_layout(height=320, margin=dict(l=10, r=30, t=20, b=30), xaxis_range=[0, 105], xaxis_title="Risk / 100", paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
        if can_manage and not health.empty:
            machine_code = st.selectbox("Riskten bakım talebi oluştur", health["Makine"].tolist(), key="predictive_request_machine")
            risk_row = health[health["Makine"] == machine_code].iloc[0]
            if st.button("Kestirimci Bakım Talebi Aç", type="primary", use_container_width=True):
                existing = query("SELECT id FROM maintenance_requests WHERE machine_code=? AND request_type='Kestirimci Bakım' AND status!='Tamamlandı'", (machine_code,))
                if not existing.empty:
                    st.info("Bu makine için açık kestirimci bakım talebi zaten var.")
                else:
                    request_id = _id()
                    execute("""INSERT INTO maintenance_requests(id,machine_code,request_type,description,priority,requested_by,requested_at,status)
                        VALUES(?,?,?,?,?,?,?,?)""", (request_id, machine_code, "Kestirimci Bakım", f'Risk {risk_row["Risk"]}/100 · {risk_row["Neden"]}', "Kritik" if risk_row["Risk"] >= 70 else "Yüksek", current_user, _now(), "Açık"))
                    st.success("Kestirimci bakım talebi açıldı.")

    with plan_tab:
        st.markdown('<div class="tpm-section">Takvim, Çalışma Saati ve Üretim Adedi Bazlı Planlar</div>', unsafe_allow_html=True)
        if can_manage:
            with st.expander("Yeni periyodik bakım planı"):
                with st.form("preventive_plan_form", clear_on_submit=True):
                    a, b, c = st.columns(3)
                    machine_code = a.selectbox("Makine", machines["machine_code"].astype(str).tolist(), key="pm_machine")
                    name = b.text_input("Plan adı *")
                    trigger = c.selectbox("Tetikleyici", ["Takvim Günü", "Çalışma Saati", "Üretim Adedi", "Sensör Riski"])
                    d, e, f = st.columns(3)
                    interval = d.number_input("Periyot / eşik", min_value=1.0, value=30.0)
                    estimated = e.number_input("Tahmini süre (dk)", min_value=5, value=60)
                    priority = f.selectbox("Öncelik", ["Kritik", "Yüksek", "Normal", "Düşük"], index=2)
                    checklist = st.text_area("Kontrol listesi", placeholder="Yağlama; filtre; kayış; emniyet")
                    save = st.form_submit_button("Planı Kaydet", type="primary", use_container_width=True)
                if save:
                    if not name.strip(): st.error("Plan adı zorunludur.")
                    else:
                        plan_id = _id()
                        execute("""INSERT INTO preventive_maintenance_plans(id,plan_code,machine_code,plan_name,trigger_type,
                            interval_value,last_value,next_value,checklist,estimated_minutes,priority,active,created_at)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (plan_id, f"PB-{plan_id % 100000:05d}", machine_code, name.strip(), trigger,
                            float(interval), 0, float(interval), checklist.strip(), int(estimated), priority, 1, _now()))
                        st.rerun()
        st.dataframe(plans[["plan_code", "machine_code", "plan_name", "trigger_type", "interval_value", "next_value", "estimated_minutes", "priority", "active"]].rename(columns={"plan_code":"Plan", "machine_code":"Makine", "plan_name":"Ad", "trigger_type":"Tetikleyici", "interval_value":"Periyot", "next_value":"Sonraki Eşik", "estimated_minutes":"Süre (dk)", "priority":"Öncelik", "active":"Aktif"}), hide_index=True, use_container_width=True, height=310)
        if can_manage and not plans.empty:
            labels = {int(row["id"]): f'{row["plan_code"]} · {row["machine_code"]} · {row["plan_name"]}' for _, row in plans.iterrows()}
            if st.session_state.get("pm_generate") not in labels:
                st.session_state.pop("pm_generate", None)
            plan_id = st.selectbox("Plandan iş emri oluştur", list(labels), format_func=lambda value: labels[value], key="pm_generate")
            if st.button("Planlı Bakım İş Emri Oluştur", use_container_width=True):
                plan = plans[plans["id"] == plan_id].iloc[0]
                wo_id = _id()
                execute("""INSERT INTO maintenance_work_orders(id,wo_code,machine_code,maintenance_type,priority,description,status,
                    verification_status,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (wo_id, f"BE-{wo_id % 100000:05d}", plan["machine_code"], "Periyodik Bakım", plan["priority"],
                     f'{plan["plan_name"]} · {plan["checklist"]}', "Açık", "Bekliyor", current_user, _now()))
                execute("UPDATE preventive_maintenance_plans SET last_value=next_value,next_value=next_value+interval_value WHERE id=?", (int(plan_id),))
                st.success("Planlı bakım iş emri oluşturuldu."); st.rerun()

    with loss_tab:
        st.markdown('<div class="tpm-section">TPM · Sekiz Büyük Üretim Kaybı</div>', unsafe_allow_html=True)
        downtime = query("SELECT machine_code,reason,duration,event_at FROM downtime ORDER BY id DESC")
        quality = query("SELECT defective FROM quality")
        if downtime.empty:
            st.info("Kayıp analizi için duruş kaydı yok.")
        else:
            downtime["duration"] = pd.to_numeric(downtime["duration"], errors="coerce").fillna(0)
            def classify(reason):
                value = str(reason).casefold()
                if "arıza" in value: return "Arıza"
                if "ayar" in value or "setup" in value or "kalıp" in value: return "Setup / Ayar"
                if "başlangıç" in value: return "Başlangıç Kaybı"
                if "küçük" in value: return "Küçük Duruş"
                if "bekle" in value or "boş" in value: return "Boşta Bekleme"
                if "hız" in value or "çevrim" in value: return "Hız Kaybı"
                if "kalite" in value or "hata" in value: return "Hatalı Üretim"
                return "Planlı / Üretim Dışı"
            downtime["Kayıp Türü"] = downtime["reason"].map(classify)
            loss = downtime.groupby("Kayıp Türü")["duration"].sum().reindex(["Arıza", "Setup / Ayar", "Başlangıç Kaybı", "Küçük Duruş", "Boşta Bekleme", "Hız Kaybı", "Hatalı Üretim", "Planlı / Üretim Dışı"], fill_value=0)
            if not quality.empty:
                loss.loc["Hatalı Üretim"] += float(pd.to_numeric(quality["defective"], errors="coerce").fillna(0).sum())
            total = max(float(loss.sum()), 1)
            loss_frame = loss.rename("Kayıp").reset_index()
            loss_frame["Pay %"] = loss_frame["Kayıp"] / total * 100
            loss_frame["Kümülatif %"] = loss_frame["Kayıp"].cumsum() / total * 100
            left, right = st.columns([1.2, 1])
            fig = go.Figure(go.Bar(x=loss_frame["Kayıp Türü"], y=loss_frame["Kayıp"], marker_color=GREEN, text=loss_frame["Kayıp"].round(1)))
            fig.add_trace(go.Scatter(x=loss_frame["Kayıp Türü"], y=loss_frame["Kümülatif %"], yaxis="y2", mode="lines+markers", line=dict(color=RED), name="Kümülatif %"))
            fig.update_layout(height=340, yaxis2=dict(overlaying="y", side="right", range=[0, 110]), margin=dict(l=10, r=20, t=20, b=80), paper_bgcolor="rgba(0,0,0,0)")
            left.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
            right.dataframe(loss_frame.round(1), hide_index=True, use_container_width=True, height=340)

    with verify_tab:
        st.markdown('<div class="tpm-section">Bakım Öncesi / Sonrası Etkinlik Doğrulaması</div>', unsafe_allow_html=True)
        candidates = work_orders[work_orders["status"].isin(["Tamamlandı", "Kalite Onayı Bekliyor"])] if not work_orders.empty else pd.DataFrame()
        if candidates.empty:
            st.info("Doğrulanabilecek tamamlanmış bakım iş emri yok.")
        else:
            view = candidates[["wo_code", "machine_code", "status", "pre_oee", "post_oee", "pre_temperature", "post_temperature", "pre_vibration", "post_vibration", "verification_status", "verification_note"]].copy()
            st.dataframe(view, hide_index=True, use_container_width=True, height=260)
            if can_manage:
                labels = {int(row["id"]): f'{row["wo_code"]} · {row["machine_code"]}' for _, row in candidates.iterrows()}
                if st.session_state.get("verify_wo") not in labels:
                    st.session_state.pop("verify_wo", None)
                selected_id = st.selectbox("Etkinliği doğrula", list(labels), format_func=lambda value: labels[value], key="verify_wo")
                selected = candidates[candidates["id"] == selected_id].iloc[0]
                machine = machines[machines["machine_code"] == selected["machine_code"]].iloc[0]
                sensor = sensors[sensors["machine_code"] == selected["machine_code"]]
                sensor_row = sensor.iloc[0] if not sensor.empty else {}
                with st.form("effectiveness_form"):
                    result = st.selectbox("Sonuç", ["Etkili", "Kısmen Etkili", "Etkisiz"])
                    note = st.text_area("Doğrulama kanıtı *", placeholder="OEE, titreşim, sıcaklık veya tekrar oranındaki değişimi yazın")
                    save = st.form_submit_button("Etkinlik Sonucunu Kaydet", type="primary", use_container_width=True)
                if save:
                    if not note.strip(): st.error("Doğrulama kanıtı zorunludur.")
                    else:
                        execute("""UPDATE maintenance_work_orders SET verification_status=?,verification_note=?,post_oee=?,
                            post_temperature=?,post_vibration=? WHERE id=?""",
                            (result, note.strip(), _machine_oee(machine), float(sensor_row.get("temperature") or 0),
                             float(sensor_row.get("vibration") or 0), int(selected_id)))
                        st.success("Bakım etkinliği doğrulandı."); st.rerun()
