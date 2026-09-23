"""TREX MES modüller arası operasyon aksiyon merkezi."""
from datetime import date, datetime, timedelta
import html
import uuid

import pandas as pd
import plotly.graph_objects as go
import streamlit as st


OPEN_STATUSES = ["Açık", "Atandı", "Müdahale", "İnceleniyor", "Planlandı", "Doğrulama"]
ALL_STATUSES = OPEN_STATUSES + ["Tamamlandı", "İptal"]
PRIORITY_ORDER = {"Kritik": 0, "Yüksek": 1, "Normal": 2, "Düşük": 3}
PRIORITY_COLOURS = {"Kritik": "#ef4249", "Yüksek": "#f2a01a", "Normal": "#15945a", "Düşük": "#4c95d7"}
STATUS_COLOURS = {"Açık": "#ef4249", "Atandı": "#4c95d7", "Müdahale": "#2688e5", "İnceleniyor": "#e99b19", "Planlandı": "#667f91", "Doğrulama": "#8065d6", "Tamamlandı": "#15945a", "İptal": "#87958e"}


def _uid():
    return int(uuid.uuid4().int % 2_000_000_000) or 1


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe(value):
    return html.escape(str(value or "—"))


def _new_action(source_key, source_type, source_id, machine, title, description, priority,
                owner_username="", owner_name="", due_at=None, estimated_loss=0, created_by="Sistem"):
    action_id = _uid()
    return (
        action_id, f"AC-{action_id % 100000:05d}", source_key, source_type, int(source_id or 0),
        machine or "", title, description or "", priority, "Açık", owner_username or "",
        owner_name or "", due_at or (datetime.now() + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S"),
        float(estimated_loss or 0), created_by, _now(), None, None, "",
    )


def _sync_source_actions(query, execute, users):
    """Açık operasyon kayıtlarını bir kez aksiyona dönüştürür; kapanan kayıtları yeniden açmaz."""
    existing = query("SELECT source_key FROM operational_actions")
    existing_keys = set(existing["source_key"].astype(str)) if not existing.empty else set()
    user_rows = users.to_dict("records") if not users.empty else []

    def assignee(role, preferred_name=""):
        if preferred_name:
            for user in user_rows:
                if str(user.get("full_name") or "") == str(preferred_name):
                    return str(user.get("username") or ""), str(user.get("full_name") or user.get("username") or "")
        for user in user_rows:
            if user.get("role") == role:
                return str(user.get("username") or ""), str(user.get("full_name") or user.get("username") or "")
        return "", "Atanmadı"

    pending = []
    alarms = query("SELECT id,machine_code,alarm,level,time FROM alarms WHERE acknowledged=0 ORDER BY id DESC LIMIT 100")
    for _, row in alarms.iterrows():
        key = f"Alarm:{int(row['id'])}"
        if key in existing_keys: continue
        username, name = assignee("maintenance")
        priority = "Kritik" if str(row["level"]) == "Kritik" else "Yüksek"
        pending.append(_new_action(key, "Alarm", row["id"], row["machine_code"], str(row["alarm"]),
                                   f'{row["level"]} seviye alarm · {row["time"]}', priority, username, name,
                                   (datetime.now() + timedelta(hours=2 if priority == "Kritik" else 6)).strftime("%Y-%m-%d %H:%M:%S"),
                                   5000 if priority == "Kritik" else 1800))

    requests = query("SELECT id,machine_code,request_type,description,priority,assigned_to,requested_at FROM maintenance_requests WHERE status NOT IN ('Tamamlandı','İptal') ORDER BY id DESC LIMIT 100")
    for _, row in requests.iterrows():
        key = f"Bakım:{int(row['id'])}"
        if key in existing_keys: continue
        username, name = assignee("maintenance", row.get("assigned_to"))
        priority = str(row.get("priority") or "Normal")
        if priority not in PRIORITY_ORDER: priority = "Normal"
        pending.append(_new_action(key, "Bakım", row["id"], row["machine_code"], str(row["request_type"]),
                                   str(row.get("description") or "Bakım talebi"), priority, username, name,
                                   (datetime.now() + timedelta(days=1 if priority in ("Kritik", "Yüksek") else 3)).strftime("%Y-%m-%d %H:%M:%S"), 3000))

    five_whys = query("SELECT id,title,machine_code,priority,owner,due_date,root_cause FROM five_why_analyses WHERE status!='Tamamlandı' ORDER BY id DESC LIMIT 100")
    for _, row in five_whys.iterrows():
        key = f"5Why:{int(row['id'])}"
        if key in existing_keys: continue
        username, name = assignee("quality", row.get("owner"))
        priority = str(row.get("priority") or "Normal")
        if priority not in PRIORITY_ORDER: priority = "Normal"
        due_at = str(row.get("due_date") or "") + " 17:00:00" if row.get("due_date") else None
        pending.append(_new_action(key, "5 Why", row["id"], row["machine_code"], str(row["title"]),
                                   f'Kök neden: {row.get("root_cause") or "Analiz ediliyor"}', priority, username, name, due_at, 1200))

    quality = query("SELECT id,machine_code,product,defective,defect_reason,timestamp FROM quality WHERE COALESCE(defective,0)>0 ORDER BY id DESC LIMIT 30")
    for _, row in quality.iterrows():
        key = f"Kalite:{int(row['id'])}"
        if key in existing_keys: continue
        username, name = assignee("quality")
        defective = int(row.get("defective") or 0)
        priority = "Kritik" if defective >= 25 else ("Yüksek" if defective >= 10 else "Normal")
        pending.append(_new_action(key, "Kalite", row["id"], row["machine_code"], str(row.get("defect_reason") or "Kalite uygunsuzluğu"),
                                   f'{row.get("product") or "Ürün"} · {defective} hatalı adet', priority, username, name,
                                   (datetime.now() + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S"), defective * 120))

    orders = query("SELECT id,order_no,machine_code,product,target,produced,due_date,status FROM work_orders WHERE status!='Tamamlandı' ORDER BY id DESC LIMIT 100")
    today = date.today()
    for _, row in orders.iterrows():
        due = pd.to_datetime(row.get("due_date"), errors="coerce")
        if pd.isna(due) or due.date() >= today: continue
        key = f"İş Emri:{int(row['id'])}"
        if key in existing_keys: continue
        username, name = assignee("operator")
        remaining = max(float(row.get("target") or 0) - float(row.get("produced") or 0), 0)
        pending.append(_new_action(key, "İş Emri", row["id"], row["machine_code"], f'{row["order_no"]} gecikti',
                                   f'{row.get("product") or "Ürün"} · kalan {remaining:.0f} adet', "Yüksek", username, name,
                                   datetime.now().strftime("%Y-%m-%d %H:%M:%S"), remaining * 25))

    if pending:
        placeholders = ",".join(["(" + ",".join(["?"] * 19) + ")"] * len(pending))
        params = tuple(value for row in pending for value in row)
        execute("""INSERT INTO operational_actions(
            id,action_no,source_key,source_type,source_id,machine_code,title,description,
            priority,status,owner_username,owner_name,due_at,estimated_loss,created_by,
            created_at,started_at,completed_at,verification_note
        ) VALUES """ + placeholders + " ON CONFLICT(source_key) DO NOTHING", params)


def _metrics(actions):
    if actions.empty: return 0, 0, 0, 0, 0.0, 100.0
    due = pd.to_datetime(actions["due_at"], errors="coerce")
    created = pd.to_datetime(actions["created_at"], errors="coerce")
    completed_at = pd.to_datetime(actions["completed_at"], errors="coerce")
    open_mask = actions["status"].isin(OPEN_STATUSES)
    completed_mask = actions["status"] == "Tamamlandı"
    critical = int((open_mask & (actions["priority"] == "Kritik")).sum())
    overdue = int((open_mask & due.notna() & (due < pd.Timestamp.now())).sum())
    completed_today = int((completed_mask & completed_at.notna() & (completed_at.dt.date == date.today())).sum())
    durations = (completed_at - created).dt.total_seconds().div(3600)
    avg_resolution = float(durations[completed_mask & durations.notna()].mean()) if (completed_mask & durations.notna()).any() else 0.0
    completed_total = int(completed_mask.sum())
    on_time = int((completed_mask & completed_at.notna() & due.notna() & (completed_at <= due)).sum())
    on_time_rate = on_time / completed_total * 100 if completed_total else 100.0
    return int(open_mask.sum()), critical, overdue, completed_today, avg_resolution, on_time_rate


def render_action_center(query, execute, *, current_username="", current_name="", current_role="operator", notifier=None):
    users = query("SELECT username,full_name,role FROM users WHERE is_active=1 ORDER BY full_name")
    _sync_source_actions(query, execute, users)
    actions = query("SELECT * FROM operational_actions ORDER BY id DESC")
    machines = query("SELECT machine_code FROM machines ORDER BY machine_code")
    handovers = query("SELECT * FROM shift_handover_notes ORDER BY id DESC LIMIT 20")

    st.markdown("""
    <style>
      .ac-hero{display:flex;justify-content:space-between;align-items:center;margin:-2px 0 8px}.ac-hero h2{font-size:1.18rem!important;color:#102f3d!important;margin:0!important}.ac-hero p{font-size:.67rem;color:#718493;margin:2px 0 0}.ac-live{background:#e5f7ed;color:#087846;border-radius:99px;padding:5px 9px;font-size:.58rem;font-weight:850}
      .ac-kpi{height:88px;box-sizing:border-box;border:1px solid #d8e9e1;border-radius:9px;background:#fff;padding:10px 11px;display:grid;grid-template-columns:37px 1fr;gap:9px;align-items:center;box-shadow:0 3px 11px rgba(5,68,42,.05)}.ac-kpi-icon{width:37px;height:37px;border-radius:9px;background:var(--c);color:#fff;display:flex;align-items:center;justify-content:center;font-weight:950}.ac-kpi small{display:block;color:#587064;font-size:.58rem;font-weight:800}.ac-kpi b{display:block;color:#112f3d;font-size:1.28rem;line-height:1.35}.ac-kpi em{font-style:normal;color:#6e8479;font-size:.52rem}
      .ac-box{border:1px solid #d8e9e1;border-radius:9px;background:#fff;padding:10px;box-shadow:0 3px 10px rgba(6,68,43,.045);height:100%;box-sizing:border-box}.ac-title{display:flex;justify-content:space-between;align-items:center;color:#153e31;font-size:.76rem;font-weight:900;border-bottom:1px solid #e8f1ec;padding-bottom:7px;margin-bottom:7px}.ac-title span{font-size:.53rem;color:#198758}.ac-table{width:100%;border-collapse:collapse;font-size:.59rem;color:#294a3c}.ac-table th{background:#f1f7f4;color:#61776d;text-align:left;padding:7px 6px;font-size:.52rem}.ac-table td{padding:7px 6px;border-bottom:1px solid #edf3ef;vertical-align:middle}.ac-pill{display:inline-block;border-radius:99px;padding:3px 6px;color:var(--c);background:color-mix(in srgb,var(--c) 13%,white);font-size:.51rem;font-weight:850;white-space:nowrap}.ac-row-title{font-weight:750;color:#24483a}.ac-side-item{display:grid;grid-template-columns:23px 1fr auto;gap:7px;align-items:start;padding:8px 3px;border-bottom:1px solid #edf3ef}.ac-side-item:last-child{border:0}.ac-num{width:22px;height:22px;border-radius:50%;display:flex;align-items:center;justify-content:center;background:#d9f5e5;color:#087a48;font-size:.56rem;font-weight:900}.ac-side-item b{display:block;font-size:.61rem;color:#26483b}.ac-side-item small{display:block;font-size:.53rem;color:#74877e;margin-top:2px}.ac-cost{font-size:1.58rem;font-weight:950;color:#102f3d;margin:8px 0}.ac-cost-note{font-size:.56rem;color:#70847a}.ac-empty{padding:22px;text-align:center;color:#71877b;font-size:.67rem;border:1px dashed #cce0d4;border-radius:8px;background:#f8fcfa}
      @media(max-width:900px){.ac-table{font-size:.52rem}.ac-kpi{height:auto;min-height:78px}}
    </style>
    <div class="ac-hero"><div><h2>Operasyon Aksiyon Merkezi</h2><p>Problemi gör, sorumlu ata, termin ver, doğrula ve sonuçlandır.</p></div><span class="ac-live">● CANLI İŞ AKIŞI</span></div>
    """, unsafe_allow_html=True)

    open_count, critical_count, overdue_count, completed_today, avg_resolution, on_time_rate = _metrics(actions)
    kpi_columns = st.columns(6, gap="small")
    kpi_data = [
        ("☷", "Açık Aksiyon", open_count, "Takipte", "#079456"),
        ("!", "Kritik", critical_count, "Öncelikli", "#ec3f47"),
        ("◷", "Geciken", overdue_count, "Termin aşıldı", "#ee9b18"),
        ("✓", "Bugün Tamamlanan", completed_today, "Kapatılan", "#079456"),
        ("◴", "Ort. Çözüm", f"{avg_resolution:.1f} sa", "Tamamlama süresi", "#2787e6"),
        ("◎", "Zamanında", f"%{on_time_rate:.0f}", "SLA başarısı", "#079456"),
    ]
    for column, (icon, label, value, note, colour) in zip(kpi_columns, kpi_data):
        column.markdown(f'<div class="ac-kpi" style="--c:{colour}"><div class="ac-kpi-icon">{icon}</div><div><small>{label}</small><b>{value}</b><em>{note}</em></div></div>', unsafe_allow_html=True)

    filter_a, filter_b, filter_c, filter_d, filter_e = st.columns([1, 1, 1, 1.5, .7], gap="small")
    selected_status = filter_a.selectbox("Durum", ["Tümü"] + ALL_STATUSES, key="ac_status")
    selected_priority = filter_b.selectbox("Öncelik", ["Tümü", "Kritik", "Yüksek", "Normal", "Düşük"], key="ac_priority")
    selected_source = filter_c.selectbox("Kaynak", ["Tümü"] + sorted(actions["source_type"].dropna().astype(str).unique().tolist()) if not actions.empty else ["Tümü"], key="ac_source")
    search = filter_d.text_input("Ara", placeholder="Aksiyon, makine veya problem...", key="ac_search")
    filter_e.write("")
    if filter_e.button("Yeni Aksiyon", type="primary", use_container_width=True, key="ac_new_toggle"):
        st.session_state["ac_new_open"] = not st.session_state.get("ac_new_open", False)

    if st.session_state.get("ac_new_open", False):
        with st.container(border=True):
            st.markdown("#### Yeni Aksiyon")
            with st.form("ac_new_form", clear_on_submit=True):
                row_a, row_b, row_c = st.columns(3)
                source_type = row_a.selectbox("Kaynak", ["Manuel", "Üretim", "Alarm", "Kalite", "Bakım", "5 Why", "Stok"])
                machine_options = [""] + machines["machine_code"].astype(str).tolist() if not machines.empty else [""]
                machine_code = row_b.selectbox("Makine", machine_options, format_func=lambda value: value or "Genel")
                priority = row_c.selectbox("Öncelik", ["Kritik", "Yüksek", "Normal", "Düşük"], index=2)
                title = st.text_input("Aksiyon başlığı *", placeholder="Çözülmesi gereken problemi kısa yazın")
                description = st.text_area("Açıklama", placeholder="Beklenen sonuç ve gerekli müdahale", height=75)
                owner_options = users["full_name"].fillna(users["username"]).astype(str).tolist() if not users.empty else [current_name or current_username]
                row_d, row_e, row_f = st.columns(3)
                owner_name = row_d.selectbox("Sorumlu", owner_options)
                due_date = row_e.date_input("Termin tarihi", value=date.today() + timedelta(days=1))
                due_time = row_f.time_input("Termin saati", value=(datetime.now() + timedelta(hours=4)).time().replace(second=0, microsecond=0))
                estimated_loss = st.number_input("Tahmini kayıp maliyeti (₺)", min_value=0.0, value=0.0, step=100.0)
                submitted = st.form_submit_button("Aksiyonu Oluştur", type="primary", use_container_width=True)
            if submitted:
                if not title.strip():
                    st.error("Aksiyon başlığı zorunludur.")
                else:
                    selected_user = users[users["full_name"].fillna(users["username"]).astype(str) == owner_name]
                    owner_username = str(selected_user.iloc[0]["username"]) if not selected_user.empty else ""
                    values = _new_action(f"Manuel:{uuid.uuid4().hex}", source_type, 0, machine_code, title.strip(), description.strip(), priority,
                                         owner_username, owner_name, datetime.combine(due_date, due_time).strftime("%Y-%m-%d %H:%M:%S"), estimated_loss, current_username)
                    execute("""INSERT INTO operational_actions(id,action_no,source_key,source_type,source_id,machine_code,title,description,priority,status,owner_username,owner_name,due_at,estimated_loss,created_by,created_at,started_at,completed_at,verification_note)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", values)
                    if notifier and owner_username:
                        notifier(recipient_username=owner_username, notification_type="Aksiyon", priority=priority,
                                 title=f"Yeni aksiyon: {title.strip()}", message=f"Termin: {due_date:%d.%m.%Y} {due_time:%H:%M}",
                                 machine_code=machine_code, target_module="🎯 Aksiyon Merkezi", entity_type="operational_action", entity_id=values[0])
                    st.session_state["ac_new_open"] = False
                    st.success("Aksiyon oluşturuldu ve sorumluya bildirildi.")
                    st.rerun()

    filtered = actions.copy()
    if not filtered.empty:
        if selected_status != "Tümü": filtered = filtered[filtered["status"] == selected_status]
        if selected_priority != "Tümü": filtered = filtered[filtered["priority"] == selected_priority]
        if selected_source != "Tümü": filtered = filtered[filtered["source_type"] == selected_source]
        if search.strip():
            haystack = filtered[["action_no", "machine_code", "title", "owner_name"]].fillna("").astype(str).agg(" ".join, axis=1)
            filtered = filtered[haystack.str.contains(search.strip(), case=False, regex=False)]
        filtered["_priority"] = filtered["priority"].map(PRIORITY_ORDER).fillna(9)
        filtered = filtered.sort_values(["_priority", "due_at", "id"])

    main_col, side_col = st.columns([4.2, 1.42], gap="small")
    with main_col:
        st.markdown('<div class="ac-box"><div class="ac-title">Aktif Aksiyonlar <span>Tümünü Gör</span></div>', unsafe_allow_html=True)
        if filtered.empty:
            st.markdown('<div class="ac-empty">Filtreye uygun aksiyon bulunmuyor.</div>', unsafe_allow_html=True)
        else:
            rows = []
            for _, row in filtered.head(25).iterrows():
                priority_colour = PRIORITY_COLOURS.get(str(row["priority"]), "#15945a")
                status_colour = STATUS_COLOURS.get(str(row["status"]), "#667f91")
                due_value = pd.to_datetime(row["due_at"], errors="coerce")
                due_text = due_value.strftime("%d.%m %H:%M") if pd.notna(due_value) else "—"
                rows.append(f'<tr><td><b>{_safe(row["action_no"])}</b></td><td>{_safe(row["source_type"])}</td><td>{_safe(row["machine_code"] or "Genel")}</td><td class="ac-row-title">{_safe(row["title"])}</td><td><span class="ac-pill" style="--c:{priority_colour}">{_safe(row["priority"])}</span></td><td>{_safe(row["owner_name"] or "Atanmadı")}</td><td>{due_text}</td><td><span class="ac-pill" style="--c:{status_colour}">{_safe(row["status"])}</span></td></tr>')
            st.markdown('<table class="ac-table"><thead><tr><th>No</th><th>Kaynak</th><th>Makine</th><th>Problem</th><th>Öncelik</th><th>Sorumlu</th><th>Termin</th><th>Durum</th></tr></thead><tbody>' + "".join(rows) + "</tbody></table></div>", unsafe_allow_html=True)

            labels = {int(row["id"]): f'{row["action_no"]} · {row["title"]}' for _, row in filtered.iterrows()}
            selected_id = st.selectbox("Aksiyon detayı", list(labels), format_func=lambda value: labels[value], key="ac_selected")
            selected = filtered[filtered["id"] == selected_id].iloc[0]
            with st.expander(f'{selected["action_no"]} · Detay ve Güncelleme', expanded=False):
                detail_a, detail_b = st.columns([1.25, 1])
                with detail_a:
                    st.markdown(f'**Problem:** {selected["title"]}')
                    st.write(selected["description"] or "Açıklama girilmedi.")
                    st.caption(f'Kaynak: {selected["source_type"]} · Makine: {selected["machine_code"] or "Genel"} · Oluşturan: {selected["created_by"]}')
                can_update = current_role == "admin" or str(selected["owner_username"] or "") == current_username or current_role in ("maintenance", "quality")
                with detail_b:
                    if can_update:
                        with st.form("ac_update_form"):
                            status_value = str(selected["status"])
                            new_status = st.selectbox("Durum", ALL_STATUSES, index=ALL_STATUSES.index(status_value) if status_value in ALL_STATUSES else 0)
                            owner_options = users["full_name"].fillna(users["username"]).astype(str).tolist()
                            owner_value = str(selected["owner_name"] or "")
                            owner_index = owner_options.index(owner_value) if owner_value in owner_options else 0
                            new_owner = st.selectbox("Sorumlu", owner_options, index=owner_index) if owner_options else st.text_input("Sorumlu", value=owner_value)
                            verification = st.text_area("Çözüm / doğrulama notu", value=str(selected["verification_note"] or ""), height=70)
                            update = st.form_submit_button("Aksiyonu Güncelle", type="primary", use_container_width=True)
                        if update:
                            user_match = users[users["full_name"].fillna(users["username"]).astype(str) == new_owner]
                            new_owner_username = str(user_match.iloc[0]["username"]) if not user_match.empty else ""
                            started_at = selected["started_at"] or (_now() if new_status in ("Müdahale", "İnceleniyor", "Doğrulama") else None)
                            completed_at = _now() if new_status == "Tamamlandı" else None
                            execute("UPDATE operational_actions SET status=?,owner_username=?,owner_name=?,started_at=?,completed_at=?,verification_note=? WHERE id=?",
                                    (new_status, new_owner_username, new_owner, started_at, completed_at, verification.strip(), int(selected_id)))
                            st.success("Aksiyon güncellendi.")
                            st.rerun()
                    else:
                        st.info("Bu aksiyonu yalnızca sorumlusu veya yetkili ekip güncelleyebilir.")

    with side_col:
        st.markdown('<div class="ac-box"><div class="ac-title">Vardiya Devir Teslimi <span>Aktif Konular</span></div>', unsafe_allow_html=True)
        open_actions = actions[actions["status"].isin(OPEN_STATUSES)].copy() if not actions.empty else pd.DataFrame()
        if not open_actions.empty:
            open_actions["_priority"] = open_actions["priority"].map(PRIORITY_ORDER).fillna(9)
            for number, (_, row) in enumerate(open_actions.sort_values(["_priority", "due_at"]).head(3).iterrows(), 1):
                st.markdown(f'<div class="ac-side-item"><div class="ac-num">{number}</div><div><b>{_safe(row["machine_code"] or "Genel")} · {_safe(row["title"])}</b><small>{_safe(row["owner_name"] or "Atanmadı")} müdahalesi bekleniyor.</small></div><span class="ac-pill" style="--c:{PRIORITY_COLOURS.get(str(row["priority"]), "#15945a")}">{_safe(row["priority"])}</span></div>', unsafe_allow_html=True)
        else:
            st.markdown('<div class="ac-empty">Devredilecek açık konu yok.</div>', unsafe_allow_html=True)
        with st.expander("Devir teslim notu oluştur"):
            with st.form("handover_form"):
                shift_a, shift_b = st.columns(2)
                from_shift = shift_a.selectbox("Çıkan vardiya", ["Sabah", "Akşam", "Gece"])
                to_shift = shift_b.selectbox("Gelen vardiya", ["Akşam", "Gece", "Sabah"])
                handover_note = st.text_area("Devir notu", placeholder="Devam eden işler ve dikkat edilmesi gerekenler", height=75)
                save_handover = st.form_submit_button("Devir Teslimi Kaydet", use_container_width=True)
            if save_handover and handover_note.strip():
                execute("INSERT INTO shift_handover_notes(id,from_shift,to_shift,note,created_by,created_at,acknowledged_by,acknowledged_at) VALUES(?,?,?,?,?,?,?,?)",
                        (_uid(), from_shift, to_shift, handover_note.strip(), current_name or current_username, _now(), None, None))
                st.success("Devir teslim notu kaydedildi.")
                st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)

        st.markdown('<div class="ac-box" style="margin-top:9px"><div class="ac-title">Kritik Bildirimler <span>Tümünü Gör</span></div>', unsafe_allow_html=True)
        critical_rows = actions[(actions["priority"] == "Kritik") & actions["status"].isin(OPEN_STATUSES)].head(5) if not actions.empty else pd.DataFrame()
        if critical_rows.empty:
            st.markdown('<div class="ac-empty">Açık kritik bildirim yok.</div>', unsafe_allow_html=True)
        else:
            for _, row in critical_rows.iterrows():
                st.markdown(f'<div class="ac-side-item"><div class="ac-num" style="background:#ffe5e6;color:#c62e35">!</div><div><b>{_safe(row["machine_code"] or "Genel")} · {_safe(row["title"])}</b><small>{_safe(row["description"])}</small></div></div>', unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True)

    bottom_a, bottom_b, bottom_c = st.columns([1.25, 1.55, 1.15], gap="small")
    estimated_loss = float(pd.to_numeric(actions["estimated_loss"], errors="coerce").fillna(0).sum()) if not actions.empty else 0
    with bottom_a:
        st.markdown(f'<div class="ac-box"><div class="ac-title">Bugünkü Kayıp Maliyeti <span>Tahmini</span></div><div class="ac-cost">₺{estimated_loss:,.0f}</div><div class="ac-cost-note">Açık alarm, kalite kaybı, bakım ve geciken iş emirlerinden hesaplanan operasyon tahminidir.</div></div>', unsafe_allow_html=True)
    with bottom_b, st.container(border=True):
        st.markdown("<div class='ac-title'>Son 7 Gün Aksiyon Trendi <span>Oluşturulan / Tamamlanan</span></div>", unsafe_allow_html=True)
        days = pd.date_range(end=pd.Timestamp(date.today()), periods=7, freq="D")
        created_dates = pd.to_datetime(actions["created_at"], errors="coerce").dt.normalize() if not actions.empty else pd.Series(dtype="datetime64[ns]")
        completed_dates = pd.to_datetime(actions["completed_at"], errors="coerce").dt.normalize() if not actions.empty else pd.Series(dtype="datetime64[ns]")
        created_counts = [int((created_dates == day).sum()) for day in days]
        completed_counts = [int((completed_dates == day).sum()) for day in days]
        trend = go.Figure()
        trend.add_scatter(x=days, y=created_counts, mode="lines+markers", name="Oluşturulan", line=dict(color="#168e56", width=3))
        trend.add_scatter(x=days, y=completed_counts, mode="lines+markers", name="Tamamlanan", line=dict(color="#2d8de0", width=2, dash="dash"))
        trend.update_layout(height=175, margin=dict(l=8, r=8, t=8, b=22), legend=dict(orientation="h", y=1.12), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
        st.plotly_chart(trend, use_container_width=True, config={"displayModeBar": False}, key="action_center_trend")
    with bottom_c, st.container(border=True):
        st.markdown("<div class='ac-title'>Aksiyon Durumu <span>Dağılım</span></div>", unsafe_allow_html=True)
        status_counts = actions["status"].value_counts() if not actions.empty else pd.Series({"Açık": 0})
        donut = go.Figure(go.Pie(labels=status_counts.index, values=status_counts.values, hole=.63,
                                 marker_colors=[STATUS_COLOURS.get(str(item), "#82948b") for item in status_counts.index], textinfo="label+value"))
        donut.update_layout(height=175, margin=dict(l=5, r=5, t=5, b=5), showlegend=False, paper_bgcolor="rgba(0,0,0,0)")
        st.plotly_chart(donut, use_container_width=True, config={"displayModeBar": False}, key="action_center_status")

    if not handovers.empty:
        with st.expander("Son vardiya devir teslim notları"):
            shown = handovers[["from_shift", "to_shift", "note", "created_by", "created_at"]].rename(columns={"from_shift": "Çıkan", "to_shift": "Gelen", "note": "Not", "created_by": "Kaydeden", "created_at": "Tarih"})
            st.dataframe(shown, hide_index=True, use_container_width=True)

