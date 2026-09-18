"""MES verilerinden açıklanabilir tahmin ve operasyon önerileri üretir."""
from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st


def _oee_components(row):
    planned = max(float(row.get("planned_time", 0) or 0), 0)
    downtime = max(float(row.get("downtime", 0) or 0), 0)
    production = max(float(row.get("production", 0) or 0), 0)
    defective = max(float(row.get("defective", 0) or 0), 0)
    ideal_cycle = max(float(row.get("ideal_cycle", 0) or 0), 0)
    operating = max(planned - downtime, 0)
    availability = operating / planned if planned else 0
    performance = min(ideal_cycle * production / operating, 1) if operating else 0
    quality = max(min((production - defective) / production, 1), 0) if production else 0
    return availability * 100, performance * 100, quality * 100, availability * performance * quality * 100


def _history_rates(history):
    if history.empty:
        return {}
    frame = history.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
    frame["quantity"] = pd.to_numeric(frame["quantity"], errors="coerce")
    frame = frame.dropna(subset=["timestamp", "quantity"]).sort_values(["machine_code", "timestamp", "id"])
    frame["increase"] = frame.groupby("machine_code")["quantity"].diff().clip(lower=0).fillna(0)
    recent_limit = frame["timestamp"].max() - pd.Timedelta(days=7) if not frame.empty else pd.Timestamp.now()
    frame = frame[frame["timestamp"] >= recent_limit]
    result = {}
    for code, group in frame.groupby("machine_code"):
        elapsed = max((group["timestamp"].max() - group["timestamp"].min()).total_seconds() / 3600, 0)
        result[str(code)] = float(group["increase"].sum()) / elapsed if elapsed >= .25 else 0
    return result


