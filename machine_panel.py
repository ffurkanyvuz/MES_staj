"""Makine kartları, tablo görünümü ve ayrıntı penceresi."""
from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st


def _pct(value):
    value = float(value or 0)
    return value * 100 if abs(value) <= 1 else value


def render_machine_panel(query, machines):
    data = machines.copy()
    for column in ("production", "target", "downtime", "availability", "performance", "quality", "oee"):
        data[column] = pd.to_numeric(data[column], errors="coerce").fillna(0)

    st.subheader("Makine Yönetimi")
    st.markdown("""
    <style>
      .mch-stat{min-height:88px;background:linear-gradient(120deg,#fff,#eef9f5);border:1px solid #d5e9e0;
        border-left:4px solid var(--mch-color);border-radius:9px;padding:11px 13px;box-sizing:border-box}
      .mch-stat small{display:block;color:#48675a;font-size:11px;font-weight:750}.mch-stat b{display:block;color:#07543a;font-size:23px;line-height:33px}
      .mch-stat span{font-size:9px;color:#6b8379}.mch-title{font-size:14px;color:#10583e;font-weight:800;margin-bottom:9px}
      .mch-card-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:7px}.mch-state{padding:3px 7px;border-radius:12px;font-size:9px;font-weight:800}
      .mch-run{background:#dcf6e8;color:#07804f}.mch-wait{background:#fff0d4;color:#ac6900}.mch-fault{background:#ffe2e5;color:#d23d48}
      .mch-visual{display:grid;grid-template-columns:72px 1fr;gap:10px;align-items:center;margin:8px 0}.mch-icon{height:68px;border-radius:8px;background:linear-gradient(145deg,#eef5f1,#dceae2);display:flex;align-items:center;justify-content:center;font-size:38px}
      .mch-ring{width:65px;height:65px;border-radius:50%;background:conic-gradient(var(--ring) calc(var(--value)*1%),#e5eee8 0);position:relative;margin:auto}.mch-ring:after{content:"";position:absolute;inset:8px;background:#fff;border-radius:50%}.mch-ring b{position:absolute;z-index:2;inset:19px 0 0;text-align:center;color:#174c38;font-size:10px}
      .mch-line{font-size:10px;color:#587568;line-height:1.8}.mch-line b{float:right;color:#234f3c}.mch-track{height:6px;background:#e2eee7;border-radius:7px;overflow:hidden;margin:4px 0 7px}.mch-track i{display:block;height:100%;border-radius:7px;background:var(--bar)}
      .mch-foot{display:grid;grid-template-columns:1fr 1fr;gap:4px;border-top:1px solid #e7f1eb;padding-top:7px;font-size:9px;color:#537466}
      .mch-sensor{border:1px solid #e0eee7;border-radius:8px;padding:8px;min-height:75px}.mch-sensor small{font-size:9px;color:#678274}.mch-sensor b{display:block;font-size:16px;color:#0d5a3e;margin:4px 0}.mch-detail-title{font-size:12px;font-weight:800;color:#15563f;margin-bottom:7px}
    </style>
    """, unsafe_allow_html=True)

    metric_area = st.container()
    filter_cols = st.columns([1.6, 1, 1, .9])
    with filter_cols[0]:
        search = st.text_input("Makine ara", placeholder="Makine kodu, ürün veya operatör...", key="machine_search_v4")
    with filter_cols[1]:
        status = st.selectbox("Durum", ["Tümü"] + sorted(data["status"].dropna().astype(str).unique().tolist()), key="machine_status_v4")
    with filter_cols[2]:
        shift = st.selectbox("Vardiya", ["Tümü"] + sorted(data["shift"].dropna().astype(str).unique().tolist()), key="machine_shift_v4")
    with filter_cols[3]:
        view_mode = st.radio("Görünüm", ["Kart", "Tablo"], horizontal=True, key="machine_view_v4")

    view = data.copy()
    if status != "Tümü": view = view[view["status"] == status]
    if shift != "Tümü": view = view[view["shift"] == shift]
    if search.strip():
        source = view["machine_code"].fillna("").astype(str) + " " + view["product"].fillna("").astype(str) + " " + view["operator"].fillna("").astype(str)
        view = view[source.str.contains(search.strip(), case=False, regex=False)]

    with metric_area:
        cols = st.columns(4)
        stats = [
            ("Toplam Makine", len(data), f"Çalışıyor: {(data['status'] == 'Çalışıyor').sum()}", "#168e63"),
            ("Toplam Üretim", f"{int(data['production'].sum()):,}", f"Hedef: {int(data['target'].sum()):,}", "#168e63"),
            ("Ortalama OEE", f"%{data['oee'].map(_pct).mean() if not data.empty else 0:.1f}", "Tüm makineler", "#168e63"),
            ("Toplam Duruş", f"{data['downtime'].sum():.1f} dk", "Kayıtlı süre", "#f0a11a"),
        ]
        for col, (label, value, note, colour) in zip(cols, stats):
            col.markdown(f'<div class="mch-stat" style="--mch-color:{colour}"><small>{label}</small><b>{value}</b><span>{note}</span></div>', unsafe_allow_html=True)

    st.markdown('<div class="mch-title">Makineler</div>', unsafe_allow_html=True)
    if view.empty:
        st.info("Filtrelere uygun makine bulunamadı.")
    elif view_mode == "Kart":
        rows = [view.iloc[index:index + 3] for index in range(0, len(view), 3)]
        for row in rows:
            columns = st.columns(3, gap="small")
            for column, (_, machine) in zip(columns, row.iterrows()):
                with column, st.container(border=True):
                    code = str(machine["machine_code"])
                    current_status = str(machine["status"])
                    state_class = "mch-run" if current_status == "Çalışıyor" else ("mch-wait" if current_status == "Beklemede" else "mch-fault")
                    ring = "#14a268" if current_status == "Çalışıyor" else ("#f4ae25" if current_status == "Beklemede" else "#ef4d55")
                    oee = _pct(machine["oee"])
                    completion = min(float(machine["production"]) / max(float(machine["target"]), 1) * 100, 100)
                    st.markdown(f'<div class="mch-card-head"><b>⚙ {code}</b><span class="mch-state {state_class}">● {current_status}</span></div>', unsafe_allow_html=True)
                    st.markdown(f'<div class="mch-visual"><div class="mch-icon">🏭</div><div class="mch-ring" style="--value:{oee:.1f};--ring:{ring}"><b>OEE<br>%{oee:.1f}</b></div></div>', unsafe_allow_html=True)
                    st.markdown(f'<div class="mch-line">Üretim <b>{int(machine["production"]):,} / {int(machine["target"]):,}</b></div><div class="mch-track"><i style="width:{completion:.1f}%;--bar:{ring}"></i></div>', unsafe_allow_html=True)
                    st.markdown(f'<div class="mch-line">Kullanılabilirlik <b>%{_pct(machine["availability"]):.1f}</b><br>Performans <b>%{_pct(machine["performance"]):.1f}</b><br>Kalite <b>%{_pct(machine["quality"]):.1f}</b></div>', unsafe_allow_html=True)
                    st.markdown(f'<div class="mch-foot"><span>👤 {machine["operator"]}</span><span>◆ {machine["product"]}</span><span>◷ {machine["shift"]}</span><span>Bakım: {machine["next_maintenance"]}</span></div>', unsafe_allow_html=True)
                    if st.button(f"{code} detayını aç  ›", use_container_width=True, key=f"machine_detail_open_{code}"):
                        st.session_state["machine_detail_open"] = code
                        st.rerun()
    else:
        table = view.copy()
        table["OEE"] = table["oee"].map(lambda value: f"%{_pct(value):.1f}")
        table["Kullanılabilirlik"] = table["availability"].map(lambda value: f"%{_pct(value):.1f}")
        table["Performans"] = table["performance"].map(lambda value: f"%{_pct(value):.1f}")
        table["Kalite"] = table["quality"].map(lambda value: f"%{_pct(value):.1f}")
        table = table.rename(columns={"machine_code":"Makine", "status":"Durum", "product":"Ürün", "operator":"Operatör", "shift":"Vardiya", "production":"Üretim", "target":"Hedef", "next_maintenance":"Sonraki Bakım"})
        st.dataframe(table[["Makine", "Durum", "Ürün", "Operatör", "Vardiya", "Üretim", "Hedef", "Kullanılabilirlik", "Performans", "Kalite", "OEE", "Sonraki Bakım"]], hide_index=True, use_container_width=True, height=300)
        detail_cols = st.columns([2, 1])
        selected = detail_cols[0].selectbox("Detayı açılacak makine", view["machine_code"].tolist(), key="machine_table_detail_v4")
        detail_cols[1].write("")
        if detail_cols[1].button("Makine detayını aç", type="primary", use_container_width=True, key="machine_table_open_v4"):
            st.session_state["machine_detail_open"] = selected
            st.rerun()

    lower = st.columns([.85, 1.65], gap="small")
    with lower[0], st.container(border=True):
        st.markdown('<div class="mch-title">Makine Durumları</div>', unsafe_allow_html=True)
        status_counts = data["status"].value_counts()
        fig = go.Figure(go.Pie(labels=status_counts.index, values=status_counts.values, hole=.62, marker_colors=["#159765", "#f4ae27", "#ee4b55"]))
        fig.add_annotation(text=f"<b>{len(data)}</b><br><span style='font-size:9px'>makine</span>", showarrow=False)
        fig.update_layout(height=210, margin=dict(l=5, r=5, t=5, b=20), font=dict(size=9), paper_bgcolor="rgba(0,0,0,0)", legend=dict(orientation="v", font=dict(size=9)))
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False}, key="machine_status_v4_chart")
    with lower[1], st.container(border=True):
        st.markdown('<div class="mch-title">Makine Bazlı Üretim / Hedef</div>', unsafe_allow_html=True)
        fig = go.Figure()
        fig.add_bar(x=data["machine_code"], y=data["production"], name="Üretim", marker_color="#149663")
        fig.add_scatter(x=data["machine_code"], y=data["target"], name="Hedef", mode="lines+markers", line=dict(color="#5bc9a0", dash="dash"))
        fig.update_layout(height=210, margin=dict(l=10, r=8, t=5, b=25), font=dict(size=9), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", legend=dict(orientation="h"))
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False}, key="machine_production_v4_chart")

    def close_machine_detail():
        st.session_state["machine_detail_open"] = None

    @st.dialog("Makine Detayı", width="large", on_dismiss=close_machine_detail)
    def machine_detail_dialog(code):
        machine = data[data["machine_code"] == code].iloc[0]
        top_left, top_right = st.columns([3, 1])
        top_left.markdown(f"### ⚙ {code} · Makine Detayı")
        top_right.markdown(f"**{machine['status']}**")
        tabs = st.tabs(["Genel", "Üretim", "OEE", "Duruş", "Sensör", "Kalite", "Bakım", "Enerji"])

        production = query("SELECT quantity,timestamp FROM production_history WHERE machine_code=? ORDER BY id DESC LIMIT 500", (code,))
        downtime = query("SELECT reason,duration,start_time,end_time,notes FROM downtime WHERE machine_code=? ORDER BY id DESC LIMIT 30", (code,))
        sensors = query("SELECT temperature,vibration,pressure,rpm,timestamp FROM sensor_history WHERE machine_code=? ORDER BY id DESC LIMIT 80", (code,))
        if sensors.empty:
            sensors = query("SELECT temperature,vibration,pressure,rpm,timestamp FROM sensors WHERE machine_code=?", (code,))
        quality = query("SELECT product,produced,defective,defect_reason,timestamp FROM quality WHERE machine_code=? ORDER BY id DESC LIMIT 30", (code,))
        maintenance = query("SELECT maintenance_type,description,maintenance_date,next_date,technician,status FROM maintenance WHERE machine_code=? ORDER BY id DESC LIMIT 30", (code,))
        energy = query("SELECT power_kw,energy_kwh,timestamp FROM energy_readings WHERE machine_code=? ORDER BY id DESC LIMIT 80", (code,))
        alarms = query("SELECT alarm,level,time,acknowledged FROM alarms WHERE machine_code=? ORDER BY id DESC LIMIT 5", (code,))
        orders = query("SELECT order_no,product,target,produced,priority,status,due_date FROM work_orders WHERE machine_code=? ORDER BY id DESC LIMIT 10", (code,))

        with tabs[0]:
            left, right = st.columns([1.5, 1])
            with left:
                st.markdown(f"""<div class="mch-detail-title">Genel Bilgiler</div>
                <div class="mch-line">Makine kodu <b>{code}</b><br>Durum <b>{machine['status']}</b><br>Operatör <b>{machine['operator']}</b><br>Vardiya <b>{machine['shift']}</b><br>Ürün <b>{machine['product']}</b><br>Son bakım <b>{machine['last_maintenance']}</b><br>Sonraki bakım <b>{machine['next_maintenance']}</b></div>""", unsafe_allow_html=True)
            with right:
                st.metric("OEE", f"%{_pct(machine['oee']):.1f}")
                st.progress(min(max(_pct(machine["oee"]) / 100, 0), 1))
                st.caption(f"Kullanılabilirlik %{_pct(machine['availability']):.1f} · Performans %{_pct(machine['performance']):.1f} · Kalite %{_pct(machine['quality']):.1f}")
            st.markdown('<div class="mch-detail-title">Hızlı İşlemler</div>', unsafe_allow_html=True)
            actions = st.columns(3)
            if actions[0].button("Bakım Talebi Oluştur", use_container_width=True, key=f"detail_maintenance_{code}"):
                st.session_state["selected_module"] = "🧰 Bakım Talebi"; st.session_state["machine_detail_open"] = None; st.rerun()
            if actions[1].button("İş Emirlerine Git", use_container_width=True, key=f"detail_order_{code}"):
                st.session_state["selected_module"] = "📋 İş Emirleri"; st.session_state["machine_detail_open"] = None; st.rerun()
            if actions[2].button("Alarmları Gör", use_container_width=True, key=f"detail_alarm_{code}"):
                st.session_state["selected_module"] = "🚨 Alarmlar"; st.session_state["machine_detail_open"] = None; st.rerun()
            if not alarms.empty:
                st.markdown('<div class="mch-detail-title">Son Alarmlar</div>', unsafe_allow_html=True)
                st.dataframe(alarms.rename(columns={"alarm":"Alarm", "level":"Seviye", "time":"Zaman", "acknowledged":"Onay"}), hide_index=True, use_container_width=True, height=170)
        with tabs[1]:
            metrics = st.columns(3); metrics[0].metric("Üretim", int(machine["production"])); metrics[1].metric("Hedef", int(machine["target"])); metrics[2].metric("Gerçekleşme", f"%{float(machine['production']) / max(float(machine['target']), 1) * 100:.1f}")
            if production.empty: st.info("Üretim geçmişi bulunmuyor.")
            else:
                production["timestamp"] = pd.to_datetime(production["timestamp"], errors="coerce")
                production = production.sort_values("timestamp")
                fig = go.Figure(go.Bar(x=production["timestamp"], y=production["quantity"], marker_color="#159765")); fig.update_layout(height=270, margin=dict(l=10,r=8,t=10,b=25))
                st.plotly_chart(fig, use_container_width=True, config={"displayModeBar":False}, key=f"detail_production_v4_{code}")
            if not orders.empty: st.dataframe(orders, hide_index=True, use_container_width=True, height=170)
        with tabs[2]:
            oee_cols = st.columns(4)
            for col, label, value in zip(oee_cols, ["Kullanılabilirlik", "Performans", "Kalite", "OEE"], [machine["availability"], machine["performance"], machine["quality"], machine["oee"]]): col.metric(label, f"%{_pct(value):.1f}")
        with tabs[3]:
            st.metric("Toplam duruş", f"{float(machine['downtime']):.1f} dk")
            if downtime.empty: st.info("Duruş kaydı bulunmuyor.")
            else: st.dataframe(downtime.rename(columns={"reason":"Neden", "duration":"Süre", "start_time":"Başlangıç", "end_time":"Bitiş", "notes":"Not"}), hide_index=True, use_container_width=True)
        with tabs[4]:
            if sensors.empty: st.info("Sensör verisi bulunmuyor.")
            else:
                latest = sensors.iloc[0]; sensor_cols = st.columns(4)
                for col, label, value, unit in zip(sensor_cols, ["Sıcaklık", "Titreşim", "Basınç", "RPM"], [latest["temperature"], latest["vibration"], latest["pressure"], latest["rpm"]], ["°C", "mm/s", "bar", ""]):
                    col.markdown(f'<div class="mch-sensor"><small>{label}</small><b>{float(value):.1f} {unit}</b><span>Canlı değer</span></div>', unsafe_allow_html=True)
                history = sensors.sort_values("timestamp").copy(); history["timestamp"] = pd.to_datetime(history["timestamp"], errors="coerce")
                fig = go.Figure()
                for field, colour in (("temperature","#ef555d"),("vibration","#f0aa2b"),("pressure","#159765")): fig.add_scatter(x=history["timestamp"], y=history[field], name=field, mode="lines")
                fig.update_layout(height=260, margin=dict(l=10,r=8,t=10,b=25)); st.plotly_chart(fig, use_container_width=True, config={"displayModeBar":False}, key=f"detail_sensor_v4_{code}")
        with tabs[5]:
            if quality.empty: st.info("Kalite kaydı bulunmuyor.")
            else: st.dataframe(quality.rename(columns={"product":"Ürün", "produced":"Üretim", "defective":"Hatalı", "defect_reason":"Hata Nedeni", "timestamp":"Zaman"}), hide_index=True, use_container_width=True)
        with tabs[6]:
            if maintenance.empty: st.info("Bakım kaydı bulunmuyor.")
            else: st.dataframe(maintenance.rename(columns={"maintenance_type":"Bakım Türü", "description":"Açıklama", "maintenance_date":"Bakım Tarihi", "next_date":"Sonraki Bakım", "technician":"Teknisyen", "status":"Durum"}), hide_index=True, use_container_width=True)
        with tabs[7]:
            if energy.empty: st.info("Enerji verisi bulunmuyor.")
            else:
                latest = energy.iloc[0]; ecols = st.columns(2); ecols[0].metric("Anlık güç", f"{float(latest['power_kw']):.1f} kW"); ecols[1].metric("Son enerji", f"{float(latest['energy_kwh']):.2f} kWh")
                history = energy.sort_values("timestamp").copy(); history["timestamp"] = pd.to_datetime(history["timestamp"], errors="coerce")
                fig = go.Figure(go.Scatter(x=history["timestamp"], y=history["power_kw"], fill="tozeroy", line=dict(color="#159765"))); fig.update_layout(height=270, margin=dict(l=10,r=8,t=10,b=25))
                st.plotly_chart(fig, use_container_width=True, config={"displayModeBar":False}, key=f"detail_energy_v4_{code}")

    detail_code = st.session_state.get("machine_detail_open")
    if detail_code and detail_code in data["machine_code"].astype(str).tolist():
        machine_detail_dialog(detail_code)

    return view
