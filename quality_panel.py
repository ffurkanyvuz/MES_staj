"""Kalite yönetim paneli; verileri mevcut kalite kayıtlarından üretir."""
from datetime import date, timedelta
import io

import pandas as pd
import plotly.graph_objects as go
import streamlit as st


def render_quality_panel(query):
    records = query("""
        SELECT q.id,q.machine_code,q.product,q.produced,q.defective,
               COALESCE(q.defect_reason,'Belirtilmemiş') AS defect_reason,q.timestamp,
               COALESCE(m.shift,'—') AS shift
        FROM quality q LEFT JOIN machines m ON m.machine_code=q.machine_code
        ORDER BY q.id DESC
    """)
    records["produced"] = pd.to_numeric(records["produced"], errors="coerce").fillna(0).clip(lower=0)
    records["defective"] = pd.to_numeric(records["defective"], errors="coerce").fillna(0).clip(lower=0)
    records["_time"] = pd.to_datetime(records["timestamp"], errors="coerce")
    records["_date"] = records["_time"].dt.date

    st.subheader("Kalite Yönetimi")
    st.markdown("""
    <style>
      .ql-stat{min-height:91px;background:linear-gradient(120deg,#fff,#eff9f5);border:1px solid #d6ebe2;
        border-left:4px solid var(--ql-color);border-radius:9px;padding:11px 13px;box-sizing:border-box}
      .ql-stat small{display:block;color:#49675a;font-size:11px;font-weight:700}.ql-stat b{display:block;color:#0b5239;font-size:24px;line-height:34px}
      .ql-stat span{font-size:10px;color:#688377}.ql-title{font-size:14px;font-weight:800;color:#12583e;margin-bottom:9px}
      .ql-alert{padding:8px 9px;margin:6px 0;border-left:3px solid var(--ql-alert);background:#fff;border-radius:5px;font-size:11px;color:#315744}
    </style>
    """, unsafe_allow_html=True)

    stat_area = st.container()
    filters = st.columns([1.45, 1, 1, 1, 1.5, .85])
    with filters[0]:
        chosen_dates = st.date_input("Tarih aralığı", value=(date.today() - timedelta(days=6), date.today()), key="quality_dates_v4")
    with filters[1]:
        chosen_machine = st.selectbox("Makine", ["Tümü"] + sorted(records["machine_code"].dropna().astype(str).unique().tolist()), key="quality_machine_v4")
    with filters[2]:
        chosen_product = st.selectbox("Ürün", ["Tümü"] + sorted(records["product"].dropna().astype(str).unique().tolist()), key="quality_product_v4")
    with filters[3]:
        chosen_shift = st.selectbox("Güncel vardiya", ["Tümü"] + sorted(records["shift"].dropna().astype(str).unique().tolist()), key="quality_shift_v4")
    with filters[4]:
        quality_search = st.text_input("Kalite kaydı ara", placeholder="Ürün, hata veya makine...", key="quality_search_v4")
    with filters[5]:
        st.write("")
        if st.button("＋ Yeni Kayıt", type="primary", use_container_width=True, key="quality_new_v4"):
            st.session_state["quality_new_open"] = True

    show_all_quality = st.checkbox("Tüm tarihleri göster", key="quality_all_dates_v4")
    if not isinstance(chosen_dates, (tuple, list)):
        chosen_dates = (chosen_dates, chosen_dates)
    quality_start, quality_end = (chosen_dates[0], chosen_dates[-1]) if chosen_dates else (date.today(), date.today())
    data = records.copy()
    if not show_all_quality:
        data = data[data["_time"].between(pd.Timestamp(quality_start), pd.Timestamp(quality_end) + pd.Timedelta(days=1), inclusive="left")]
    if chosen_machine != "Tümü": data = data[data["machine_code"] == chosen_machine]
    if chosen_product != "Tümü": data = data[data["product"] == chosen_product]
    if chosen_shift != "Tümü": data = data[data["shift"] == chosen_shift]
    if quality_search.strip():
        search_text = data["machine_code"].fillna("").astype(str) + " " + data["product"].fillna("").astype(str) + " " + data["defect_reason"].fillna("").astype(str)
        data = data[search_text.str.contains(quality_search.strip(), case=False, regex=False)]

    total_produced = float(data["produced"].sum())
    total_defective = min(float(data["defective"].sum()), total_produced) if total_produced else float(data["defective"].sum())
    total_good = max(total_produced - total_defective, 0)
    quality_rate = total_good / max(total_produced, 1) * 100
    defect_rate = total_defective / max(total_produced, 1) * 100
    with stat_area:
        stat_cols = st.columns(5)
        stats = [
            ("Toplam Üretim", f"{int(total_produced):,}", "Seçili kayıtlar", "#169b68"),
            ("Sağlam Üretim", f"{int(total_good):,}", "Hatasız adet", "#169b68"),
            ("Hatalı Üretim", f"{int(total_defective):,}", "Kayıtlı hata", "#e45e65"),
            ("Kalite Oranı", f"%{quality_rate:.1f}", "Sağlam / toplam", "#169b68"),
            ("Hata Oranı", f"%{defect_rate:.1f}", "Hatalı / toplam", "#e3a135"),
        ]
        for column, (label, value, note, colour) in zip(stat_cols, stats):
            column.markdown(f'<div class="ql-stat" style="--ql-color:{colour}"><small>{label}</small><b>{value}</b><span>{note}</span></div>', unsafe_allow_html=True)

    def title(text):
        st.markdown(f'<div class="ql-title">{text}</div>', unsafe_allow_html=True)

    def show_chart(figure, key, height=215):
        figure.update_layout(height=height, margin=dict(l=10, r=12, t=18, b=34), font=dict(size=10, color="#365b49"), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", legend=dict(orientation="h", font=dict(size=9)))
        st.plotly_chart(figure, use_container_width=True, config={"displayModeBar": False}, key=key)

    defect_data = data[data["defective"] > 0].copy()
    reason_totals = defect_data.groupby("defect_reason")["defective"].sum().sort_values(ascending=False)
    machine_summary = data.groupby("machine_code", as_index=False).agg(Üretim=("produced", "sum"), Hatalı=("defective", "sum"))
    machine_summary["Kalite"] = ((machine_summary["Üretim"] - machine_summary["Hatalı"]) / machine_summary["Üretim"].replace(0, 1) * 100).clip(0, 100)
    machine_summary["Hata"] = (machine_summary["Hatalı"] / machine_summary["Üretim"].replace(0, 1) * 100).clip(0, 100)

    top_row = st.columns([1, 1.18, 1.15], gap="small")
    with top_row[0], st.container(border=True):
        title("Hata Türleri Dağılımı")
        if reason_totals.empty:
            st.success("Seçili filtrede hata kaydı yok.")
        else:
            donut = go.Figure(go.Pie(labels=reason_totals.index, values=reason_totals.values, hole=.67, textinfo="none", marker_colors=["#ee565c", "#f1a72d", "#349bc7", "#8b6ad3", "#54b889"]))
            donut.add_annotation(x=.5, y=.5, text=f"<b>{int(reason_totals.sum())}</b><br>Toplam hata", showarrow=False)
            show_chart(donut, "quality_reason_donut_v4")
    with top_row[1], st.container(border=True):
        title("Makine Bazlı Kalite Oranı")
        if machine_summary.empty:
            st.caption("Makine kalite verisi yok.")
        else:
            comparison = go.Figure()
            comparison.add_trace(go.Bar(x=machine_summary["machine_code"], y=machine_summary["Kalite"], name="Kalite Oranı", marker_color="#149a63", text=machine_summary["Kalite"].map(lambda value: f"%{value:.1f}"), textposition="outside"))
            comparison.add_trace(go.Bar(x=machine_summary["machine_code"], y=machine_summary["Hata"], name="Hata Oranı", marker_color="#e56065", text=machine_summary["Hata"].map(lambda value: f"%{value:.1f}"), textposition="outside"))
            comparison.update_layout(barmode="group", yaxis=dict(range=[0, 108], ticksuffix="%"))
            show_chart(comparison, "quality_machine_comparison_v4")
    with top_row[2], st.container(border=True):
        title("Kalite Oranı Trendi")
        daily = data.dropna(subset=["_time"]).groupby(data["_time"].dt.date, as_index=False).agg(Üretim=("produced", "sum"), Hatalı=("defective", "sum"))
        if daily.empty:
            st.caption("Tarih bazlı kalite kaydı yok.")
        else:
            daily["Kalite"] = ((daily["Üretim"] - daily["Hatalı"]) / daily["Üretim"].replace(0, 1) * 100).clip(0, 100)
            daily["Hata"] = (daily["Hatalı"] / daily["Üretim"].replace(0, 1) * 100).clip(0, 100)
            trend = go.Figure()
            trend.add_trace(go.Scatter(x=daily["_time"], y=daily["Kalite"], name="Kalite Oranı", mode="lines+markers", line=dict(color="#159a63", width=3)))
            trend.add_trace(go.Scatter(x=daily["_time"], y=daily["Hata"], name="Hata Oranı", mode="lines+markers", line=dict(color="#e56065", width=2)))
            trend.update_layout(yaxis=dict(ticksuffix="%"))
            show_chart(trend, "quality_daily_trend_v4")

    middle_row = st.columns([1.05, 1.1, 1], gap="small")
    with middle_row[0], st.container(border=True):
        title("Ürün Bazlı Kalite Analizi")
        product_summary = data.groupby("product", as_index=False).agg(**{"Toplam Üretim": ("produced", "sum"), "Hatalı": ("defective", "sum")})
        product_summary["Sağlam"] = (product_summary["Toplam Üretim"] - product_summary["Hatalı"]).clip(lower=0)
        product_summary["Kalite Oranı"] = (product_summary["Sağlam"] / product_summary["Toplam Üretim"].replace(0, 1) * 100).clip(0, 100).map(lambda value: f"%{value:.1f}")
        st.dataframe(product_summary[["product", "Toplam Üretim", "Sağlam", "Hatalı", "Kalite Oranı"]].rename(columns={"product": "Ürün"}), hide_index=True, use_container_width=True, height=215)
    with middle_row[1], st.container(border=True):
        title("Son Kalite Kayıtları")
        recent = data.head(8)[["timestamp", "machine_code", "product", "defect_reason", "defective"]].rename(columns={"timestamp":"Tarih", "machine_code":"Makine", "product":"Ürün", "defect_reason":"Hata Türü", "defective":"Adet"})
        st.dataframe(recent, hide_index=True, use_container_width=True, height=215)
    with middle_row[2], st.container(border=True):
        title("Kalite Uyarıları")
        warning_machines = machine_summary[(machine_summary["Kalite"] < 95) | (machine_summary["Hatalı"] > 0)].sort_values("Kalite")
        if warning_machines.empty:
            st.success("Seçili filtrede kalite uyarısı yok.")
        else:
            for _, warning in warning_machines.head(6).iterrows():
                alert_colour = "#e35d64" if warning["Kalite"] < 90 else "#e9a338"
                st.markdown(f'<div class="ql-alert" style="--ql-alert:{alert_colour}"><b>{warning["machine_code"]}</b> · Kalite %{warning["Kalite"]:.1f}<br>{int(warning["Hatalı"])} hatalı parça</div>', unsafe_allow_html=True)

    bottom_row = st.columns([1.15, 1, 1, .9], gap="small")
    with bottom_row[0], st.container(border=True):
        title("Hata Dağılımı · Pareto")
        if reason_totals.empty:
            st.caption("Pareto analizi için hata kaydı yok.")
        else:
            pareto = go.Figure(go.Bar(x=reason_totals.index, y=reason_totals.values, marker_color="#159a63", name="Hatalı adet"))
            pareto.add_trace(go.Scatter(x=reason_totals.index, y=reason_totals.cumsum() / max(reason_totals.sum(), 1) * 100, yaxis="y2", mode="lines+markers", name="Kümülatif %", line=dict(color="#285f6e")))
            pareto.update_layout(yaxis2=dict(overlaying="y", side="right", range=[0, 110], ticksuffix="%"))
            show_chart(pareto, "quality_pareto_v4")
    with bottom_row[1], st.container(border=True):
        title("Kalite Oranı Dağılımı")
        quality_donut = go.Figure(go.Pie(labels=["Sağlam", "Hatalı"], values=[total_good, total_defective], hole=.7, textinfo="none", marker_colors=["#159a63", "#e56065"]))
        quality_donut.add_annotation(x=.5, y=.5, text=f"<b>%{quality_rate:.1f}</b><br>Toplam kalite", showarrow=False)
        show_chart(quality_donut, "quality_total_donut_v4")
    with bottom_row[2], st.container(border=True):
        title("En Fazla Hata Olan Makineler")
        ranking = machine_summary.sort_values("Hatalı", ascending=False).head(5)
        if ranking.empty:
            st.caption("Makine hata kaydı yok.")
        else:
            ranking_chart = go.Figure(go.Bar(y=ranking["machine_code"], x=ranking["Hatalı"], orientation="h", marker_color="#eda733", text=ranking["Hatalı"], textposition="outside"))
            show_chart(ranking_chart, "quality_machine_ranking_v4")
    with bottom_row[3], st.container(border=True):
        title("Hızlı İşlemler")
        if st.button("＋ Yeni Kalite Kaydı", type="primary", use_container_width=True, key="quality_quick_new_v4"):
            st.session_state["quality_new_open"] = True
        report_view = data[["timestamp", "machine_code", "product", "produced", "defective", "defect_reason", "shift"]].copy()
        st.download_button("Hata Raporu · CSV", report_view.to_csv(index=False).encode("utf-8-sig"), "kalite_raporu.csv", mime="text/csv", use_container_width=True)
        excel_output = io.BytesIO()
        with pd.ExcelWriter(excel_output, engine="openpyxl") as writer:
            report_view.to_excel(writer, sheet_name="Kalite Kayıtları", index=False)
            product_summary.to_excel(writer, sheet_name="Ürün Analizi", index=False)
        st.download_button("Excel'e Aktar", excel_output.getvalue(), "kalite_raporu.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)