def render_smart_analysis(query):
    machines = query("""SELECT id,machine_code,status,production,target,planned_time,downtime,
        ideal_cycle,defective,product,operator,shift,last_maintenance,next_maintenance
        FROM machines ORDER BY machine_code""")
    history = query("SELECT id,machine_code,quantity,timestamp FROM production_history ORDER BY id DESC LIMIT 3000")
    stops = query("SELECT machine_code,reason,duration,event_at FROM downtime ORDER BY id DESC LIMIT 1500")
    sensors = query("SELECT machine_code,temperature,vibration,pressure,rpm,timestamp FROM sensors ORDER BY machine_code")
    orders = query("SELECT order_no,machine_code,product,target,produced,priority,status,due_date FROM work_orders WHERE status!='Tamamlandı' ORDER BY id DESC")
    maintenance = query("SELECT machine_code,maintenance_type,next_date,status FROM maintenance WHERE status!='Tamamlandı' ORDER BY next_date")
    alarms = query("SELECT machine_code,alarm,level,time FROM alarms WHERE acknowledged=0 ORDER BY id DESC")
    stock = query("SELECT product_code,product_name,stock,min_stock FROM products ORDER BY product_code")

    st.subheader("🧠 Akıllı Analiz")
    st.caption("Tahminler mevcut üretim, duruş, sensör, alarm, bakım ve stok kayıtlarından hesaplanır.")
    st.markdown("""
    <style>
      .ai-kpi{min-height:91px;background:linear-gradient(120deg,#fff,#eef9f5);border:1px solid #d4e9df;border-left:4px solid var(--c);border-radius:10px;padding:11px 13px;box-sizing:border-box}
      .ai-kpi small{display:block;color:#4b695b;font-size:11px;font-weight:750}.ai-kpi b{display:block;color:#07543a;font-size:23px;line-height:34px}.ai-kpi span{font-size:9px;color:#6c8579}
      .ai-title{font-size:14px;color:#10583e;font-weight:850;margin-bottom:9px}.ai-rec{border-left:4px solid var(--c);background:#f8fcfa;border-radius:7px;padding:9px 11px;margin:7px 0;color:#315b49;font-size:11px}
      .ai-rec b{display:block;color:#114e38;font-size:12px;margin-bottom:3px}.ai-score{display:inline-block;padding:3px 8px;border-radius:12px;font-size:9px;font-weight:800;background:#e1f4ea;color:#08794d}
    </style>
    """, unsafe_allow_html=True)

    machine_options = ["Tümü"] + machines["machine_code"].dropna().astype(str).tolist()
    filter_cols = st.columns([1, 1.5, .65])
    selected_machine = filter_cols[0].selectbox("Makine", machine_options, key="smart_machine")
    search = filter_cols[1].text_input("Analizde ara", placeholder="Makine, ürün veya öneri...", key="smart_search")
    filter_cols[2].write("")
    if filter_cols[2].button("↻ Yenile", use_container_width=True, key="smart_refresh"):
        st.cache_data.clear()
        st.rerun()

    rates = _history_rates(history)
    forecast_rows = []
    oee_rows = []
    for _, machine in machines.iterrows():
        code = str(machine["machine_code"])
        availability, performance, quality, oee = _oee_components(machine)
        oee_rows.append({"Makine": code, "Kullanılabilirlik": availability, "Performans": performance, "Kalite": quality, "OEE": oee})
        rate = rates.get(code, 0)
        current = float(machine["production"] or 0)
        target = float(machine["target"] or 0)
        remaining = max(target - current, 0)
        eta = remaining / rate if rate > 0 else None
        predicted = current + rate * 8
        if remaining <= 0:
            state = "Tamamlandı"
        elif machine["status"] == "Arızalı":
            state = "Kritik"
        elif rate <= 0:
            state = "Veri Bekleniyor"
        elif predicted < target:
            state = "Riskli"
        else:
            state = "Hedefte"
        forecast_rows.append({"Makine": code, "Ürün": machine["product"], "Mevcut": current, "Hedef": target,
                              "Kalan": remaining, "Saatlik Hız": rate, "8 Saat Tahmini": predicted,
                              "Tahmini Süre (saat)": eta, "Durum": state})
    forecast = pd.DataFrame(forecast_rows)
    oee_data = pd.DataFrame(oee_rows)

    stops["duration"] = pd.to_numeric(stops["duration"], errors="coerce").fillna(0).clip(lower=0)
    stops["event_at"] = pd.to_datetime(stops["event_at"], errors="coerce")
    recent_stops = stops[stops["event_at"] >= pd.Timestamp.now() - pd.Timedelta(days=30)]
    failure_minutes = recent_stops[recent_stops["reason"].fillna("").str.contains("Arıza", case=False, regex=False)].groupby("machine_code")["duration"].sum().to_dict()
    active_alarm_count = alarms.groupby("machine_code").size().to_dict() if not alarms.empty else {}
    sensor_map = sensors.set_index("machine_code").to_dict("index") if not sensors.empty else {}
    latest_maintenance = maintenance.drop_duplicates("machine_code", keep="first").set_index("machine_code").to_dict("index") if not maintenance.empty else {}
    maintenance_rows = []
    today = pd.Timestamp(date.today())
    for _, machine in machines.iterrows():
        code = str(machine["machine_code"])
        score, reasons = 0, []
        record = latest_maintenance.get(code, {})
        next_date = pd.to_datetime(record.get("next_date"), errors="coerce")
        days = (next_date.normalize() - today).days if pd.notna(next_date) else None
        if days is not None and days < 0:
            score += 40; reasons.append(f"Bakım {abs(days)} gün gecikmiş")
        elif days is not None and days <= 7:
            score += 22; reasons.append(f"Bakıma {days} gün kaldı")
        if machine["status"] == "Arızalı":
            score += 30; reasons.append("Makine arızalı")
        failure = float(failure_minutes.get(code, 0))
        if failure > 0:
            score += min(20, 5 + failure / 10); reasons.append(f"30 günde {failure:.0f} dk arıza")
        sensor = sensor_map.get(code, {})
        if float(sensor.get("temperature", 0) or 0) >= 85:
            score += 15; reasons.append("Sıcaklık yüksek")
        if float(sensor.get("vibration", 0) or 0) >= 5:
            score += 15; reasons.append("Titreşim yüksek")
        alarm_count = int(active_alarm_count.get(code, 0))
        if alarm_count:
            score += min(15, alarm_count * 5); reasons.append(f"{alarm_count} açık alarm")
        score = min(round(score), 100)
        level = "Kritik" if score >= 70 else ("Yüksek" if score >= 45 else ("İzle" if score >= 20 else "Normal"))
        maintenance_rows.append({"Makine": code, "Risk Puanı": score, "Seviye": level,
                                 "Sonraki Bakım": next_date.strftime("%d.%m.%Y") if pd.notna(next_date) else "—",
                                 "Neden": " · ".join(reasons) if reasons else "Risk göstergesi yok"})
    maintenance_risk = pd.DataFrame(maintenance_rows).sort_values("Risk Puanı", ascending=False)

    recommendations = []
    for _, row in maintenance_risk.iterrows():
        if row["Risk Puanı"] >= 45:
            recommendations.append({"Öncelik": 1 if row["Risk Puanı"] >= 70 else 2, "Alan": "Bakım", "Makine": row["Makine"],
                                    "Başlık": f"{row['Makine']} bakım müdahalesi", "Açıklama": row["Neden"], "Aksiyon": "Bakım talebi açın ve teknisyen atayın."})
    for _, row in forecast.iterrows():
        if row["Durum"] in ("Kritik", "Riskli"):
            recommendations.append({"Öncelik": 1 if row["Durum"] == "Kritik" else 2, "Alan": "Üretim", "Makine": row["Makine"],
                                    "Başlık": f"{row['Makine']} hedef riski", "Açıklama": f"Kalan {row['Kalan']:.0f} adet, hız {row['Saatlik Hız']:.1f} adet/saat.", "Aksiyon": "İş emri sırasını, duruşları ve kapasiteyi kontrol edin."})
    for _, row in oee_data.iterrows():
        if row["OEE"] < 60:
            component = min(["Kullanılabilirlik", "Performans", "Kalite"], key=lambda item: row[item])
            recommendations.append({"Öncelik": 2, "Alan": "OEE", "Makine": row["Makine"], "Başlık": f"{row['Makine']} OEE kaybı",
                                    "Açıklama": f"OEE %{row['OEE']:.1f}; en düşük bileşen {component} (%{row[component]:.1f}).", "Aksiyon": f"{component} kaybının nedenlerini inceleyin."})
    for _, row in stock.iterrows():
        if float(row["stock"] or 0) < float(row["min_stock"] or 0):
            recommendations.append({"Öncelik": 2, "Alan": "Stok", "Makine": "—", "Başlık": f"{row['product_code']} kritik stok",
                                    "Açıklama": f"Mevcut {row['stock']}, minimum {row['min_stock']}.", "Aksiyon": "Stok girişi veya satın alma planlayın."})
    recs = pd.DataFrame(recommendations)
    if not recs.empty:
        recs = recs.sort_values(["Öncelik", "Alan", "Makine"]).reset_index(drop=True)

    if selected_machine != "Tümü":
        forecast = forecast[forecast["Makine"] == selected_machine]
        oee_data = oee_data[oee_data["Makine"] == selected_machine]
        maintenance_risk = maintenance_risk[maintenance_risk["Makine"] == selected_machine]
        if not recs.empty: recs = recs[(recs["Makine"] == selected_machine) | (recs["Makine"] == "—")]
    if search.strip():
        needle = search.strip()
        forecast = forecast[(forecast["Makine"].astype(str) + " " + forecast["Ürün"].fillna("")).str.contains(needle, case=False, regex=False)]
        maintenance_risk = maintenance_risk[(maintenance_risk["Makine"] + " " + maintenance_risk["Neden"]).str.contains(needle, case=False, regex=False)]
        if not recs.empty:
            recs = recs[(recs["Makine"] + " " + recs["Başlık"] + " " + recs["Açıklama"]).str.contains(needle, case=False, regex=False)]

    risk_count = int(forecast["Durum"].isin(["Kritik", "Riskli"]).sum())
    critical_maintenance = int((maintenance_risk["Risk Puanı"] >= 70).sum())
    average_oee = float(oee_data["OEE"].mean()) if not oee_data.empty else 0
    metrics = st.columns(4)
    for col, (label, value, note, colour) in zip(metrics, [
        ("Hedef Riski", risk_count, "Makine", "#ef4d55"), ("Ortalama OEE", f"%{average_oee:.1f}", "Seçili makineler", "#159765"),
        ("Kritik Bakım Riski", critical_maintenance, "Makine", "#f0a11a"), ("Açık Öneri", len(recs), "Öncelikli aksiyon", "#159765")]):
        col.markdown(f'<div class="ai-kpi" style="--c:{colour}"><small>{label}</small><b>{value}</b><span>{note}</span></div>', unsafe_allow_html=True)

    tabs = st.tabs(["Üretim Tahmini", "OEE Analizi", "Bakım Tahmini", "Otomatik Öneriler"])
    with tabs[0]:
        st.markdown('<div class="ai-title">Önümüzdeki 8 Saat Üretim Tahmini</div>', unsafe_allow_html=True)
        if forecast.empty: st.info("Analiz edilecek üretim verisi bulunamadı.")
        else:
            fig = go.Figure()
            fig.add_bar(x=forecast["Makine"], y=forecast["Mevcut"], name="Mevcut", marker_color="#159765")
            fig.add_bar(x=forecast["Makine"], y=(forecast["8 Saat Tahmini"] - forecast["Mevcut"]).clip(lower=0), name="8 saat ek üretim", marker_color="#69c99d")
            fig.add_scatter(x=forecast["Makine"], y=forecast["Hedef"], name="Hedef", mode="lines+markers", line=dict(color="#f0a11a", dash="dash"))
            fig.update_layout(barmode="stack", height=300, margin=dict(l=10,r=10,t=15,b=30), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar":False}, key="smart_forecast_chart")
            table = forecast.copy(); table["Tahmini Süre (saat)"] = table["Tahmini Süre (saat)"].map(lambda value: "—" if pd.isna(value) else f"{value:.1f}")
            for col in ["Mevcut", "Hedef", "Kalan", "Saatlik Hız", "8 Saat Tahmini"]: table[col] = table[col].round(1)
            st.dataframe(table, hide_index=True, use_container_width=True)
    with tabs[1]:
        st.markdown('<div class="ai-title">Makine Bazlı OEE Bileşenleri</div>', unsafe_allow_html=True)
        if oee_data.empty: st.info("OEE verisi bulunamadı.")
        else:
            fig = go.Figure()
            for component, colour in (("Kullanılabilirlik","#159765"),("Performans","#4ab884"),("Kalite","#7fd1a7"),("OEE","#f0a11a")):
                fig.add_bar(x=oee_data["Makine"], y=oee_data[component], name=component, marker_color=colour)
            fig.update_layout(barmode="group", height=320, yaxis_range=[0,100], margin=dict(l=10,r=10,t=15,b=30), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar":False}, key="smart_oee_chart")
            st.dataframe(oee_data.round(1), hide_index=True, use_container_width=True)
    with tabs[2]:
        st.markdown('<div class="ai-title">Bakım Risk Sıralaması</div>', unsafe_allow_html=True)
        if maintenance_risk.empty: st.info("Bakım riski hesaplanabilecek makine yok.")
        else:
            colours = maintenance_risk["Risk Puanı"].map(lambda value: "#ef4d55" if value >= 70 else ("#f0a11a" if value >= 45 else "#159765"))
            fig = go.Figure(go.Bar(x=maintenance_risk["Risk Puanı"], y=maintenance_risk["Makine"], orientation="h", marker_color=colours, text=maintenance_risk["Risk Puanı"], textposition="outside"))
            fig.update_layout(height=max(240, len(maintenance_risk)*45), xaxis_range=[0,105], margin=dict(l=10,r=25,t=10,b=25), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar":False}, key="smart_maintenance_chart")
            st.dataframe(maintenance_risk, hide_index=True, use_container_width=True)
    with tabs[3]:
        st.markdown('<div class="ai-title">Önceliklendirilmiş Otomatik Öneriler</div>', unsafe_allow_html=True)
        if recs.empty:
            st.success("Şu anda öncelikli aksiyon gerektiren bir risk bulunmuyor.")
        else:
            for _, rec in recs.head(20).iterrows():
                colour = "#ef4d55" if rec["Öncelik"] == 1 else "#f0a11a"
                st.markdown(f'<div class="ai-rec" style="--c:{colour}"><b>{rec["Başlık"]} <span class="ai-score">{rec["Alan"]}</span></b>{rec["Açıklama"]}<br><strong>Öneri:</strong> {rec["Aksiyon"]}</div>', unsafe_allow_html=True)
            st.download_button("Önerileri CSV olarak indir", recs.to_csv(index=False).encode("utf-8-sig"), "akilli-analiz-onerileri.csv", "text/csv", use_container_width=True)

    st.caption("Bu ekran açıklanabilir kural ve eğilim analizidir; sonuçlar kayıt kalitesine bağlıdır ve operasyon sorumlusunun kararıyla uygulanmalıdır.")
