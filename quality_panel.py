"""TREX MES kalite yönetimi: genel görünüm, hata, SPC ve Pareto analizleri."""
from datetime import date, timedelta
import io
import uuid

import pandas as pd
import plotly.graph_objects as go
import streamlit as st


GREEN = "#0b9257"
RED = "#e65258"
AMBER = "#eea629"
BLUE = "#349bc7"


def _chart(figure, key, height=285):
    figure.update_layout(
        height=height, margin=dict(l=12, r=12, t=28, b=34),
        font=dict(size=10, color="#365b49"), paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)", legend=dict(orientation="h", y=1.12),
    )
    st.plotly_chart(figure, use_container_width=True, config={"displayModeBar": False}, key=key)


def _metric_cards(items):
    columns = st.columns(len(items), gap="small")
    for column, (label, value, note, colour) in zip(columns, items):
        column.markdown(
            f'<div class="ql-stat" style="--ql-color:{colour}"><small>{label}</small>'
            f'<b>{value}</b><span>{note}</span></div>', unsafe_allow_html=True,
        )


def _quality_summaries(data):
    total = float(data["produced"].sum())
    defective = min(float(data["defective"].sum()), total) if total else float(data["defective"].sum())
    good = max(total - defective, 0)
    rate = good / max(total, 1) * 100
    machine = data.groupby("machine_code", as_index=False).agg(
        **{"Toplam Üretim": ("produced", "sum"), "Hatalı": ("defective", "sum")}
    )
    machine["Uygun"] = (machine["Toplam Üretim"] - machine["Hatalı"]).clip(lower=0)
    machine["Kalite Oranı"] = (machine["Uygun"] / machine["Toplam Üretim"].replace(0, 1) * 100).clip(0, 100)
    reasons = data[data["defective"] > 0].groupby("defect_reason")["defective"].sum().sort_values(ascending=False)
    return total, good, defective, rate, machine, reasons


