"""Yönetici ve operatör için üretim kontrol merkezi."""
from datetime import date, datetime, timedelta
import io

import pandas as pd
import plotly.graph_objects as go
import streamlit as st


def render_production_panel(query, connection_factory, audit_callback, machine_frame, selected_shift_context, can_edit=True):
    # OEE bileşenleri app.py içinde gerçek makine sayaçlarından hesaplanır.
    machines = machine_frame.copy().sort_values("machine_code")
    orders = query("""
        SELECT id,order_no,machine_id,machine_code,product,target,produced,priority,status,
               due_date,start_production
        FROM work_orders ORDER BY id DESC
    """)
    history = query("""
        SELECT id,machine_id,machine_code,quantity,timestamp,shift
        FROM production_history ORDER BY machine_code,timestamp,id
    """)

    st.subheader("Üretim Yönetimi")
    st.caption("Üretim hedefleri, aktif iş emirleri ve vardiya performansı tek ekranda izlenir.")
    st.markdown("""
    <style>
      .prd-stat{min-height:88px;background:linear-gradient(125deg,#fff,#f0faf5);border:1px solid #d5ebe0;
        border-left:4px solid var(--prd-color);border-radius:10px;padding:10px 12px;box-sizing:border-box;box-shadow:0 3px 10px rgba(8,91,52,.045)}
      .prd-stat small{display:block;color:#506d5f;font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.03em}
      .prd-stat b{display:block;color:#0b4c35;font-size:22px;line-height:31px}.prd-stat span{font-size:10px;color:#6b8679}
      .prd-title{font-size:14px;font-weight:850;color:#10543b;margin:0 0 8px}.prd-machine{border:1px solid #dceee5;border-radius:9px;
        background:linear-gradient(140deg,#fff,#f4fcf8);padding:9px;margin-bottom:7px}.prd-machine-head{display:flex;justify-content:space-between;
        font-size:11px;font-weight:850;color:#164c38}.prd-machine-meta{display:flex;justify-content:space-between;color:#6b8277;font-size:9px;margin-top:6px}
      .prd-track{height:7px;background:#e2efe8;border-radius:9px;overflow:hidden;margin-top:7px}.prd-track i{height:100%;display:block;border-radius:9px;background:var(--prd-bar)}
      .prd-status{padding:3px 6px;border-radius:99px;background:var(--prd-bg);color:var(--prd-fg);font-size:9px}.prd-note{font-size:10px;color:#6c8578}
      div[data-testid="stDialog"]{max-width:760px}
    </style>
    """, unsafe_allow_html=True)

    if machines.empty:
        st.warning("Üretim ekranı için önce en az bir makine kaydı oluşturun.")
        return

    machines["production"] = pd.to_numeric(machines["production"], errors="coerce").fillna(0).clip(lower=0)
    machines["target"] = pd.to_numeric(machines["target"], errors="coerce").fillna(0).clip(lower=0)
    machines["defective"] = pd.to_numeric(machines["defective"], errors="coerce").fillna(0).clip(lower=0)
    for column in ["availability", "performance", "quality", "oee"]:
        machines[column] = pd.to_numeric(machines[column], errors="coerce").fillna(0).clip(0, 1)
    orders["target"] = pd.to_numeric(orders["target"], errors="coerce").fillna(0).clip(lower=0)
    orders["produced"] = pd.to_numeric(orders["produced"], errors="coerce").fillna(0).clip(lower=0)

    if not history.empty:
        history["timestamp"] = pd.to_datetime(history["timestamp"], errors="coerce")
        history["quantity"] = pd.to_numeric(history["quantity"], errors="coerce").fillna(0)
        history = history.dropna(subset=["timestamp"]).sort_values(["machine_code", "timestamp", "id"])
        history["Üretim"] = history.groupby("machine_code")["quantity"].diff().fillna(0).clip(lower=0)
        history["Tarih"] = history["timestamp"].dt.date

    filter_cols = st.columns([1.35, 1, 1, 1, 1.45, .8], gap="small")
    with filter_cols[0]:
        chosen_dates = st.date_input("Tarih aralığı", value=(date.today() - timedelta(days=6), date.today()), key="production_dates_v5")
    with filter_cols[1]:
        chosen_machine = st.selectbox("Makine", ["Tümü"] + machines["machine_code"].astype(str).tolist(), key="production_machine_v5")
    with filter_cols[2]:
        shifts = ["Tümü", "Sabah", "Akşam", "Gece"]
        default_shift = selected_shift_context if selected_shift_context in shifts else "Tümü"
        chosen_shift = st.selectbox("Vardiya", shifts, index=shifts.index(default_shift), key="production_shift_v5")
    with filter_cols[3]:
        products = sorted(set(machines["product"].dropna().astype(str)) | set(orders["product"].dropna().astype(str)))
        chosen_product = st.selectbox("Ürün", ["Tümü"] + products, key="production_product_v5")
    with filter_cols[4]:
        search = st.text_input("Üretim veya iş emri ara", placeholder="İş emri, ürün, makine...", key="production_search_v5")
    with filter_cols[5]:
        st.write("")
        if st.button("＋ Yeni Kayıt", type="primary", use_container_width=True, key="production_new_v5", disabled=not can_edit):
            st.session_state["production_new_open_v5"] = True

    if not isinstance(chosen_dates, (tuple, list)):
        chosen_dates = (chosen_dates, chosen_dates)
    start_date, end_date = (chosen_dates[0], chosen_dates[-1]) if chosen_dates else (date.today(), date.today())

    machine_view = machines.copy()
    order_view = orders.copy()
    history_view = history.copy()
    if not history_view.empty:
        history_view = history_view[(history_view["Tarih"] >= start_date) & (history_view["Tarih"] <= end_date)]
    if chosen_machine != "Tümü":
        machine_view = machine_view[machine_view["machine_code"] == chosen_machine]
        order_view = order_view[order_view["machine_code"] == chosen_machine]
        if not history_view.empty: history_view = history_view[history_view["machine_code"] == chosen_machine]
    if chosen_shift != "Tümü":
        machine_view = machine_view[machine_view["shift"] == chosen_shift]
        if not history_view.empty: history_view = history_view[history_view["shift"] == chosen_shift]
    if chosen_product != "Tümü":
        machine_view = machine_view[machine_view["product"] == chosen_product]
        order_view = order_view[order_view["product"] == chosen_product]
    if search.strip():
        needle = search.strip()
        order_text = order_view["order_no"].fillna("").astype(str) + " " + order_view["machine_code"].fillna("").astype(str) + " " + order_view["product"].fillna("").astype(str)
        order_view = order_view[order_text.str.contains(needle, case=False, regex=False)]
        machine_text = machine_view["machine_code"].fillna("").astype(str) + " " + machine_view["product"].fillna("").astype(str)
        machine_view = machine_view[machine_text.str.contains(needle, case=False, regex=False)]

    total_production = int(machine_view["production"].sum())
    total_target = int(machine_view["target"].sum())
    total_defective = int(machine_view["defective"].sum())
    completion = total_production / max(total_target, 1) * 100
    quality_rate = (total_production - total_defective) / max(total_production, 1) * 100
    active_orders = int((~orders["status"].isin(["Tamamlandı", "İptal"])).sum())
    if history_view.empty:
        production_speed = 0.0
    else:
        elapsed_hours = max((history_view["timestamp"].max() - history_view["timestamp"].min()).total_seconds() / 3600, 1)
        production_speed = float(history_view["Üretim"].sum()) / elapsed_hours

    stats = [
        ("Toplam Üretim", f"{total_production:,}", "adet", "#159a63"),
        ("Hedef", f"{total_target:,}", f"%{completion:.1f} gerçekleşme", "#159a63"),
        ("Kalan", f"{max(total_target-total_production, 0):,}", "hedefe kalan adet", "#efa72b"),
        ("Üretim Hızı", f"{production_speed:.0f}", "adet / saat", "#2d9cc4"),
        ("Kalite", f"%{quality_rate:.1f}", f"{total_defective} hatalı", "#159a63" if quality_rate >= 95 else "#e35d64"),
        ("Aktif İş Emri", str(active_orders), "üretimde veya sırada", "#6a79c8"),
    ]
    stat_cols = st.columns(6, gap="small")
    for column, (label, value, note, colour) in zip(stat_cols, stats):
        column.markdown(f'<div class="prd-stat" style="--prd-color:{colour}"><small>{label}</small><b>{value}</b><span>{note}</span></div>', unsafe_allow_html=True)

    def title(text):
        st.markdown(f'<div class="prd-title">{text}</div>', unsafe_allow_html=True)

    def chart(figure, key, height=230):
        figure.update_layout(height=height, margin=dict(l=8, r=8, t=18, b=28), font=dict(size=10, color="#365b49"), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", legend=dict(orientation="h", y=1.12, font=dict(size=9)))
        st.plotly_chart(figure, use_container_width=True, config={"displayModeBar": False}, key=key)

    top_row = st.columns([1.75, .85], gap="small")
    with top_row[0], st.container(border=True):
        title("Üretim Planı ve İş Emirleri")
        plan = order_view.copy()
        if plan.empty:
            st.info("Seçilen filtrelerde iş emri bulunamadı.")
        else:
            plan["Kalan"] = (plan["target"] - plan["produced"]).clip(lower=0).astype(int)
            plan["Gerçekleşme"] = (plan["produced"] / plan["target"].replace(0, 1) * 100).clip(0, 100).map(lambda value: f"%{value:.1f}")
            plan["Termin"] = pd.to_datetime(plan["due_date"], errors="coerce").dt.strftime("%d.%m.%Y").fillna("—")
            plan = plan.rename(columns={"order_no":"İş Emri", "machine_code":"Makine", "product":"Ürün", "target":"Hedef", "produced":"Üretim", "priority":"Öncelik", "status":"Durum"})
            st.dataframe(plan[["İş Emri", "Makine", "Ürün", "Hedef", "Üretim", "Kalan", "Gerçekleşme", "Öncelik", "Durum", "Termin"]], hide_index=True, use_container_width=True, height=255)
    with top_row[1], st.container(border=True):
        title("Makine Durumu")
        if machine_view.empty:
            st.caption("Filtreye uygun makine yok.")
        for _, machine in machine_view.head(6).iterrows():
            percent = min(float(machine["production"]) / max(float(machine["target"]), 1) * 100, 100)
            status = str(machine["status"])
            bar = "#159a63" if status == "Çalışıyor" else ("#e05a5a" if status == "Arızalı" else "#efa72b")
            bg = "#dcf6e8" if status == "Çalışıyor" else ("#ffe0e0" if status == "Arızalı" else "#fff0cf")
            fg = "#087b45" if status == "Çalışıyor" else ("#b52e34" if status == "Arızalı" else "#9b6700")
            st.markdown(f'<div class="prd-machine"><div class="prd-machine-head"><span>{machine["machine_code"]}</span><span class="prd-status" style="--prd-bg:{bg};--prd-fg:{fg}">● {status}</span></div><div class="prd-machine-meta"><span>{machine["product"]}</span><b>{int(machine["production"]):,} / {int(machine["target"]):,} · %{percent:.0f}</b></div><div class="prd-track" style="--prd-bar:{bar}"><i style="width:{percent:.1f}%"></i></div><div class="prd-machine-meta"><span>👤 {machine["operator"]}</span><span>OEE %{float(machine["oee"])*100:.1f}</span></div></div>', unsafe_allow_html=True)

    middle_row = st.columns([1.35, .8, .85], gap="small")
    with middle_row[0], st.container(border=True):
        title("Üretim Trendi")
        if history_view.empty:
            st.caption("Seçilen tarihlerde üretim geçmişi bulunamadı.")
        else:
            trend = history_view.groupby([history_view["timestamp"].dt.floor("h"), "shift"], dropna=False, as_index=False)["Üretim"].sum()
            figure = go.Figure()
            for shift_name, shift_data in trend.groupby("shift", dropna=False):
                figure.add_trace(go.Scatter(x=shift_data["timestamp"], y=shift_data["Üretim"], mode="lines+markers", name=str(shift_name or "Belirsiz"), line=dict(width=3)))
            figure.update_layout(yaxis_title="Adet", xaxis_title=None)
            chart(figure, "production_trend_v5")
    with middle_row[1], st.container(border=True):
        title("Vardiya Performansı")
        if history_view.empty:
            st.caption("Vardiya verisi bulunamadı.")
        else:
            shift_data = history_view.groupby("shift", as_index=False)["Üretim"].sum()
            shift_figure = go.Figure(go.Bar(x=shift_data["shift"], y=shift_data["Üretim"], marker_color=["#169b68", "#f0a62d", "#329bd0"][:len(shift_data)], text=shift_data["Üretim"].astype(int), textposition="outside"))
            chart(shift_figure, "production_shift_chart_v5")
    with middle_row[2], st.container(border=True):
        title("Hedef / Gerçekleşme")
        summary = machine_view.groupby("machine_code", as_index=False).agg(Hedef=("target", "sum"), Üretim=("production", "sum"))
        if summary.empty:
            st.caption("Makine üretim verisi bulunamadı.")
        else:
            comparison = go.Figure()
            comparison.add_trace(go.Bar(x=summary["machine_code"], y=summary["Hedef"], name="Hedef", marker_color="#cde7db"))
            comparison.add_trace(go.Bar(x=summary["machine_code"], y=summary["Üretim"], name="Üretim", marker_color="#159a63"))
            comparison.update_layout(barmode="group")
            chart(comparison, "production_target_v5")

    bottom_row = st.columns([1.35, .75, .7], gap="small")
    with bottom_row[0], st.container(border=True):
        title("Son Üretim Kayıtları")
        if history_view.empty:
            st.caption("Üretim kaydı bulunamadı.")
        else:
            recent = history_view.sort_values("timestamp", ascending=False).head(10).copy()
            recent["Tarih / Saat"] = recent["timestamp"].dt.strftime("%d.%m.%Y %H:%M")
            recent = recent.rename(columns={"machine_code":"Makine", "shift":"Vardiya", "quantity":"Toplam Sayaç"})
            st.dataframe(recent[["Tarih / Saat", "Makine", "Vardiya", "Üretim", "Toplam Sayaç"]], hide_index=True, use_container_width=True, height=225)
    with bottom_row[1], st.container(border=True):
        title("Üretim Özeti")
        st.metric("Ortalama OEE", f"%{machine_view['oee'].mean()*100:.1f}" if not machine_view.empty else "%0,0")
        st.metric("Çalışan Makine", int((machine_view["status"] == "Çalışıyor").sum()))
        st.metric("Hatalı Ürün", total_defective)
    with bottom_row[2], st.container(border=True):
        title("Hızlı İşlemler")
        if st.button("＋ Üretim Kaydı", type="primary", use_container_width=True, key="production_quick_new_v5", disabled=not can_edit):
            st.session_state["production_new_open_v5"] = True
        report = order_view[["order_no", "machine_code", "product", "target", "produced", "priority", "status", "due_date"]].copy()
        st.download_button("Üretim Planı · CSV", report.to_csv(index=False).encode("utf-8-sig"), "uretim_plani.csv", mime="text/csv", use_container_width=True)
        excel_file = io.BytesIO()
        with pd.ExcelWriter(excel_file, engine="openpyxl") as writer:
            report.to_excel(writer, sheet_name="Üretim Planı", index=False)
            machine_view.to_excel(writer, sheet_name="Makine Özeti", index=False)
        st.download_button("Excel Raporu", excel_file.getvalue(), "uretim_raporu.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)
        if not can_edit:
            st.caption("Yeni kayıt için operatör veya yönetici yetkisi gerekir.")

    @st.dialog("Yeni Üretim Kaydı")
    def production_record_dialog():
        active_orders_frame = orders[~orders["status"].isin(["Tamamlandı", "İptal"])].copy()
        order_labels = ["İş emri seçmeden kayıt"] + [f"{row['order_no']} · {row['product']} · {row['machine_code'] or 'Kuyruk'}" for _, row in active_orders_frame.iterrows()]
        chosen_order_label = st.selectbox("İş emri", order_labels, key="production_entry_order_v5")
        chosen_order_row = None
        if chosen_order_label != order_labels[0]:
            chosen_order_row = active_orders_frame.iloc[order_labels.index(chosen_order_label) - 1]
        machine_options = machines["machine_code"].astype(str).tolist()
        default_machine_index = 0
        if chosen_order_row is not None and str(chosen_order_row["machine_code"]) in machine_options:
            default_machine_index = machine_options.index(str(chosen_order_row["machine_code"]))
        machine_code = st.selectbox("Makine", machine_options, index=default_machine_index, key="production_entry_machine_v5")
        machine_row = machines[machines["machine_code"] == machine_code].iloc[0]
        entry_cols = st.columns(2)
        with entry_cols[0]:
            shift_name = st.selectbox("Vardiya", ["Sabah", "Akşam", "Gece"], index=["Sabah", "Akşam", "Gece"].index(str(machine_row["shift"])) if str(machine_row["shift"]) in ["Sabah", "Akşam", "Gece"] else 0, key="production_entry_shift_v5")
            good_quantity = st.number_input("Sağlam üretim", min_value=0, step=1, key="production_entry_good_v5")
        with entry_cols[1]:
            operator_name = st.text_input("Operatör", value=str(machine_row["operator"] or "Atama yok"), key="production_entry_operator_v5")
            defective_quantity = st.number_input("Hatalı üretim", min_value=0, step=1, key="production_entry_defective_v5")
        source = st.selectbox("Veri kaynağı", ["Manuel giriş", "Simülasyon doğrulaması", "PLC aktarımı"], key="production_entry_source_v5")
        note = st.text_input("Açıklama", placeholder="İsteğe bağlı üretim notu", key="production_entry_note_v5")
        submit_cols = st.columns(2)
        if submit_cols[0].button("Kaydı Tamamla", type="primary", use_container_width=True, key="production_entry_save_v5"):
            total_added = int(good_quantity) + int(defective_quantity)
            if total_added <= 0:
                st.error("En az bir adet üretim girmelisiniz.")
                return
            current_total = int(machine_row["production"])
            new_total = current_total + total_added
            connection = connection_factory()
            try:
                connection.execute("UPDATE machines SET production=?,defective=defective+?,operator=?,shift=?,status='Çalışıyor' WHERE id=?", (new_total, int(defective_quantity), operator_name.strip() or "Atama yok", shift_name, int(machine_row["id"])))
                connection.execute("INSERT INTO production_history(machine_id,machine_code,quantity,timestamp,shift) VALUES(?,?,?,?,?)", (int(machine_row["id"]), machine_code, new_total, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), shift_name))
                if int(defective_quantity) > 0:
                    product_name = str(chosen_order_row["product"]) if chosen_order_row is not None else str(machine_row["product"])
                    connection.execute("INSERT INTO quality(machine_id,machine_code,product,produced,defective,defect_reason,timestamp) VALUES(?,?,?,?,?,?,?)", (int(machine_row["id"]), machine_code, product_name, total_added, int(defective_quantity), note.strip() or "Üretim kaydı", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                if chosen_order_row is not None:
                    new_order_total = min(int(chosen_order_row["produced"]) + total_added, int(chosen_order_row["target"]))
                    new_status = "Tamamlandı" if new_order_total >= int(chosen_order_row["target"]) else "Üretimde"
                    connection.execute("UPDATE work_orders SET produced=?,status=?,machine_id=?,machine_code=? WHERE id=?", (new_order_total, new_status, int(machine_row["id"]), machine_code, int(chosen_order_row["id"])))
                connection.commit()
            except Exception as error:
                st.error(f"Üretim kaydı eklenemedi: {error}")
                return
            finally:
                connection.close()
            audit_callback("Üretim kaydı ekledi", machine_code, f"{total_added} adet · {shift_name} · {source} · {note}")
            st.session_state["production_new_open_v5"] = False
            st.success(f"{machine_code} için {total_added} adet üretim kaydedildi.")
            st.rerun()
        if submit_cols[1].button("Vazgeç", use_container_width=True, key="production_entry_cancel_v5"):
            st.session_state["production_new_open_v5"] = False
            st.rerun()

    if st.session_state.get("production_new_open_v5", False) and can_edit:
        production_record_dialog()
