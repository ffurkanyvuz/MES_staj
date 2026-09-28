import html
import uuid
from datetime import datetime

import pandas as pd
import plotly.express as px
import streamlit as st

from process_data_core import ensure_process_data_schema as _ensure_schema


@st.cache_resource(show_spinner=False)
def ensure_process_data_schema(database_identity, _connection_factory, schema_version="process-tags-v1"):
    _ensure_schema(_connection_factory)
    return True


def _id():
    return int(uuid.uuid4().int % 2_000_000_000) or 1


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _live_tags(tags, sensors):
    """Simülasyon etiketlerini var olan sensör tablosundan canlı gösterir; veri üretimine dokunmaz."""
    if tags.empty:
        return tags
    frame = tags.copy()
    sensor_map = sensors.set_index("machine_code").to_dict("index") if not sensors.empty else {}
    for index, row in frame.iterrows():
        if row.get("source_type") == "Simülasyon" and row.get("machine_code") in sensor_map:
            field = str(row.get("source_address") or "")
            if field in sensor_map[row["machine_code"]]:
                frame.at[index, "current_value"] = sensor_map[row["machine_code"]][field]
                frame.at[index, "last_seen_at"] = sensor_map[row["machine_code"]].get("timestamp")
    return frame


def _tag_status(row):
    if not int(row.get("active") or 0):
        return "Pasif"
    if str(row.get("value_quality") or "") not in {"İyi", "Good"}:
        return "Veri Hatası"
    value = pd.to_numeric(pd.Series([row.get("current_value")]), errors="coerce").iloc[0]
    if pd.isna(value):
        return "Veri Yok"
    critical_min, critical_max = row.get("critical_min"), row.get("critical_max")
    warning_min, warning_max = row.get("warning_min"), row.get("warning_max")
    if (pd.notna(critical_min) and value <= critical_min) or (pd.notna(critical_max) and value >= critical_max):
        return "Kritik"
    if (pd.notna(warning_min) and value <= warning_min) or (pd.notna(warning_max) and value >= warning_max):
        return "Uyarı"
    return "Normal"


