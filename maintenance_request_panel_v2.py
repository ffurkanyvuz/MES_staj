"""Bakım talepleri için yönetici odaklı analiz ekranı."""
from datetime import date, timedelta
import io

import pandas as pd
import plotly.graph_objects as go
import streamlit as st


def render_request_panel(query):
    records = query("""
        SELECT id,machine_code,request_type,description,priority,requested_by,
               requested_at,assigned_to,status,completed_at
        FROM maintenance_requests ORDER BY id DESC
    """)
    machines = query("SELECT machine_code,status,last_maintenance,next_maintenance FROM machines ORDER BY machine_code")
    maintenance = query("SELECT machine_code,maintenance_type,maintenance_date,next_date,technician,status FROM maintenance ORDER BY next_date")
    downtime = query("SELECT machine_code,reason,duration,event_at FROM downtime ORDER BY id")

    records["_created"] = pd.to_datetime(records["requested_at"], errors="coerce")
    records["_completed"] = pd.to_datetime(records["completed_at"], errors="coerce")
    records["_completion_hours"] = (records["_completed"] - records["_created"]).dt.total_seconds() / 3600
    records["_age_hours"] = (pd.Timestamp.now() - records["_created"]).dt.total_seconds() / 3600
    records["_late"] = records["status"].ne("Tamamlandı") & records["_age_hours"].gt(24)
    records["Talep No"] = records["id"].map(lambda value: f"MT-{int(value):03d}")
    downtime["duration"] = pd.to_numeric(downtime["duration"], errors="coerce").fillna(0).clip(lower=0)
    downtime["_time"] = pd.to_datetime(downtime["event_at"], errors="coerce")

    st.subheader("Bakım Talebi Yönetimi")
    st.markdown("""
    <style>
      .mrv2-stat{min-height:88px;background:linear-gradient(120deg,#fff,#eff9f5);border:1px solid #d4e9df;
        border-left:4px solid var(--mrv2-color);border-radius:9px;padding:11px 13px;box-sizing:border-box}
      .mrv2-stat small{display:block;color:#486659;font-size:11px;font-weight:700}.mrv2-stat b{display:block;color:#0a533a;font-size:24px;line-height:33px}
      .mrv2-stat span{font-size:9px;color:#6a8377}.mrv2-title{font-size:14px;color:#11563d;font-weight:800;margin-bottom:9px}
      .mrv2-bar{display:grid;grid-template-columns:88px minmax(0,1fr) 62px;gap:8px;align-items:center;margin:10px 0;font-size:10px;color:#45685a}
      .mrv2-track{height:8px;background:#e2f0e9;border-radius:9px;overflow:hidden}.mrv2-track i{height:100%;display:block;border-radius:9px;background:#159967}
    </style>
    """, unsafe_allow_html=True)

    metric_area = st.container()
    filter_cols = st.columns([1.5, 1, 1, 1, 1.55, .85])
    with filter_cols[0]:
        request_dates = st.date_input("Tarih aralığı", value=(date.today() - timedelta(days=6), date.today()), key="mrv2_dates")
    with filter_cols[1]:
        request_machine = st.selectbox("Makine", ["Tümü"] + sorted(machines["machine_code"].dropna().astype(str).unique().tolist()), key="mrv2_machine")
    with filter_cols[2]:
        request_type = st.selectbox("Bakım türü", ["Tümü"] + sorted(records["request_type"].dropna().astype(str).unique().tolist()), key="mrv2_type")
    with filter_cols[3]:
        request_status = st.selectbox("Durum", ["Tümü", "Açık", "Atandı", "Bakımda", "Tamamlandı", "Geciken"], key="mrv2_status")
    with filter_cols[4]:
        request_search = st.text_input("Bakım talebi ara", placeholder="Talep no, makine veya açıklama...", key="mrv2_search")
    with filter_cols[5]:
        st.write("")
        if st.button("＋ Yeni Talep", type="primary", use_container_width=True, key="mrv2_new"):
            st.session_state["mr_new"] = True

    show_all_dates = st.checkbox("Tüm tarihleri göster", key="mrv2_all_dates")
    if not isinstance(request_dates, (tuple, list)):
        request_dates = (request_dates, request_dates)
    request_start, request_end = (request_dates[0], request_dates[-1]) if request_dates else (date.today(), date.today())
    data = records.copy()
    if not show_all_dates:
        data = data[data["_created"].between(pd.Timestamp(request_start), pd.Timestamp(request_end) + pd.Timedelta(days=1), inclusive="left")]
    if request_machine != "Tümü": data = data[data["machine_code"] == request_machine]
    if request_type != "Tümü": data = data[data["request_type"] == request_type]
    if request_status == "Geciken": data = data[data["_late"]]
    elif request_status != "Tümü": data = data[data["status"] == request_status]
    if request_search.strip():
        search_source = data["Talep No"] + " " + data["machine_code"].fillna("") + " " + data["description"].fillna("")
        data = data[search_source.str.contains(request_search.strip(), case=False, regex=False)]

    completed = data[data["status"] == "Tamamlandı"]
    ongoing = data[data["status"].isin(["Atandı", "Bakımda"])]
    late = data[data["_late"]]
    avg_hours = float(completed["_completion_hours"].dropna().mean()) if completed["_completion_hours"].notna().any() else None
    with metric_area:
        metric_cols = st.columns(5)
        metrics = [
            ("Toplam Bakım Talebi", len(data), "Seçili dönem", "#169b68"),
            ("Tamamlanan", len(completed), "Kapatılan talep", "#169b68"),
            ("Devam Eden", len(ongoing), "Atandı veya bakımda", "#3b9dbb"),
            ("Geciken", len(late), "24 saati aşan açık talep", "#e35f65"),
            ("Ortalama Tamamlama", f"{avg_hours:.1f} saat" if avg_hours is not None else "—", "Tamamlanan kayıtlar", "#15976a"),
        ]
        for column, (label, value, note, colour) in zip(metric_cols, metrics):
            column.markdown(f'<div class="mrv2-stat" style="--mrv2-color:{colour}"><small>{label}</small><b>{value}</b><span>{note}</span></div>', unsafe_allow_html=True)

    def title(text):
        st.markdown(f'<div class="mrv2-title">{text}</div>', unsafe_allow_html=True)

    def chart(figure, key, height=205):
        figure.update_layout(height=height, margin=dict(l=10, r=12, t=18, b=34), font=dict(size=10, color="#365b49"), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", legend=dict(orientation="h", font=dict(size=9)))
        st.plotly_chart(figure, use_container_width=True, config={"displayModeBar": False}, key=key)

    status_counts = data.assign(Gösterim=data.apply(lambda row: "Geciken" if row["_late"] else row["status"], axis=1)).groupby("Gösterim").size()
    type_counts = data.groupby("request_type").size().sort_values(ascending=False)
    machine_stats = data.groupby("machine_code", as_index=False).agg(**{"Toplam Talep": ("id", "count"), "Tamamlanan": ("status", lambda values: int((values == "Tamamlandı").sum())), "Devam Eden": ("status", lambda values: int(values.isin(["Atandı", "Bakımda"]).sum())), "Geciken": ("_late", "sum")})

    top_panels = st.columns([1, 1.1, 1.15], gap="small")
    with top_panels[0], st.container(border=True):
        title("Bakım Talebi Durumu")
        if status_counts.empty:
            st.caption("Talep kaydı bulunmuyor.")
        else:
            status_chart = go.Figure(go.Pie(labels=status_counts.index, values=status_counts.values, hole=.68, textinfo="none", marker_colors=["#159a63", "#3b9ec5", "#eeac3f", "#e36066", "#9abfaf"]))
            status_chart.add_annotation(x=.5, y=.5, text=f"<b>{len(data)}</b><br>Toplam talep", showarrow=False)
            chart(status_chart, "mrv2_status_chart")
    with top_panels[1], st.container(border=True):
        title("Bakım Türü Dağılımı")
        if type_counts.empty:
            st.caption("Bakım türü verisi bulunmuyor.")
        else:
            type_chart = go.Figure(go.Bar(x=type_counts.index, y=type_counts.values, text=type_counts.values, textposition="outside", marker_color="#169a67"))
            chart(type_chart, "mrv2_types")
    with top_panels[2], st.container(border=True):
        title("Makine Bazlı Bakım Durumu")
        if machine_stats.empty:
            st.caption("Makine bakım verisi bulunmuyor.")
        else:
            st.dataframe(machine_stats, hide_index=True, use_container_width=True, height=205)

    middle_panels = st.columns([1.55, .95, .9], gap="small")
    with middle_panels[0], st.container(border=True):
        title("Son Bakım Talepleri")
        request_view = data[["Talep No", "machine_code", "request_type", "description", "requested_at", "status"]].head(10).rename(columns={"machine_code":"Makine", "request_type":"Bakım Türü", "description":"Açıklama", "requested_at":"Tarih", "status":"Durum"}).fillna("—")
        st.dataframe(request_view, hide_index=True, use_container_width=True, height=245)
    with middle_panels[1], st.container(border=True):
        title("Bakım Takvimi")
        plan = maintenance.copy()
        plan["_next"] = pd.to_datetime(plan["next_date"], errors="coerce")
        if request_machine != "Tümü": plan = plan[plan["machine_code"] == request_machine]
        plan = plan[plan["_next"].notna()].sort_values("_next").head(7)
        st.dataframe(plan[["machine_code", "maintenance_type", "next_date"]].rename(columns={"machine_code":"Makine", "maintenance_type":"Bakım", "next_date":"Tarih"}), hide_index=True, use_container_width=True, height=245)
    with middle_panels[2], st.container(border=True):
        title("Bakım Performansı")
        completion_rate = len(completed) / max(len(data), 1) * 100
        on_time = completed[completed["_completion_hours"].le(24)]
        on_time_rate = len(on_time) / max(len(completed), 1) * 100
        performance_cols = st.columns(2)
        performance_cols[0].metric("Tamamlama", f"%{completion_rate:.1f}")
        performance_cols[1].metric("24 saat içinde", f"%{on_time_rate:.1f}")
        performance_cols[0].metric("Planlı bakım", f"{len(maintenance)}")
        performance_cols[1].metric("Ort. süre", f"{avg_hours:.1f} sa" if avg_hours is not None else "—")
        st.caption("Zamanında tamamlama ölçütü: talebin 24 saat içinde kapatılması.")

    filtered_downtime = downtime.copy()
    if not show_all_dates:
        filtered_downtime = filtered_downtime[filtered_downtime["_time"].between(pd.Timestamp(request_start), pd.Timestamp(request_end) + pd.Timedelta(days=1), inclusive="left")]
    if request_machine != "Tümü": filtered_downtime = filtered_downtime[filtered_downtime["machine_code"] == request_machine]
    bottom_panels = st.columns([1.2, 1, .9], gap="small")
    with bottom_panels[0], st.container(border=True):
        title("Duruş Süresine Etkisi")
        daily_stop = filtered_downtime.dropna(subset=["_time"]).groupby(filtered_downtime["_time"].dt.date)["duration"].sum()
        if daily_stop.empty:
            st.caption("Seçili dönemde tarihli duruş kaydı yok.")
        else:
            stop_chart = go.Figure(go.Scatter(x=daily_stop.index, y=daily_stop.values, mode="lines+markers", line=dict(color="#159a67", width=3), fill="tozeroy", fillcolor="rgba(21,154,103,.10)"))
            stop_chart.update_layout(yaxis_title="Dakika")
            chart(stop_chart, "mrv2_downtime")
    with bottom_panels[1], st.container(border=True):
        title("Bakım Türü Tamamlama Süreleri")
        duration_by_type = completed.dropna(subset=["_completion_hours"]).groupby("request_type")["_completion_hours"].mean().sort_values(ascending=False)
        if duration_by_type.empty:
            st.caption("Tamamlama süresi hesaplanabilecek kayıt yok.")
        else:
            maximum = max(float(duration_by_type.max()), 1)
            for label, value in duration_by_type.items():
                st.markdown(f'<div class="mrv2-bar"><span>{label}</span><div class="mrv2-track"><i style="width:{value / maximum * 100:.1f}%"></i></div><span>{value:.1f} saat</span></div>', unsafe_allow_html=True)
    with bottom_panels[2], st.container(border=True):
        title("Hızlı İşlemler")
        if st.button("＋ Yeni Bakım Talebi", type="primary", use_container_width=True, key="mrv2_quick_new"):
            st.session_state["mr_new"] = True
        if st.button("Bakım Planı Oluştur", use_container_width=True, key="mrv2_plan"):
            st.session_state["selected_module"] = "🔧 Bakım"
            st.rerun()
        report_view = data[["Talep No", "machine_code", "request_type", "description", "priority", "requested_at", "assigned_to", "status", "completed_at"]]
        st.download_button("Bakım Raporu · CSV", report_view.to_csv(index=False).encode("utf-8-sig"), "bakim_talepleri.csv", mime="text/csv", use_container_width=True)
        excel_output = io.BytesIO()
        with pd.ExcelWriter(excel_output, engine="openpyxl") as writer:
            report_view.to_excel(writer, sheet_name="Bakım Talepleri", index=False)
            machine_stats.to_excel(writer, sheet_name="Makine Özeti", index=False)
        st.download_button("Excel'e Aktar", excel_output.getvalue(), "bakim_talepleri.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)

    with st.container(border=True):
        title("Bakım Talebi Detayı")
        if data.empty:
            st.info("Seçili filtrede bakım talebi bulunmuyor.")
        else:
            labels = {int(row["id"]): f'{row["Talep No"]} · {row["machine_code"]} · {row["status"]}' for _, row in data.iterrows()}
            if st.session_state.get("mr_focus") not in labels:
                st.session_state.pop("mr_focus", None)
            selected_request = st.selectbox("Talep seçin", list(labels), format_func=labels.get, key="mr_focus")
            selected_row = data[data["id"] == selected_request].iloc[0]
            detail_cols = st.columns(4)
            detail_cols[0].metric("Makine", selected_row["machine_code"])
            detail_cols[1].metric("Öncelik", selected_row["priority"])
            detail_cols[2].metric("Durum", selected_row["status"])
            detail_cols[3].metric("Atanan", selected_row["assigned_to"] if pd.notna(selected_row["assigned_to"]) else "Atanmadı")
            st.write(selected_row["description"] or "Açıklama girilmemiş.")
            st.caption("Yetkili kullanıcılar aşağıdaki işlem alanından teknisyen atayabilir, bakımı başlatabilir veya tamamlayabilir.")
    return data