def render_quality_panel(query, execute, machines, has_role):
    records = query("""
        SELECT q.id,q.machine_code,q.product,q.produced,q.defective,
               COALESCE(q.defect_reason,'Belirtilmemiş') AS defect_reason,q.timestamp,
               COALESCE(m.shift,'—') AS shift
        FROM quality q LEFT JOIN machines m ON m.machine_code=q.machine_code
        ORDER BY q.id DESC
    """)
    for column in ["produced", "defective"]:
        records[column] = pd.to_numeric(records[column], errors="coerce").fillna(0).clip(lower=0)
    records["_time"] = pd.to_datetime(records["timestamp"], errors="coerce")

    st.markdown("""
    <style>
    .ql-stat{min-height:78px;background:linear-gradient(125deg,#fff,#eff9f4);border:1px solid #d6ebe2;border-left:4px solid var(--ql-color);border-radius:9px;padding:9px 12px;box-sizing:border-box}
    .ql-stat small{display:block;color:#49675a;font-size:10px;font-weight:800}.ql-stat b{display:block;color:#0b5239;font-size:21px;line-height:29px}.ql-stat span{font-size:9px;color:#688377}
    .ql-panel-title{font-size:13px;font-weight:850;color:#12583e;margin-bottom:7px}.spc-state{padding:11px 14px;border-radius:9px;border:1px solid var(--state-border);background:var(--state-bg);color:var(--state-text);font-weight:850;margin:5px 0 10px}.spc-note{font-size:11px;color:#638071}
    div[data-baseweb="tab-list"]{gap:5px;border-bottom:1px solid #d9ece2}button[data-baseweb="tab"]{height:40px;padding:0 17px;border-radius:8px 8px 0 0;font-weight:750}
    </style>
    """, unsafe_allow_html=True)

    filter_columns = st.columns([1.35, 1, 1, 1.35, .78], gap="small")
    with filter_columns[0]:
        selected_dates = st.date_input("Tarih aralığı", (date.today() - timedelta(days=30), date.today()), key="quality_dates_spc")
    with filter_columns[1]:
        selected_machine = st.selectbox("Makine", ["Tümü"] + sorted(records["machine_code"].dropna().astype(str).unique().tolist()), key="quality_machine_spc")
    with filter_columns[2]:
        selected_product = st.selectbox("Ürün", ["Tümü"] + sorted(records["product"].dropna().astype(str).unique().tolist()), key="quality_product_spc")
    with filter_columns[3]:
        search = st.text_input("Kayıt ara", placeholder="Makine, ürün veya hata...", key="quality_search_spc")
    with filter_columns[4]:
        st.write("")
        if st.button("Yeni Kayıt", type="primary", use_container_width=True, key="quality_new_spc"):
            st.session_state["quality_new_open"] = True

    if not isinstance(selected_dates, (tuple, list)):
        selected_dates = (selected_dates, selected_dates)
    start_date, end_date = selected_dates[0], selected_dates[-1]
    data = records[records["_time"].between(pd.Timestamp(start_date), pd.Timestamp(end_date) + pd.Timedelta(days=1), inclusive="left")].copy()
    if selected_machine != "Tümü":
        data = data[data["machine_code"] == selected_machine]
    if selected_product != "Tümü":
        data = data[data["product"] == selected_product]
    if search.strip():
        haystack = data[["machine_code", "product", "defect_reason"]].fillna("").astype(str).agg(" ".join, axis=1)
        data = data[haystack.str.contains(search.strip(), case=False, regex=False)]

    total, good, defective, quality_rate, machine_summary, reason_totals = _quality_summaries(data)
    _metric_cards([
        ("Kontrol Edilen", f"{int(total):,}", "Seçili dönem", GREEN),
        ("Uygun Ürün", f"{int(good):,}", "Hatasız adet", GREEN),
        ("Hatalı Ürün", f"{int(defective):,}", "Toplam hata", RED),
        ("Kalite Oranı", f"%{quality_rate:.1f}", "Uygun / toplam", GREEN),
        ("Aktif Makine", f"{machine_summary['machine_code'].nunique()}", "Kalite kaydı olan", BLUE),
    ])

    overview_tab, defect_tab, spc_tab, pareto_tab = st.tabs(["Genel Bakış", "Hata Analizi", "SPC Analizi", "Pareto Analizi"])

    with overview_tab:
        left, right = st.columns([1.15, 1], gap="small")
        with left, st.container(border=True):
            st.markdown('<div class="ql-panel-title">Makine Bazında Kalite Oranı</div>', unsafe_allow_html=True)
            if machine_summary.empty:
                st.info("Seçili filtrelerde kalite verisi yok.")
            else:
                figure = go.Figure(go.Bar(x=machine_summary["machine_code"], y=machine_summary["Kalite Oranı"], marker_color=GREEN, text=machine_summary["Kalite Oranı"].map(lambda x: f"%{x:.1f}"), textposition="outside"))
                figure.update_yaxes(range=[0, 105], ticksuffix="%")
                _chart(figure, "quality_machine_rate_spc")
        with right, st.container(border=True):
            st.markdown('<div class="ql-panel-title">Makine Kalite Özeti</div>', unsafe_allow_html=True)
            table = machine_summary.rename(columns={"machine_code": "Makine"}).copy()
            table["Kalite Oranı"] = table["Kalite Oranı"].map(lambda x: f"%{x:.1f}")
            st.dataframe(table[["Makine", "Toplam Üretim", "Uygun", "Hatalı", "Kalite Oranı"]], hide_index=True, use_container_width=True, height=310)

    with defect_tab:
        top_defect = reason_totals.index[0] if not reason_totals.empty else "—"
        defect_columns = st.columns(3)
        defect_columns[0].metric("Toplam Hata", f"{int(reason_totals.sum()):,}")
        defect_columns[1].metric("En Çok Görülen Hata", top_defect)
        defect_columns[2].metric("Hata Türü Sayısı", len(reason_totals))
        defect_left, defect_right = st.columns(2, gap="small")
        with defect_left, st.container(border=True):
            st.markdown('<div class="ql-panel-title">Hata Türlerine Göre Dağılım</div>', unsafe_allow_html=True)
            if reason_totals.empty:
                st.success("Seçili dönemde hata yok.")
            else:
                donut = go.Figure(go.Pie(labels=reason_totals.index, values=reason_totals.values, hole=.62, marker_colors=[RED, AMBER, BLUE, "#8b6ad3", "#54b889"]))
                _chart(donut, "quality_defect_donut_spc")
        with defect_right, st.container(border=True):
            st.markdown('<div class="ql-panel-title">Makine Bazında Hata</div>', unsafe_allow_html=True)
            ranking = machine_summary.sort_values("Hatalı")
            if ranking.empty:
                st.caption("Makine hata kaydı yok.")
            else:
                _chart(go.Figure(go.Bar(x=ranking["Hatalı"], y=ranking["machine_code"], orientation="h", marker_color=RED, text=ranking["Hatalı"])), "quality_defect_machine_spc")

    with spc_tab:
        spc_records = query("""
            SELECT id,machine_id,machine_code,product,measurement_name,measurement_value,
                   unit,timestamp,spec_low,spec_high
            FROM spc_measurements ORDER BY timestamp,id
        """)
        spc_records["measurement_value"] = pd.to_numeric(spc_records["measurement_value"], errors="coerce")
        spc_records["timestamp"] = pd.to_datetime(spc_records["timestamp"], errors="coerce")
        spc_records = spc_records.dropna(subset=["measurement_value", "timestamp"])

        if st.session_state.get("spc_new_open", False):
            with st.container(border=True):
                st.markdown('<div class="ql-panel-title">Yeni SPC Ölçümü</div>', unsafe_allow_html=True)
                if not has_role("admin", "quality"):
                    st.info("Ölçüm girişi için kalite veya yönetici yetkisi gerekir.")
                elif machines.empty:
                    st.warning("Ölçüm bağlanabilecek makine yok.")
                else:
                    with st.form("spc_measurement_form"):
                        form_cols = st.columns(4)
                        machine_code = form_cols[0].selectbox("Makine", machines["machine_code"].tolist())
                        machine_row = machines[machines["machine_code"] == machine_code].iloc[0]
                        product = form_cols[1].text_input("Ürün", str(machine_row["product"] or "Tanımsız Ürün"))
                        characteristic = form_cols[2].text_input("Karakteristik", "Çap")
                        unit = form_cols[3].text_input("Birim", "mm")
                        value_cols = st.columns(3)
                        measurement_value = value_cols[0].number_input("Ölçüm değeri", value=25.0, format="%.4f")
                        spec_low = value_cols[1].number_input("LSL", value=24.90, format="%.4f")
                        spec_high = value_cols[2].number_input("USL", value=25.10, format="%.4f")
                        saved = st.form_submit_button("Ölçümü Kaydet", type="primary", use_container_width=True)
                    if saved:
                        if spec_low >= spec_high:
                            st.error("LSL, USL değerinden küçük olmalıdır.")
                        else:
                            execute("""INSERT INTO spc_measurements(id,machine_id,machine_code,product,measurement_name,measurement_value,unit,timestamp,spec_low,spec_high) VALUES(?,?,?,?,?,?,?,?,?,?)""", (int(uuid.uuid4().int % 2_000_000_000) or 1, int(machine_row["id"]), machine_code, product.strip(), characteristic.strip(), float(measurement_value), unit.strip(), pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"), float(spec_low), float(spec_high)))
                            st.session_state["spc_new_open"] = False
                            st.session_state["spc_saved"] = True
                            st.rerun()
                if st.button("İptal", key="spc_measurement_cancel"):
                    st.session_state["spc_new_open"] = False
                    st.rerun()
        if st.session_state.pop("spc_saved", False):
            st.success("SPC ölçümü kaydedildi.")

        spc_filter_cols = st.columns([1, 1, 1, .65], gap="small")
        machine_options = sorted(spc_records["machine_code"].astype(str).unique().tolist())
        if not machine_options:
            st.info("Henüz SPC ölçümü yok. Yeni Ölçüm ile ilk kaydı oluşturabilirsin.")
        else:
            spc_machine = spc_filter_cols[0].selectbox("SPC Makine", machine_options, key="spc_machine")
            machine_spc = spc_records[spc_records["machine_code"] == spc_machine]
            spc_product = spc_filter_cols[1].selectbox("SPC Ürün", sorted(machine_spc["product"].astype(str).unique()), key="spc_product")
            product_spc = machine_spc[machine_spc["product"] == spc_product]
            spc_characteristic = spc_filter_cols[2].selectbox("Karakteristik", sorted(product_spc["measurement_name"].astype(str).unique()), key="spc_characteristic")
            with spc_filter_cols[3]:
                st.write("")
                if st.button("Yeni Ölçüm", type="primary", use_container_width=True, key="spc_new_button"):
                    st.session_state["spc_new_open"] = True
                    st.rerun()
            spc_view = product_spc[product_spc["measurement_name"] == spc_characteristic].sort_values("timestamp").tail(100).copy()
            values = spc_view["measurement_value"]
            center = float(values.mean())
            deviation = float(values.std(ddof=1)) if len(values) > 1 else 0.0
            ucl, lcl = center + 3 * deviation, center - 3 * deviation
            spec_low = float(spc_view["spec_low"].dropna().iloc[-1]) if spc_view["spec_low"].notna().any() else None
            spec_high = float(spc_view["spec_high"].dropna().iloc[-1]) if spc_view["spec_high"].notna().any() else None
            outside = (values > ucl) | (values < lcl)
            if spec_low is not None:
                outside |= values < spec_low
            if spec_high is not None:
                outside |= values > spec_high
            outside_count = int(outside.sum())
            state_style = ("#ffe9e9", "#efbcbc", "#a3272d") if outside_count else ("#e9f8ef", "#bfe6ce", "#087746")
            state_text = f"Proses kontrol dışında · {outside_count} ölçüm limit dışı" if outside_count else "Proses kontrol altında"
            st.markdown(f'<div class="spc-state" style="--state-bg:{state_style[0]};--state-border:{state_style[1]};--state-text:{state_style[2]}">{state_text}</div>', unsafe_allow_html=True)
            _metric_cards([("Ortalama / CL", f"{center:.3f}", spc_view["unit"].iloc[-1], GREEN), ("UCL", f"{ucl:.3f}", "+3σ", AMBER), ("LCL", f"{lcl:.3f}", "−3σ", AMBER), ("Kontrol Dışı", str(outside_count), f"{len(spc_view)} ölçüm", RED if outside_count else GREEN), ("Standart Sapma", f"{deviation:.4f}", "σ", BLUE)])
            control = go.Figure()
            control.add_trace(go.Scatter(x=spc_view["timestamp"], y=values, mode="lines+markers", name="Ölçüm", line=dict(color=GREEN, width=2), marker=dict(color=[RED if flag else GREEN for flag in outside], size=7)))
            for limit, label, colour, dash in [(center, "CL", GREEN, "dash"), (ucl, "UCL", RED, "dot"), (lcl, "LCL", RED, "dot"), (spec_high, "USL", AMBER, "dashdot"), (spec_low, "LSL", AMBER, "dashdot")]:
                if limit is not None:
                    control.add_hline(y=limit, line_color=colour, line_dash=dash, annotation_text=f"{label} {limit:.3f}")
            _chart(control, "spc_control_chart", 390)
            st.dataframe(spc_view[["timestamp", "machine_code", "product", "measurement_name", "measurement_value", "unit", "spec_low", "spec_high"]].sort_values("timestamp", ascending=False), hide_index=True, use_container_width=True, height=220)

    with pareto_tab:
        st.markdown("#### Hata Türleri Pareto Analizi")
        if reason_totals.empty:
            st.info("Pareto analizi için hata kaydı yok.")
        else:
            pareto_frame = reason_totals.rename("Hatalı Adet").reset_index().rename(columns={"defect_reason": "Hata Türü"})
            pareto_frame["Kümülatif %"] = pareto_frame["Hatalı Adet"].cumsum() / max(pareto_frame["Hatalı Adet"].sum(), 1) * 100
            pareto = go.Figure(go.Bar(x=pareto_frame["Hata Türü"], y=pareto_frame["Hatalı Adet"], marker_color=GREEN, name="Hatalı adet", text=pareto_frame["Hatalı Adet"]))
            pareto.add_trace(go.Scatter(x=pareto_frame["Hata Türü"], y=pareto_frame["Kümülatif %"], yaxis="y2", mode="lines+markers", name="Kümülatif %", line=dict(color=RED, width=3)))
            pareto.update_layout(yaxis2=dict(overlaying="y", side="right", range=[0, 110], ticksuffix="%"))
            _chart(pareto, "quality_pareto_spc", 390)
            display_pareto = pareto_frame.copy()
            display_pareto["Kümülatif %"] = display_pareto["Kümülatif %"].map(lambda x: f"%{x:.1f}")
            st.dataframe(display_pareto, hide_index=True, use_container_width=True)

    with st.expander("Rapor ve dışa aktarma"):
        report = data[["timestamp", "machine_code", "product", "produced", "defective", "defect_reason", "shift"]]
        report_columns = st.columns(2)
        report_columns[0].download_button("Kalite CSV", report.to_csv(index=False).encode("utf-8-sig"), "kalite_raporu.csv", "text/csv", use_container_width=True)
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            report.to_excel(writer, sheet_name="Kalite", index=False)
            machine_summary.to_excel(writer, sheet_name="Makine Özeti", index=False)
        report_columns[1].download_button("Kalite Excel", output.getvalue(), "kalite_raporu.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)