def render_process_data_panel(query, execute, machines, current_user="", can_manage=False, api_url=""):
    tags = query("""SELECT t.*,s.source_code,s.source_name FROM process_tags t
        LEFT JOIN process_data_sources s ON s.id=t.source_id ORDER BY t.machine_code,t.tag_code""")
    sources = query("SELECT * FROM process_data_sources ORDER BY id")
    licenses = query("SELECT * FROM process_data_licenses ORDER BY id LIMIT 1")
    sensors = query("SELECT machine_code,temperature,vibration,pressure,rpm,timestamp FROM sensors ORDER BY machine_code")
    tags = _live_tags(tags, sensors)
    if not tags.empty:
        tags["Durum"] = tags.apply(_tag_status, axis=1)

    tag_limit = int(licenses.iloc[0]["tag_limit"]) if not licenses.empty else 0
    used = int(tags["active"].fillna(0).astype(int).sum()) if not tags.empty else 0
    remaining = max(tag_limit - used, 0)
    healthy = int(tags["Durum"].isin(["Normal", "Uyarı", "Kritik"]).sum()) if not tags.empty else 0
    connected = int(sources["status"].isin(["Bağlı", "Hazır"]).sum()) if not sources.empty else 0

    st.markdown("""
    <style>
      .tag-hero{padding:15px 18px;border:1px solid #cce8da;border-radius:14px;background:linear-gradient(120deg,#f7fffb,#eaf8f0);margin-bottom:12px}
      .tag-hero h2{font-size:1.24rem;color:#063e2b;margin:0 0 3px}.tag-hero p{font-size:.77rem;color:#5d786c;margin:0}
      .tag-card{min-height:88px;background:#fff;border:1px solid #d6eade;border-radius:12px;padding:12px 14px;box-shadow:0 3px 12px rgba(13,91,60,.05)}
      .tag-card small{display:block;color:#668075;font-size:.67rem;font-weight:700}.tag-card b{display:block;color:#06442f;font-size:1.35rem;margin:4px 0}.tag-card span{font-size:.62rem;color:#15965d}
      .tag-info{padding:11px 13px;border-radius:10px;background:#eef8f3;border-left:4px solid #13945c;color:#315c49;font-size:.75rem}
    </style>
    <div class="tag-hero"><h2>Proses Veri Toplama · Etiket Merkezi</h2><p>Simülasyon, API ve gelecekteki PLC kaynaklarını tek bir etiket modeliyle yönetin.</p></div>
    """, unsafe_allow_html=True)

    metrics = [
        ("Lisans Kapasitesi", tag_limit, "TREX Etiket Demo"),
        ("Aktif Etiket", used, f"%{(used / tag_limit * 100 if tag_limit else 0):.0f} kullanım"),
        ("Kalan Kapasite", remaining, "Yeni tanımlar için"),
        ("Güncel Veri", healthy, f"{used} aktif etiket içinde"),
        ("Hazır Kaynak", connected, f"{len(sources)} veri kaynağı"),
    ]
    columns = st.columns(5, gap="small")
    for column, (label, value, note) in zip(columns, metrics):
        column.markdown(f'<div class="tag-card"><small>{html.escape(str(label))}</small><b>{html.escape(str(value))}</b><span>{html.escape(str(note))}</span></div>', unsafe_allow_html=True)

    registry_tab, live_tab, sources_tab, license_tab = st.tabs(["Etiket Yönetimi", "Canlı İzleme", "Veri Kaynakları", "Lisans"])

    with registry_tab:
        if can_manage:
            with st.expander("Yeni etiket tanımla", expanded=False):
                machine_options = machines["machine_code"].astype(str).tolist() if not machines.empty else []
                source_labels = {int(row["id"]): f'{row["source_name"]} · {row["source_type"]}' for _, row in sources.iterrows()}
                with st.form("new_process_tag"):
                    c1, c2, c3 = st.columns(3)
                    tag_code = c1.text_input("Etiket kodu *", placeholder="CNC01_MOTOR_CURRENT")
                    tag_name = c2.text_input("Etiket adı *", placeholder="Motor Akımı")
                    machine_code = c3.selectbox("Makine *", machine_options)
                    c4, c5, c6 = st.columns(3)
                    category = c4.selectbox("Kategori", ["Sıcaklık", "Titreşim", "Basınç", "Devir", "Üretim Sayacı", "Makine Durumu", "Enerji", "Akım", "Diğer"])
                    unit = c5.text_input("Birim", placeholder="A, °C, bar, adet")
                    data_type = c6.selectbox("Veri tipi", ["Float", "Integer", "Boolean", "String"])
                    c7, c8, c9 = st.columns(3)
                    source_id = c7.selectbox("Veri kaynağı", list(source_labels), format_func=lambda value: source_labels[value])
                    source_address = c8.text_input("Kaynak adresi *", placeholder="ns=2;s=Motor.Current")
                    interval = c9.number_input("Okuma aralığı (sn)", min_value=1, max_value=3600, value=10)
                    c10, c11, c12, c13 = st.columns(4)
                    warning_min = c10.number_input("Uyarı alt", value=None, placeholder="Boş")
                    warning_max = c11.number_input("Uyarı üst", value=None, placeholder="Boş")
                    critical_min = c12.number_input("Kritik alt", value=None, placeholder="Boş")
                    critical_max = c13.number_input("Kritik üst", value=None, placeholder="Boş")
                    submitted = st.form_submit_button("Etiketi Kaydet", type="primary", use_container_width=True)
                if submitted:
                    clean_code = tag_code.strip().upper().replace(" ", "_")
                    if not clean_code or not tag_name.strip() or not source_address.strip():
                        st.error("Etiket kodu, adı ve kaynak adresi zorunludur.")
                    elif used >= tag_limit:
                        st.error("Etiket lisans kapasitesi dolu.")
                    elif not query("SELECT id FROM process_tags WHERE tag_code=?", (clean_code,)).empty:
                        st.error("Bu etiket kodu zaten kullanılıyor.")
                    else:
                        source = sources[sources["id"] == source_id].iloc[0]
                        execute("""INSERT INTO process_tags(
                            id,tag_code,tag_name,machine_code,category,unit,data_type,source_id,source_type,source_address,
                            sample_interval_seconds,warning_min,warning_max,critical_min,critical_max,scale_factor,scale_offset,
                            active,value_quality,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (_id(), clean_code, tag_name.strip(), machine_code, category, unit.strip(), data_type, int(source_id),
                             source["source_type"], source_address.strip(), int(interval), warning_min, warning_max, critical_min,
                             critical_max, 1.0, 0.0, 1, "Veri Yok", current_user, _now()))
                        st.success(f"{clean_code} tanımlandı."); st.rerun()

        if tags.empty:
            st.info("Henüz etiket tanımlanmadı.")
        else:
            search_col, machine_col, source_col, status_col = st.columns([1.45, 1, 1, 1])
            search = search_col.text_input("Etiket ara", placeholder="Kod veya ad")
            machine_filter = machine_col.selectbox("Makine", ["Tümü"] + sorted(tags["machine_code"].dropna().astype(str).unique().tolist()), key="tag_machine_filter")
            source_filter = source_col.selectbox("Kaynak", ["Tümü"] + sorted(tags["source_type"].dropna().astype(str).unique().tolist()), key="tag_source_filter")
            status_filter = status_col.selectbox("Durum", ["Tümü", "Normal", "Uyarı", "Kritik", "Veri Yok", "Pasif"], key="tag_status_filter")
            visible = tags.copy()
            if search.strip():
                needle = search.strip().lower()
                visible = visible[(visible["tag_code"].str.lower().str.contains(needle, na=False)) | (visible["tag_name"].str.lower().str.contains(needle, na=False))]
            if machine_filter != "Tümü": visible = visible[visible["machine_code"] == machine_filter]
            if source_filter != "Tümü": visible = visible[visible["source_type"] == source_filter]
            if status_filter != "Tümü": visible = visible[visible["Durum"] == status_filter]
            table = visible[["tag_code", "tag_name", "machine_code", "category", "current_value", "unit", "source_type", "source_address", "Durum", "last_seen_at"]].copy()
            table.columns = ["Etiket", "Tanım", "Makine", "Kategori", "Değer", "Birim", "Kaynak", "Adres", "Durum", "Son Veri"]
            st.dataframe(table, use_container_width=True, hide_index=True, height=365)
            if can_manage and not visible.empty:
                labels = {int(row["id"]): f'{row["tag_code"]} · {row["tag_name"]}' for _, row in visible.iterrows()}
                action_col, state_col, button_col = st.columns([2, 1, 1])
                selected_id = action_col.selectbox("Etiket işlemi", list(labels), format_func=lambda value: labels[value])
                selected_active = int(tags[tags["id"] == selected_id].iloc[0]["active"])
                state_col.text_input("Mevcut durum", "Aktif" if selected_active else "Pasif", disabled=True)
                if button_col.button("Pasifleştir" if selected_active else "Aktifleştir", use_container_width=True):
                    if not selected_active and used >= tag_limit:
                        st.error("Etiket lisans kapasitesi dolu.")
                    else:
                        execute("UPDATE process_tags SET active=? WHERE id=?", (0 if selected_active else 1, int(selected_id)))
                        st.success("Etiket durumu güncellendi."); st.rerun()

    with live_tab:
        if tags.empty:
            st.info("Canlı izleme için etiket bulunmuyor.")
        else:
            machine_filter_live = st.selectbox("Canlı makine", ["Tümü"] + sorted(tags["machine_code"].dropna().astype(str).unique().tolist()), key="tag_live_machine")
            live = tags[tags["active"] == 1].copy()
            if machine_filter_live != "Tümü": live = live[live["machine_code"] == machine_filter_live]
            card_columns = st.columns(4, gap="small")
            for card, (_, row) in zip(card_columns * ((len(live) + 3) // 4), live.iterrows()):
                value = "—" if pd.isna(row["current_value"]) else f'{float(row["current_value"]):,.2f}'.rstrip("0").rstrip(".")
                colour = {"Normal":"#13945c", "Uyarı":"#e6a11b", "Kritik":"#e3484f"}.get(row["Durum"], "#899b93")
                card.markdown(f'''<div class="tag-card" style="border-top:4px solid {colour}"><small>{html.escape(str(row["machine_code"]))} · {html.escape(str(row["category"]))}</small><b>{html.escape(value)} {html.escape(str(row["unit"] or ""))}</b><span>{html.escape(str(row["tag_code"]))} · {html.escape(str(row["Durum"]))}</span></div>''', unsafe_allow_html=True)
            history = query("SELECT tag_code,numeric_value,quality,source_timestamp FROM process_tag_readings ORDER BY id DESC LIMIT 500")
            if not history.empty:
                history["source_timestamp"] = pd.to_datetime(history["source_timestamp"], errors="coerce")
                selected_codes = st.multiselect("Trend etiketleri", live["tag_code"].tolist(), default=live["tag_code"].tolist()[:3])
                chart_data = history[history["tag_code"].isin(selected_codes)].dropna(subset=["source_timestamp", "numeric_value"])
                if not chart_data.empty:
                    chart = px.line(chart_data.sort_values("source_timestamp"), x="source_timestamp", y="numeric_value", color="tag_code", markers=True)
                    chart.update_layout(height=300, margin=dict(l=5, r=5, t=10, b=5), legend_title_text="Etiket")
                    st.plotly_chart(chart, use_container_width=True, config={"displayModeBar": False})

    with sources_tab:
        st.markdown('<div class="tag-info">Yerel simülasyon aktif kalır. API veya PLC kaynağı eklenmesi, mevcut simülasyon verisini silmez.</div>', unsafe_allow_html=True)
        source_table = sources[["source_code", "source_name", "source_type", "connection_address", "status", "enabled", "last_seen_at"]].copy()
        source_table.columns = ["Kod", "Kaynak", "Tür", "Bağlantı", "Durum", "Aktif", "Son Görülme"]
        source_table["Aktif"] = source_table["Aktif"].map({1: "Evet", 0: "Hayır"})
        st.dataframe(source_table, use_container_width=True, hide_index=True)
        st.markdown("#### API ile örnek veri gönderimi")
        endpoint = f'{api_url.rstrip("/")}/api/v1/tags/readings' if api_url else "/api/v1/tags/readings"
        st.code(f'''POST {endpoint}\nX-API-Key: <TREX_API_KEY>\nContent-Type: application/json\n\n{{\n  "tag_code": "CNC01_TEMPERATURE",\n  "value": 76.4,\n  "quality": "İyi"\n}}''', language="http")
        st.caption("OPC UA kaynağı bu sürümde hazırlık kaydıdır; gerçek PLC sürücüsü sonraki bağlantı aşamasında etkinleştirilir.")

    with license_tab:
        progress = used / tag_limit if tag_limit else 0
        st.progress(min(progress, 1.0), text=f"{used} / {tag_limit} aktif etiket")
        l1, l2, l3 = st.columns(3)
        l1.metric("Lisans", licenses.iloc[0]["license_name"] if not licenses.empty else "Tanımsız")
        l2.metric("Durum", licenses.iloc[0]["status"] if not licenses.empty else "Tanımsız")
        l3.metric("Kalan", remaining)
        st.info("Bu demo lisansı ürün modelini göstermek içindir. Simülasyon etiketleri de kapasite kullanımına dahildir; hiçbir özellik ücretlendirme veya ödeme işlemi yapmaz.")
