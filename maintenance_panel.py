"""Planlı ve tamamlanmış bakım kayıtları için yönetici paneli."""
from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st


def render_maintenance_panel(query):
    records = query("""SELECT id,machine_id,machine_code,maintenance_type,description,
        maintenance_date,next_date,technician,status FROM maintenance ORDER BY next_date""")
    machines = query("SELECT machine_code,status,last_maintenance,next_maintenance FROM machines ORDER BY machine_code")
    records["_maintenance"] = pd.to_datetime(records["maintenance_date"], errors="coerce")
    records["_next"] = pd.to_datetime(records["next_date"], errors="coerce")
    records["_late"] = records["status"].ne("Tamamlandı") & records["_next"].notna() & records["_next"].lt(pd.Timestamp(date.today()))
    records["_upcoming"] = records["status"].ne("Tamamlandı") & records["_next"].between(pd.Timestamp(date.today()), pd.Timestamp(date.today() + timedelta(days=7)))
    records["Bakım Durumu"] = "Planlı"
    records.loc[records["_upcoming"], "Bakım Durumu"] = "Yaklaşıyor"
    records.loc[records["_late"], "Bakım Durumu"] = "Gecikmiş"
    records.loc[records["status"].eq("Tamamlandı"), "Bakım Durumu"] = "Tamamlandı"

    st.subheader("Bakım Yönetimi")
    st.markdown("""
    <style>
      .mnt-stat{min-height:88px;background:linear-gradient(120deg,#fff,#eef9f5);border:1px solid #d5e9e0;
        border-left:4px solid var(--mnt-color);border-radius:9px;padding:11px 13px;box-sizing:border-box}
      .mnt-stat small{display:block;color:#48675a;font-size:11px;font-weight:750}.mnt-stat b{display:block;color:#07543a;font-size:23px;line-height:33px}
      .mnt-stat span{font-size:9px;color:#6b8379}.mnt-title{font-size:14px;color:#10583e;font-weight:800;margin-bottom:9px}
      .mnt-row{display:grid;grid-template-columns:58px minmax(0,1fr) 76px;gap:8px;align-items:center;
        border-bottom:1px solid #e1eee7;padding:8px 1px;font-size:10px;color:#355e4b}
      .mnt-row b{font-size:10px}.mnt-pill{justify-self:end;padding:3px 7px;border-radius:12px;background:#e1f4ea;color:#08784d;font-size:9px;font-weight:700}
      .mnt-pill.warn{background:#fff0d5;color:#ae6900}.mnt-pill.late{background:#ffe2e5;color:#d43845}
      .mnt-progress{height:8px;background:#e3f0e9;border-radius:10px;overflow:hidden}.mnt-progress i{display:block;height:100%;background:#149563;border-radius:10px}
    </style>
    """, unsafe_allow_html=True)

    metric_area = st.container()
    filters = st.columns([1.4, 1, 1, 1.05, 1.55, .85])
    with filters[0]:
        selected_dates = st.date_input("Tarih aralığı", value=(date.today() - timedelta(days=30), date.today()), key="mnt_dates_v3")
    with filters[1]:
        selected_machine = st.selectbox("Makine", ["Tümü"] + sorted(records["machine_code"].dropna().astype(str).unique().tolist()), key="mnt_machine_v3")
    with filters[2]:
        selected_type = st.selectbox("Bakım türü", ["Tümü"] + sorted(records["maintenance_type"].dropna().astype(str).unique().tolist()), key="mnt_type_v3")
    with filters[3]:
        selected_status = st.selectbox("Durum", ["Tümü", "Planlı", "Bakımda", "Tamamlandı", "Yaklaşıyor", "Gecikmiş"], key="mnt_status_v3")
    with filters[4]:
        search = st.text_input("Bakım kaydı ara", placeholder="Makine, açıklama veya teknisyen...", key="mnt_search_v3")
    with filters[5]:
        st.write("")
        if st.button("＋ Yeni Bakım", type="primary", use_container_width=True, key="mnt_new_v3"):
            st.session_state["maintenance_new_open"] = True

    if not isinstance(selected_dates, (tuple, list)):
        selected_dates = (selected_dates, selected_dates)
    start, end = (selected_dates[0], selected_dates[-1]) if selected_dates else (date.today(), date.today())
    data = records[records["_maintenance"].between(pd.Timestamp(start), pd.Timestamp(end) + pd.Timedelta(days=1), inclusive="left")].copy()
    if selected_machine != "Tümü": data = data[data["machine_code"] == selected_machine]
    if selected_type != "Tümü": data = data[data["maintenance_type"] == selected_type]
    if selected_status == "Gecikmiş": data = data[data["_late"]]
    elif selected_status == "Yaklaşıyor": data = data[data["_upcoming"]]
    elif selected_status == "Planlı": data = data[data["status"].isin(["Planlı", "Planlandı"])]
    elif selected_status != "Tümü": data = data[data["status"] == selected_status]
    if search.strip():
        source = data["machine_code"].fillna("").astype(str) + " " + data["description"].fillna("").astype(str) + " " + data["technician"].fillna("").astype(str)
        data = data[source.str.contains(search.strip(), case=False, regex=False)]

    completed = records[records["status"] == "Tamamlandı"]
    active = records[records["status"].isin(["Planlandı", "Planlı", "Bakımda"])]
    with metric_area:
        cols = st.columns(5)
        stats = [
            ("Toplam Bakım", len(records), "Tüm kayıtlar", "#168e63"),
            ("Tamamlanan", len(completed), "Bakımı biten", "#168e63"),
            ("Devam Eden", len(active), "Planlı ve bakımda", "#168e63"),
            ("Geciken", int(records["_late"].sum()), "Tarihi geçen", "#ef4b55"),
            ("7 Gün İçinde", int(records["_upcoming"].sum()), "Yaklaşan bakım", "#f0a11a"),
        ]
        for col, (label, value, note, colour) in zip(cols, stats):
            col.markdown(f'<div class="mnt-stat" style="--mnt-color:{colour}"><small>{label}</small><b>{value}</b><span>{note}</span></div>', unsafe_allow_html=True)

    def title(text):
        st.markdown(f'<div class="mnt-title">{text}</div>', unsafe_allow_html=True)

    def chart(fig, key, height=218):
        fig.update_layout(height=height, margin=dict(l=8, r=10, t=14, b=30), font=dict(size=10, color="#365e4c"),
                          paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", legend=dict(orientation="h", font=dict(size=9)))
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False}, key=key)

    row1 = st.columns([1, 1, 1], gap="small")
    with row1[0], st.container(border=True):
        title("Bakım Durumu")
        status_counts = records["Bakım Durumu"].value_counts()
        if status_counts.empty: st.info("Bakım kaydı yok.")
        else:
            fig = go.Figure(go.Pie(labels=status_counts.index, values=status_counts.values, hole=.62,
                                   marker_colors=["#148c5e", "#35bd82", "#f2ac24", "#ef4d55"]))
            fig.add_annotation(text=f"<b>{len(records)}</b><br><span style='font-size:9px'>bakım</span>", showarrow=False)
            chart(fig, "mnt_status_chart")
    with row1[1], st.container(border=True):
        title("Bakım Türü Dağılımı")
        types = records["maintenance_type"].fillna("Belirsiz").value_counts().sort_values(ascending=False)
        if types.empty: st.info("Bakım kaydı yok.")
        else:
            fig = go.Figure(go.Bar(x=types.index, y=types.values, marker_color=["#118b5c", "#26ad76", "#5bc18f", "#9bd9ba"], text=types.values, textposition="outside"))
            chart(fig, "mnt_type_chart")
    with row1[2], st.container(border=True):
        title("Makine Bazlı Bakım Durumu")
        if machines.empty:
            st.info("Makine kaydı yok.")
        else:
            counts = records.groupby("machine_code").size().to_dict()
            for machine in machines.itertuples():
                count = int(counts.get(machine.machine_code, 0))
                colour_class = "late" if str(machine.status) == "Arızalı" else ("warn" if str(machine.status) == "Beklemede" else "")
                st.markdown(f'<div class="mnt-row"><b>{machine.machine_code}</b><span>{count} bakım kaydı<br>Son: {machine.last_maintenance or "—"}</span><i class="mnt-pill {colour_class}">{machine.status}</i></div>', unsafe_allow_html=True)

    row2 = st.columns([1.65, .95, .85], gap="small")
    with row2[0], st.container(border=True):
        title("Bakım Kayıtları")
        table = data.copy()
        table["description"] = table["description"].fillna("—").replace("", "—")
        table["technician"] = table["technician"].fillna("Atanmadı")
        table["maintenance_date"] = table["_maintenance"].dt.strftime("%d.%m.%Y").fillna("—")
        table["next_date"] = table["_next"].dt.strftime("%d.%m.%Y").fillna("—")
        table = table.rename(columns={"id":"#", "machine_code":"Makine", "maintenance_type":"Bakım Türü", "description":"Açıklama",
                                      "maintenance_date":"Bakım Tarihi", "next_date":"Sonraki Bakım", "technician":"Teknisyen", "status":"Durum"})
        st.dataframe(table[["#", "Makine", "Bakım Türü", "Açıklama", "Bakım Tarihi", "Sonraki Bakım", "Teknisyen", "Durum", "Bakım Durumu"]],
                     hide_index=True, use_container_width=True, height=286)
    with row2[1], st.container(border=True):
        title("Yaklaşan Bakımlar")
        upcoming = records[records["_next"].notna() & records["status"].ne("Tamamlandı")].sort_values("_next").head(7)
        if upcoming.empty: st.success("Yaklaşan bakım yok.")
        for _, item in upcoming.iterrows():
            days = (item["_next"].date() - date.today()).days
            css = "late" if days < 0 else ("warn" if days <= 7 else "")
            label = f"{abs(days)} gün gecikti" if days < 0 else ("Bugün" if days == 0 else f"{days} gün")
            st.markdown(f'<div class="mnt-row"><b>{item["machine_code"]}</b><span>{item["maintenance_type"]}<br>{item["_next"].strftime("%d.%m.%Y")}</span><i class="mnt-pill {css}">{label}</i></div>', unsafe_allow_html=True)
    with row2[2], st.container(border=True):
        title("Hızlı İşlemler")
        if st.button("＋ Yeni Bakım", type="primary", use_container_width=True, key="mnt_quick_new"):
            st.session_state["maintenance_new_open"] = True
            st.rerun()
        if st.button("⚙ Bakımı Güncelle", type="primary", use_container_width=True, key="mnt_quick_action"):
            st.session_state["maintenance_action_open"] = True
            st.rerun()
        export = data.drop(columns=["_maintenance", "_next", "_late", "_upcoming"], errors="ignore").to_csv(index=False).encode("utf-8-sig")
        st.download_button("⇩ Bakım Raporu", export, "bakim-raporu.csv", "text/csv", use_container_width=True, key="mnt_export")
        planned_ratio = len(completed) / max(len(records), 1) * 100
        st.caption("Tamamlanma oranı")
        st.markdown(f'<div class="mnt-progress"><i style="width:{planned_ratio:.1f}%"></i></div><small>%{planned_ratio:.1f}</small>', unsafe_allow_html=True)

    return records
