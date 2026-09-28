"""TREX MES kalite iş akışları: kontrol planı, görev, uygunsuzluk ve DÖF."""
from datetime import date, datetime, timedelta
import html
import uuid

import pandas as pd
import streamlit as st


GREEN = "#0b9257"
RED = "#e65258"
AMBER = "#eea629"
BLUE = "#349bc7"


def _id():
    return int(uuid.uuid4().int % 2_000_000_000) or 1


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe(value):
    return html.escape(str(value or "—"))


def _db_value(row, key, index):
    """SQLite Row ve PostgreSQL dict satırlarını ortak biçimde okur."""
    try:
        return row[key]
    except (TypeError, KeyError, IndexError):
        return row[index]


def _seed_quality_workflow_examples(connection):
    """Yeni kalite iş akışlarını anlaşılır, idempotent örneklerle doldurur."""
    machines = connection.execute(
        "SELECT id,machine_code,product FROM machines ORDER BY id LIMIT 3"
    ).fetchall()
    machine_rows = []
    for index in range(3):
        if index < len(machines):
            row = machines[index]
            machine_rows.append((
                str(_db_value(row, "machine_code", 1)),
                str(_db_value(row, "product", 2) or f"Örnek Ürün {index + 1}"),
            ))
        else:
            machine_rows.append((f"CNC-0{index + 1}", ["Mil Parçası", "Flanş", "Gövde"][index]))

    plan_definitions = [
        ("KP-DEMO-001", machine_rows[0][1], "Torna", "İlk Onay", "Dış Çap", "mm", 24.90, 25.10, 5, "İş Emri Başlangıcı", 0, machine_rows[0][0], "Dijital Kumpas"),
        ("KP-DEMO-002", machine_rows[1][1], "İşleme", "Frekansiyel", "Kalınlık", "mm", 7.95, 8.05, 3, "Üretim Adedi", 1000, machine_rows[1][0], "Mikrometre"),
        ("KP-DEMO-003", machine_rows[2][1], "Final", "Final", "Yüzey Pürüzlülüğü", "Ra", 0.00, 1.60, 4, "İş Emri Tamamlanması", 0, machine_rows[2][0], "Pürüzlülük Ölçer"),
        ("KP-DEMO-004", machine_rows[0][1], "Bakım Sonrası", "Bakım Sonrası", "Salınım", "mm", 0.00, 0.03, 3, "Bakım Tamamlanması", 0, machine_rows[0][0], "Komparatör"),
    ]
    plan_ids = {}
    for definition in plan_definitions:
        existing = connection.execute("SELECT id FROM quality_control_plans WHERE plan_code=?", (definition[0],)).fetchone()
        if existing:
            plan_ids[definition[0]] = int(_db_value(existing, "id", 0))
            continue
        plan_id = _id()
        plan_ids[definition[0]] = plan_id
        connection.execute("""INSERT INTO quality_control_plans(
            id,plan_code,product,operation_name,inspection_stage,characteristic,unit,spec_low,spec_high,
            sample_size,trigger_type,frequency_qty,machine_code,instrument,active,created_at,created_by)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (plan_id, *definition, 1, _now(), "Örnek Veri"))

    task_definitions = [
        ("KK-DEMO-001", "demo:first", "KP-DEMO-001", "WO-001", machine_rows[0][0], machine_rows[0][1], "İlk Onay", "İş emri başlangıç kontrolü", 0, "Bekliyor", None, None, "İlk 5 parçanın ölçümü bekleniyor", None, None),
        ("KK-DEMO-002", "demo:frequency", "KP-DEMO-002", "WO-002", machine_rows[1][0], machine_rows[1][1], "Frekansiyel", "1.000 adet kontrolü yaklaşıyor (950 üretildi)", 1000, "Kontrolde", None, None, "Kalite personeline atandı", None, "trex Kalite"),
        ("KK-DEMO-003", "demo:final", "KP-DEMO-003", "WO-003", machine_rows[2][0], machine_rows[2][1], "Final", "İş emri tamamlanma kontrolü", 800, "Onaylandı", "Uygun", 1.20, "Final numuneleri uygun", _now(), "trex Kalite"),
        ("KK-DEMO-004", "demo:maintenance", "KP-DEMO-004", "WO-004", machine_rows[0][0], machine_rows[0][1], "Bakım Sonrası", "Periyodik bakım tamamlandı", 0, "Reddedildi", "Uygun Değil", 0.06, "Salınım üst limitin üzerinde", _now(), "trex Kalite"),
    ]
    task_ids = {}
    for task in task_definitions:
        existing = connection.execute("SELECT id FROM quality_inspection_tasks WHERE task_code=?", (task[0],)).fetchone()
        if existing:
            task_ids[task[0]] = int(_db_value(existing, "id", 0))
            continue
        task_id = _id()
        task_ids[task[0]] = task_id
        connection.execute("""INSERT INTO quality_inspection_tasks(
            id,task_code,source_key,plan_id,work_order,machine_code,product,inspection_stage,trigger_reason,
            due_quantity,status,result,measured_value,notes,assigned_role,created_at,completed_at,inspector)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (task_id, task[0], task[1], plan_ids[task[2]], *task[3:13], "quality", _now(), task[13], task[14]))

    nc_definitions = [
        ("UYG-DEMO-001", task_ids["KK-DEMO-004"], machine_rows[0][0], machine_rows[0][1], "LOT-2609-A", "WO-004", "Salınım tolerans dışı", 5, "Karantina", "DÖF Açıldı", "Kalite Lideri", "Bakım sonrası ilk ürün kontrolünde üst limit aşıldı."),
        ("UYG-DEMO-002", None, machine_rows[1][0], machine_rows[1][1], "LOT-2609-B", "WO-002", "Yüzey çizikleri", 12, "Yeniden İşleme", "İncelemede", "Hat Sorumlusu", "Görsel kontrolde yüzey çizikleri tespit edildi."),
    ]
    nc_ids = {}
    for nc in nc_definitions:
        existing = connection.execute("SELECT id FROM quality_nonconformities WHERE nc_code=?", (nc[0],)).fetchone()
        if existing:
            nc_ids[nc[0]] = int(_db_value(existing, "id", 0))
            continue
        nc_id = _id()
        nc_ids[nc[0]] = nc_id
        connection.execute("""INSERT INTO quality_nonconformities(
            id,nc_code,inspection_task_id,machine_code,product,lot_code,work_order,defect_type,quantity,
            disposition,status,owner,description,created_at,closed_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (nc_id, *nc, _now(), None))

    analysis_row = connection.execute(
        "SELECT id FROM five_why_analyses WHERE source_type='Uygunsuzluk' AND source_id=?",
        (nc_ids["UYG-DEMO-001"],),
    ).fetchone()
    if analysis_row:
        analysis_id = int(_db_value(analysis_row, "id", 0))
    else:
        analysis_id = _id()
        connection.execute("""INSERT INTO five_why_analyses(
            id,title,event_type,source_type,source_id,machine_code,event_description,priority,status,owner,due_date,
            root_cause,containment_action,corrective_action,verification_method,created_by,created_at,completed_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (analysis_id, "Bakım sonrası salınım tolerans dışı", "Kalite Uygunsuzluğu", "Uygunsuzluk",
             nc_ids["UYG-DEMO-001"], machine_rows[0][0], "İlk ürün ölçümünde salınım 0,06 mm ölçüldü.",
             "Yüksek", "Aksiyon Açık", "Bakım Lideri", (date.today() + timedelta(days=7)).isoformat(),
             "Mil-kaplin hizalaması bakım sonrasında doğrulanmadı.", "Ürünler karantinaya alındı.",
             "Hizalama kontrol listesi ve bakım sonrası kalite onayı zorunlu hâle getirilecek.",
             "Ardışık 3 üretimde salınım ≤0,03 mm", "Örnek Veri", _now(), None))
        why_steps = [
            "Mil salınımı toleransın üzerinde kaldı.",
            "Kaplin hizalaması doğru yapılmadı.",
            "Bakım sonrası hizalama ölçümü atlandı.",
            "Bakım kontrol listesinde ölçüm adımı bulunmuyordu.",
            "Bakım ve kalite onay akışları birbirine bağlı değildi.",
        ]
        for step_no, answer in enumerate(why_steps, 1):
            connection.execute(
                "INSERT INTO five_why_steps(id,analysis_id,step_no,question,answer) VALUES(?,?,?,?,?)",
                (_id(), analysis_id, step_no, f"{step_no}. neden?", answer),
            )

    existing_capa = connection.execute("SELECT id FROM quality_capa WHERE capa_code='DOF-DEMO-001'").fetchone()
    if not existing_capa:
        connection.execute("""INSERT INTO quality_capa(
            id,capa_code,nonconformity_id,title,source_type,priority,root_cause,containment_action,corrective_action,
            preventive_action,owner,due_date,status,effectiveness_result,five_why_analysis_id,created_at,completed_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (_id(), "DOF-DEMO-001", nc_ids["UYG-DEMO-001"], "Bakım sonrası hizalama standardı", "Uygunsuzluk",
             "Yüksek", "Bakım kontrol listesinde hizalama doğrulaması bulunmaması", "Şüpheli lot karantinaya alındı",
             "Kaplin yeniden hizalanacak ve ilk ürün kalite kontrolünden geçirilecek",
             "Bakım formuna ölçüm değeri ve kalite onayı zorunluluğu eklenecek", "Bakım Lideri",
             (date.today() + timedelta(days=7)).isoformat(), "Uygulanıyor", "", analysis_id, _now(), None))


@st.cache_resource(show_spinner=False)
def ensure_quality_workflow_schema(database_identity, _connection_factory, schema_version="quality-workflow-v2-demo"):
    """Kalite kontrol planı, muayene, uygunsuzluk ve DÖF tablolarını kurar."""
    connection = _connection_factory()
    connection.execute("""
        CREATE TABLE IF NOT EXISTS quality_control_plans(
            id INTEGER PRIMARY KEY,
            plan_code TEXT UNIQUE NOT NULL,
            product TEXT NOT NULL,
            operation_name TEXT,
            inspection_stage TEXT NOT NULL,
            characteristic TEXT NOT NULL,
            unit TEXT,
            spec_low REAL,
            spec_high REAL,
            sample_size INTEGER NOT NULL,
            trigger_type TEXT NOT NULL,
            frequency_qty INTEGER DEFAULT 0,
            machine_code TEXT,
            instrument TEXT,
            active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            created_by TEXT
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS quality_inspection_tasks(
            id INTEGER PRIMARY KEY,
            task_code TEXT UNIQUE NOT NULL,
            source_key TEXT UNIQUE NOT NULL,
            plan_id INTEGER,
            work_order TEXT,
            machine_code TEXT,
            product TEXT,
            inspection_stage TEXT NOT NULL,
            trigger_reason TEXT,
            due_quantity INTEGER DEFAULT 0,
            status TEXT NOT NULL,
            result TEXT,
            measured_value REAL,
            notes TEXT,
            assigned_role TEXT DEFAULT 'quality',
            created_at TEXT NOT NULL,
            completed_at TEXT,
            inspector TEXT
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS quality_nonconformities(
            id INTEGER PRIMARY KEY,
            nc_code TEXT UNIQUE NOT NULL,
            inspection_task_id INTEGER,
            machine_code TEXT,
            product TEXT,
            lot_code TEXT,
            work_order TEXT,
            defect_type TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            disposition TEXT NOT NULL,
            status TEXT NOT NULL,
            owner TEXT,
            description TEXT,
            created_at TEXT NOT NULL,
            closed_at TEXT
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS quality_capa(
            id INTEGER PRIMARY KEY,
            capa_code TEXT UNIQUE NOT NULL,
            nonconformity_id INTEGER,
            title TEXT NOT NULL,
            source_type TEXT NOT NULL,
            priority TEXT NOT NULL,
            root_cause TEXT,
            containment_action TEXT,
            corrective_action TEXT NOT NULL,
            preventive_action TEXT,
            owner TEXT,
            due_date TEXT,
            status TEXT NOT NULL,
            effectiveness_result TEXT,
            five_why_analysis_id INTEGER,
            created_at TEXT NOT NULL,
            completed_at TEXT
        )
    """)
    connection.execute("CREATE INDEX IF NOT EXISTS idx_quality_plans_product ON quality_control_plans(product,active)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_quality_tasks_status ON quality_inspection_tasks(status,created_at)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_quality_tasks_machine ON quality_inspection_tasks(machine_code,work_order)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_quality_nc_status ON quality_nonconformities(status,created_at)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_quality_capa_status ON quality_capa(status,due_date)")
    _seed_quality_workflow_examples(connection)
    connection.commit()
    connection.close()
    return True


def sync_inspection_tasks(query, execute, notifier=None):
    """Aktif iş emirlerinden ilk onay, frekans ve final kontrol görevlerini üretir."""
    plans = query("SELECT * FROM quality_control_plans WHERE active=1 ORDER BY id")
    if plans.empty:
        return 0
    orders = query("""
        SELECT order_no,machine_code,product,target,produced,status
        FROM work_orders WHERE status!='Tamamlandı' OR produced>=target ORDER BY id DESC
    """)
    maintenance = query("""
        SELECT id,machine_code,maintenance_type,maintenance_date,status
        FROM maintenance WHERE status='Tamamlandı' ORDER BY id DESC
    """)
    created = 0
    for _, plan in plans.iterrows():
        matches = orders[orders["product"].astype(str).str.casefold() == str(plan["product"]).casefold()]
        if str(plan.get("machine_code") or "").strip():
            matches = matches[matches["machine_code"] == plan["machine_code"]]
        for _, order in matches.head(10).iterrows():
            produced = int(order.get("produced") or 0)
            target = max(int(order.get("target") or 0), 1)
            stage = str(plan["inspection_stage"])
            trigger = str(plan["trigger_type"])
            threshold = 0
            due = False
            reason = trigger
            if stage == "İlk Onay" or trigger == "İş Emri Başlangıcı":
                due = produced > 0
                threshold = 0
            elif stage == "Frekansiyel" or trigger == "Üretim Adedi":
                frequency = max(int(plan.get("frequency_qty") or 0), 1)
                completed_threshold = (produced // frequency) * frequency
                if produced > 0 and produced % frequency == 0:
                    threshold = produced
                    due = True
                else:
                    threshold = completed_threshold + frequency
                    warning_margin = max(int(frequency * .05), 1)
                    due = produced >= threshold - warning_margin
                reason = f"{threshold:,} adet kontrolü" + (f" yaklaşıyor ({produced:,} üretildi)" if produced < threshold else "")
            elif stage == "Final" or trigger == "İş Emri Tamamlanması":
                due = produced >= target
                threshold = target
            if not due:
                continue
            source_key = f'{int(plan["id"])}:{order["order_no"]}:{stage}:{threshold}'
            if not query("SELECT id FROM quality_inspection_tasks WHERE source_key=?", (source_key,)).empty:
                continue
            task_id = _id()
            task_code = f"KK-{datetime.now():%y%m%d}-{task_id % 100000:05d}"
            execute("""INSERT INTO quality_inspection_tasks(
                id,task_code,source_key,plan_id,work_order,machine_code,product,inspection_stage,
                trigger_reason,due_quantity,status,result,assigned_role,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (task_id, task_code, source_key, int(plan["id"]), str(order["order_no"]),
                 str(order["machine_code"]), str(order["product"]), stage, reason, threshold,
                 "Bekliyor", None, "quality", _now()))
            created += 1
            if notifier is not None:
                notifier(
                    recipient_role="quality", notification_type="Kalite Görevi", priority="Uyarı",
                    title=f'{order["machine_code"]} · {stage}',
                    message=f'{order["order_no"]} / {order["product"]} için {reason.lower()}',
                    machine_code=str(order["machine_code"]), target_module="✅ Kalite",
                    entity_type="quality_inspection", entity_id=task_id,
                )
    maintenance_plans = plans[(plans["inspection_stage"] == "Bakım Sonrası") | (plans["trigger_type"] == "Bakım Tamamlanması")]
    for _, plan in maintenance_plans.iterrows():
        machine_maintenance = maintenance.copy()
        if str(plan.get("machine_code") or "").strip():
            machine_maintenance = machine_maintenance[machine_maintenance["machine_code"] == plan["machine_code"]]
        for _, event in machine_maintenance.head(10).iterrows():
            source_key = f'{int(plan["id"])}:maintenance:{int(event["id"])}'
            if not query("SELECT id FROM quality_inspection_tasks WHERE source_key=?", (source_key,)).empty:
                continue
            task_id = _id()
            task_code = f"KK-{datetime.now():%y%m%d}-{task_id % 100000:05d}"
            execute("""INSERT INTO quality_inspection_tasks(
                id,task_code,source_key,plan_id,work_order,machine_code,product,inspection_stage,
                trigger_reason,due_quantity,status,result,assigned_role,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (task_id, task_code, source_key, int(plan["id"]), "", str(event["machine_code"]),
                 str(plan["product"]), "Bakım Sonrası", f'{event["maintenance_type"]} tamamlandı', 0,
                 "Bekliyor", None, "quality", _now()))
            created += 1
            if notifier is not None:
                notifier(
                    recipient_role="quality", notification_type="Bakım Sonrası Kontrol", priority="Uyarı",
                    title=f'{event["machine_code"]} kalite onayı bekliyor',
                    message=f'{event["maintenance_type"]} sonrası ilk ürün kontrolü gerekli',
                    machine_code=str(event["machine_code"]), target_module="✅ Kalite",
                    entity_type="quality_inspection", entity_id=task_id,
                )
    return created


def quality_workflow_metrics(query):
    plans = query("SELECT COUNT(*) AS total FROM quality_control_plans WHERE active=1")
    tasks = query("SELECT status,COUNT(*) AS total FROM quality_inspection_tasks GROUP BY status")
    nonconformities = query("SELECT status,COUNT(*) AS total FROM quality_nonconformities GROUP BY status")
    capas = query("SELECT status,due_date FROM quality_capa")
    task_map = dict(zip(tasks.get("status", []), tasks.get("total", []))) if not tasks.empty else {}
    nc_open = int(nonconformities.loc[nonconformities["status"] != "Kapalı", "total"].sum()) if not nonconformities.empty else 0
    overdue = 0
    if not capas.empty:
        due = pd.to_datetime(capas["due_date"], errors="coerce")
        overdue = int(((capas["status"] != "Tamamlandı") & due.notna() & (due.dt.date < date.today())).sum())
    return int(plans.iloc[0]["total"] if not plans.empty else 0), int(task_map.get("Bekliyor", 0)), nc_open, overdue


def _status_pill(value):
    colours = {"Bekliyor": AMBER, "Kontrolde": BLUE, "Onaylandı": GREEN, "Reddedildi": RED,
               "Açık": RED, "Karantinada": AMBER, "Tamamlandı": GREEN, "Kapalı": GREEN}
    colour = colours.get(str(value), "#7d9488")
    return f'<span class="qwf-pill" style="--pill:{colour}">{_safe(value)}</span>'


def render_control_plans(query, execute, machines, current_user="", can_manage=False):
    plans = query("SELECT * FROM quality_control_plans ORDER BY active DESC,id DESC")
    st.markdown("#### Kontrol Planları")
    st.caption("Ürün ve operasyon bazında neyin, ne zaman, kaç numuneyle ve hangi toleranslarda kontrol edileceğini tanımlayın.")
    if can_manage:
        with st.expander("Yeni kontrol planı", expanded=plans.empty):
            with st.form("quality_control_plan_form", clear_on_submit=True):
                a, b, c, d = st.columns(4)
                product = a.text_input("Ürün *", placeholder="Mil Parçası")
                operation = b.text_input("Operasyon", placeholder="Torna / Taşlama")
                machine_options = ["Tümü"] + (machines["machine_code"].dropna().astype(str).tolist() if not machines.empty else [])
                machine = c.selectbox("Makine", machine_options)
                stage = d.selectbox("Kontrol aşaması", ["İlk Onay", "Frekansiyel", "Final", "Bakım Sonrası"])
                e, f, g, h = st.columns(4)
                characteristic = e.text_input("Karakteristik *", placeholder="Çap")
                unit = f.text_input("Birim", value="mm")
                spec_low = g.number_input("Alt tolerans", value=24.9000, format="%.4f")
                spec_high = h.number_input("Üst tolerans", value=25.1000, format="%.4f")
                i, j, k, l = st.columns(4)
                sample_size = i.number_input("Numune", min_value=1, value=5)
                trigger_default = {"İlk Onay": "İş Emri Başlangıcı", "Frekansiyel": "Üretim Adedi", "Final": "İş Emri Tamamlanması", "Bakım Sonrası": "Bakım Tamamlanması"}[stage]
                trigger = j.selectbox("Tetikleyici", ["İş Emri Başlangıcı", "Üretim Adedi", "İş Emri Tamamlanması", "Bakım Tamamlanması"], index=["İş Emri Başlangıcı", "Üretim Adedi", "İş Emri Tamamlanması", "Bakım Tamamlanması"].index(trigger_default))
                frequency = k.number_input("Kontrol sıklığı (adet)", min_value=0, value=1000 if stage == "Frekansiyel" else 0, step=100)
                instrument = l.text_input("Ölçüm cihazı", placeholder="Kumpas")
                save = st.form_submit_button("Kontrol Planını Kaydet", type="primary", use_container_width=True)
            if save:
                if not product.strip() or not characteristic.strip() or spec_low >= spec_high:
                    st.error("Ürün, karakteristik ve geçerli tolerans aralığını girin.")
                else:
                    plan_id = _id()
                    execute("""INSERT INTO quality_control_plans(id,plan_code,product,operation_name,inspection_stage,
                        characteristic,unit,spec_low,spec_high,sample_size,trigger_type,frequency_qty,machine_code,
                        instrument,active,created_at,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (plan_id, f"KP-{plan_id % 100000:05d}", product.strip(), operation.strip(), stage,
                         characteristic.strip(), unit.strip(), float(spec_low), float(spec_high), int(sample_size),
                         trigger, int(frequency), None if machine == "Tümü" else machine, instrument.strip(), 1,
                         _now(), current_user))
                    st.success("Kontrol planı oluşturuldu.")
                    st.rerun()
    if plans.empty:
        st.info("Henüz kontrol planı yok. İlk ürün, frekansiyel veya final kontrol planı oluşturun.")
        return
    display = plans[["plan_code", "product", "operation_name", "machine_code", "inspection_stage", "characteristic", "spec_low", "spec_high", "unit", "sample_size", "trigger_type", "frequency_qty", "instrument", "active"]].copy()
    display.columns = ["Plan", "Ürün", "Operasyon", "Makine", "Aşama", "Karakteristik", "Alt", "Üst", "Birim", "Numune", "Tetikleyici", "Sıklık", "Cihaz", "Aktif"]
    display["Aktif"] = display["Aktif"].map({1: "Evet", 0: "Hayır"})
    st.dataframe(display, hide_index=True, use_container_width=True, height=330)
    if can_manage:
        with st.expander("Plan durumunu değiştir"):
            plan_labels = {int(row["id"]): f'{row["plan_code"]} · {row["product"]} · {row["inspection_stage"]}' for _, row in plans.iterrows()}
            selected = st.selectbox("Plan", list(plan_labels), format_func=lambda value: plan_labels[value], key="quality_plan_toggle")
            current = plans[plans["id"] == selected].iloc[0]
            new_state = st.toggle("Aktif", value=bool(current["active"]), key="quality_plan_active")
            if st.button("Plan Durumunu Kaydet", use_container_width=True):
                execute("UPDATE quality_control_plans SET active=? WHERE id=?", (1 if new_state else 0, int(selected)))
                st.rerun()


def render_inspection_tasks(query, execute, current_user="", can_manage=False, notifier=None):
    # Kalite ekranı açıldığında planların üretim ve bakım tetikleyicilerini otomatik değerlendir.
    sync_inspection_tasks(query, execute, notifier)
    header_a, header_b = st.columns([4, 1])
    header_a.markdown("#### Kalite Kontrol Görevleri")
    if header_b.button("Görevleri Yenile", use_container_width=True, key="quality_sync_tasks"):
        count = sync_inspection_tasks(query, execute, notifier)
        st.success(f"{count} yeni kontrol görevi oluşturuldu." if count else "Yeni görev gerektiren üretim bulunmadı.")
        if count:
            st.rerun()
    tasks = query("""SELECT t.*,p.characteristic,p.unit,p.spec_low,p.spec_high,p.sample_size,p.instrument
                     FROM quality_inspection_tasks t LEFT JOIN quality_control_plans p ON p.id=t.plan_id
                     ORDER BY CASE t.status WHEN 'Bekliyor' THEN 1 WHEN 'Kontrolde' THEN 2 ELSE 3 END,t.id DESC""")
    if tasks.empty:
        st.info("Aktif üretimler için henüz kontrol görevi oluşmadı. Önce kontrol planı tanımlayın.")
        return
    filters = st.columns(3)
    status_filter = filters[0].selectbox("Görev durumu", ["Tümü", "Bekliyor", "Kontrolde", "Onaylandı", "Reddedildi"], key="quality_task_status")
    machine_filter = filters[1].selectbox("Görev makinesi", ["Tümü"] + sorted(tasks["machine_code"].dropna().astype(str).unique().tolist()), key="quality_task_machine")
    stage_filter = filters[2].selectbox("Kontrol aşaması", ["Tümü"] + sorted(tasks["inspection_stage"].dropna().astype(str).unique().tolist()), key="quality_task_stage")
    view = tasks.copy()
    if status_filter != "Tümü": view = view[view["status"] == status_filter]
    if machine_filter != "Tümü": view = view[view["machine_code"] == machine_filter]
    if stage_filter != "Tümü": view = view[view["inspection_stage"] == stage_filter]
    summary = view[["task_code", "machine_code", "work_order", "product", "inspection_stage", "trigger_reason", "characteristic", "due_quantity", "status", "result", "inspector", "created_at"]].copy()
    summary.columns = ["Görev", "Makine", "İş Emri", "Ürün", "Aşama", "Tetikleyici", "Kontrol", "Eşik", "Durum", "Sonuç", "Kontrol Eden", "Oluşturma"]
    st.dataframe(summary, hide_index=True, use_container_width=True, height=300)
    if not can_manage or view.empty:
        return
    labels = {int(row["id"]): f'{row["task_code"]} · {row["machine_code"]} · {row["inspection_stage"]}' for _, row in view.iterrows()}
    selected_id = st.selectbox("Kontrolü gerçekleştir", list(labels), format_func=lambda value: labels[value], key="quality_selected_task")
    task = view[view["id"] == selected_id].iloc[0]
    with st.container(border=True):
        st.markdown(f'**{_safe(task["characteristic"])}:** {_safe(task["spec_low"])} – {_safe(task["spec_high"])} {_safe(task["unit"])} · Numune: {int(task.get("sample_size") or 1)} · Cihaz: {_safe(task["instrument"])}')
        with st.form("quality_task_complete_form"):
            a, b, c = st.columns(3)
            measured_existing = task.get("measured_value")
            spec_low_value, spec_high_value = task.get("spec_low"), task.get("spec_high")
            if pd.notna(measured_existing):
                default_measurement = float(measured_existing)
            elif pd.notna(spec_low_value) and pd.notna(spec_high_value):
                default_measurement = (float(spec_low_value) + float(spec_high_value)) / 2
            elif pd.notna(spec_low_value):
                default_measurement = float(spec_low_value)
            else:
                default_measurement = 0.0
            measured = a.number_input("Ölçüm değeri", value=default_measurement, format="%.4f")
            result = b.selectbox("Kontrol sonucu", ["Uygun", "Uygun Değil"])
            defect_quantity = c.number_input("Uygunsuz miktar", min_value=0, value=1 if result == "Uygun Değil" else 0)
            notes = st.text_area("Kontrol notu", placeholder="Numune, görsel kontrol veya sapma açıklaması")
            complete = st.form_submit_button("Kontrolü Tamamla", type="primary", use_container_width=True)
        if complete:
            final_result = result
            if pd.notna(task.get("spec_low")) and measured < float(task["spec_low"]): final_result = "Uygun Değil"
            if pd.notna(task.get("spec_high")) and measured > float(task["spec_high"]): final_result = "Uygun Değil"
            status = "Onaylandı" if final_result == "Uygun" else "Reddedildi"
            execute("UPDATE quality_inspection_tasks SET status=?,result=?,measured_value=?,notes=?,completed_at=?,inspector=? WHERE id=?",
                    (status, final_result, float(measured), notes.strip(), _now(), current_user, int(selected_id)))
            if final_result == "Uygun Değil":
                nc_id = _id()
                execute("""INSERT INTO quality_nonconformities(id,nc_code,inspection_task_id,machine_code,product,lot_code,
                    work_order,defect_type,quantity,disposition,status,owner,description,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (nc_id, f"UYG-{nc_id % 100000:05d}", int(selected_id), task["machine_code"], task["product"],
                     "", task["work_order"], f'{task["characteristic"]} tolerans dışı', max(int(defect_quantity), 1),
                     "Karantina", "Açık", current_user, notes.strip(), _now()))
            st.success("Kontrol tamamlandı." + (" Uygunsuzluk kaydı otomatik açıldı." if final_result == "Uygun Değil" else " Ürün onaylandı."))
            st.rerun()


def render_nonconformities(query, execute, current_user="", can_manage=False):
    records = query("SELECT * FROM quality_nonconformities ORDER BY id DESC")
    st.markdown("#### Uygunsuzluk ve Karantina")
    st.caption("Uygun olmayan ürünü lot ve iş emriyle izleyin; serbest bırakma, yeniden işleme veya hurda kararını kayıt altına alın.")
    if can_manage:
        with st.expander("Manuel uygunsuzluk kaydı"):
            with st.form("quality_nc_form", clear_on_submit=True):
                a, b, c, d = st.columns(4)
                machine = a.text_input("Makine")
                product = b.text_input("Ürün *")
                work_order = c.text_input("İş emri")
                lot = d.text_input("Lot / parti")
                e, f, g = st.columns(3)
                defect = e.text_input("Uygunsuzluk *")
                quantity = f.number_input("Miktar", min_value=1, value=1)
                disposition = g.selectbox("İlk karar", ["Karantina", "Yeniden İşleme", "Hurda", "Şartlı Kabul"])
                description = st.text_area("Açıklama")
                add = st.form_submit_button("Uygunsuzluğu Kaydet", type="primary", use_container_width=True)
            if add:
                if not product.strip() or not defect.strip():
                    st.error("Ürün ve uygunsuzluk alanları zorunludur.")
                else:
                    nc_id = _id()
                    execute("""INSERT INTO quality_nonconformities(id,nc_code,inspection_task_id,machine_code,product,lot_code,
                        work_order,defect_type,quantity,disposition,status,owner,description,created_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (nc_id, f"UYG-{nc_id % 100000:05d}", None, machine.strip(), product.strip(), lot.strip(),
                         work_order.strip(), defect.strip(), int(quantity), disposition, "Açık", current_user,
                         description.strip(), _now()))
                    st.success("Uygunsuzluk karantinaya alındı.")
                    st.rerun()
    if records.empty:
        st.success("Açık uygunsuzluk bulunmuyor.")
        return
    display = records[["nc_code", "machine_code", "product", "lot_code", "work_order", "defect_type", "quantity", "disposition", "status", "owner", "created_at"]].copy()
    display.columns = ["Kayıt", "Makine", "Ürün", "Lot", "İş Emri", "Uygunsuzluk", "Miktar", "Karar", "Durum", "Sorumlu", "Tarih"]
    st.dataframe(display, hide_index=True, use_container_width=True, height=300)
    if can_manage:
        labels = {int(row["id"]): f'{row["nc_code"]} · {row["product"]} · {row["defect_type"]}' for _, row in records.iterrows()}
        selected_id = st.selectbox("Uygunsuzluk kararı", list(labels), format_func=lambda value: labels[value], key="quality_selected_nc")
        selected = records[records["id"] == selected_id].iloc[0]
        with st.form("quality_nc_update_form"):
            a, b, c = st.columns(3)
            disposition = a.selectbox("Karar", ["Karantina", "Yeniden İşleme", "Hurda", "Şartlı Kabul", "Serbest Bırakıldı"], index=0)
            status = b.selectbox("Durum", ["Açık", "İncelemede", "DÖF Açıldı", "Kapalı"])
            owner = c.text_input("Sorumlu", value=str(selected.get("owner") or current_user))
            update = st.form_submit_button("Kararı Kaydet", use_container_width=True)
        if update:
            execute("UPDATE quality_nonconformities SET disposition=?,status=?,owner=?,closed_at=? WHERE id=?",
                    (disposition, status, owner.strip(), _now() if status == "Kapalı" else None, int(selected_id)))
            st.rerun()


def render_capa(query, execute, current_user="", can_manage=False):
    capas = query("SELECT c.*,n.nc_code,n.machine_code,n.product,n.defect_type FROM quality_capa c LEFT JOIN quality_nonconformities n ON n.id=c.nonconformity_id ORDER BY c.id DESC")
    nonconformities = query("SELECT * FROM quality_nonconformities WHERE status!='Kapalı' ORDER BY id DESC")
    st.markdown("#### DÖF · Düzeltici ve Önleyici Faaliyet")
    st.caption("Kritik uygunsuzlukları kök neden, kalıcı aksiyon, sorumlu, termin ve etkinlik doğrulamasıyla kapatın.")
    if can_manage and not nonconformities.empty:
        with st.expander("Yeni DÖF ve 5 Why analizi", expanded=capas.empty):
            nc_labels = {int(row["id"]): f'{row["nc_code"]} · {row["machine_code"]} · {row["defect_type"]}' for _, row in nonconformities.iterrows()}
            nc_id = st.selectbox("Kaynak uygunsuzluk", list(nc_labels), format_func=lambda value: nc_labels[value], key="capa_nc")
            nc = nonconformities[nonconformities["id"] == nc_id].iloc[0]
            with st.form("quality_capa_form", clear_on_submit=True):
                a, b, c = st.columns([1.5, 1, 1])
                title = a.text_input("DÖF başlığı *", value=f'{nc["machine_code"]} · {nc["defect_type"]}')
                priority = b.selectbox("Öncelik", ["Kritik", "Yüksek", "Normal", "Düşük"], index=1)
                due_date = c.date_input("Termin", value=date.today() + timedelta(days=7))
                st.markdown("##### 5 Why neden zinciri")
                why_answers = [st.text_input(f"{index}. Neden", key=f"quality_capa_why_{index}") for index in range(1, 6)]
                d, e = st.columns(2)
                root_cause = d.text_area("Doğrulanmış kök neden *")
                containment = e.text_area("Geçici önlem")
                corrective = d.text_area("Düzeltici faaliyet *")
                preventive = e.text_area("Önleyici faaliyet")
                owner = st.text_input("Sorumlu", value=current_user)
                save = st.form_submit_button("DÖF ve 5 Why Kaydını Oluştur", type="primary", use_container_width=True)
            if save:
                filled = [answer.strip() for answer in why_answers if answer.strip()]
                if not title.strip() or not root_cause.strip() or not corrective.strip() or len(filled) < 2:
                    st.error("Başlık, en az iki neden, kök neden ve düzeltici faaliyet zorunludur.")
                else:
                    analysis_id = _id()
                    execute("""INSERT INTO five_why_analyses(id,title,event_type,source_type,source_id,machine_code,event_description,
                        priority,status,owner,due_date,root_cause,containment_action,corrective_action,verification_method,
                        created_by,created_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (analysis_id, title.strip(), "Kalite Uygunsuzluğu", "Uygunsuzluk", int(nc_id), nc["machine_code"],
                         nc["description"] or nc["defect_type"], priority, "Aksiyon Açık", owner.strip(), due_date.isoformat(),
                         root_cause.strip(), containment.strip(), corrective.strip(), "Tekrar ve hata oranı doğrulaması",
                         current_user, _now(), None))
                    prompts = ["Problem neden oluştu?", "Bu durum neden gerçekleşti?", "Önceki neden neden ortaya çıktı?", "Sistem bunu neden önleyemedi?", "Kök neden neden kalıcı hâle geldi?"]
                    for step, answer in enumerate(why_answers, 1):
                        if answer.strip():
                            execute("INSERT INTO five_why_steps(id,analysis_id,step_no,question,answer) VALUES(?,?,?,?,?)",
                                    (_id(), analysis_id, step, prompts[step - 1], answer.strip()))
                    capa_id = _id()
                    execute("""INSERT INTO quality_capa(id,capa_code,nonconformity_id,title,source_type,priority,root_cause,
                        containment_action,corrective_action,preventive_action,owner,due_date,status,five_why_analysis_id,created_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (capa_id, f"DOF-{capa_id % 100000:05d}", int(nc_id), title.strip(), "Uygunsuzluk", priority,
                         root_cause.strip(), containment.strip(), corrective.strip(), preventive.strip(), owner.strip(),
                         due_date.isoformat(), "Açık", analysis_id, _now()))
                    execute("UPDATE quality_nonconformities SET status='DÖF Açıldı',owner=? WHERE id=?", (owner.strip(), int(nc_id)))
                    st.success("DÖF açıldı ve 5 Why analizi Akıllı Analiz modülüne bağlandı.")
                    st.rerun()
    if capas.empty:
        st.info("Henüz DÖF kaydı yok.")
        return
    kpis = st.columns(4)
    due = pd.to_datetime(capas["due_date"], errors="coerce")
    kpis[0].metric("Toplam DÖF", len(capas))
    kpis[1].metric("Açık", int((capas["status"] != "Tamamlandı").sum()))
    kpis[2].metric("Geciken", int(((capas["status"] != "Tamamlandı") & due.notna() & (due.dt.date < date.today())).sum()))
    kpis[3].metric("Tamamlanan", int((capas["status"] == "Tamamlandı").sum()))
    display = capas[["capa_code", "nc_code", "machine_code", "product", "title", "priority", "owner", "due_date", "status", "five_why_analysis_id"]].copy()
    display.columns = ["DÖF", "Uygunsuzluk", "Makine", "Ürün", "Başlık", "Öncelik", "Sorumlu", "Termin", "Durum", "5 Why"]
    st.dataframe(display, hide_index=True, use_container_width=True, height=280)
    if can_manage:
        labels = {int(row["id"]): f'{row["capa_code"]} · {row["title"]}' for _, row in capas.iterrows()}
        selected_id = st.selectbox("DÖF güncelle", list(labels), format_func=lambda value: labels[value], key="quality_selected_capa")
        selected = capas[capas["id"] == selected_id].iloc[0]
        with st.form("quality_capa_update_form"):
            a, b = st.columns(2)
            status = a.selectbox("Durum", ["Açık", "Uygulanıyor", "Doğrulama", "Tamamlandı"], index=["Açık", "Uygulanıyor", "Doğrulama", "Tamamlandı"].index(selected["status"]) if selected["status"] in ["Açık", "Uygulanıyor", "Doğrulama", "Tamamlandı"] else 0)
            effectiveness = b.text_input("Etkinlik sonucu", value=str(selected.get("effectiveness_result") or ""), placeholder="Hata tekrarlanmadı / oran %... azaldı")
            update = st.form_submit_button("DÖF Durumunu Kaydet", use_container_width=True)
        if update:
            if status == "Tamamlandı" and not effectiveness.strip():
                st.error("DÖF kapatılırken etkinlik doğrulama sonucu zorunludur.")
            else:
                completed = _now() if status == "Tamamlandı" else None
                execute("UPDATE quality_capa SET status=?,effectiveness_result=?,completed_at=? WHERE id=?", (status, effectiveness.strip(), completed, int(selected_id)))
                why_status = {"Açık": "Aksiyon Açık", "Uygulanıyor": "Aksiyon Açık", "Doğrulama": "Doğrulama", "Tamamlandı": "Tamamlandı"}[status]
                execute("UPDATE five_why_analyses SET status=?,completed_at=? WHERE id=?", (why_status, completed, int(selected["five_why_analysis_id"])))
                if status == "Tamamlandı":
                    execute("UPDATE quality_nonconformities SET status='Kapalı',closed_at=? WHERE id=?", (_now(), int(selected["nonconformity_id"])))
                st.rerun()
