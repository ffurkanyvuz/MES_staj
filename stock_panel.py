"""Stok yönetimi için kompakt yönetici paneli."""
from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st


def _status(row):
    stock = float(row.get("stock", 0) or 0)
    minimum = float(row.get("min_stock", 0) or 0)
    if minimum > 0 and stock < minimum:
        return "Kritik"
    if minimum > 0 and stock <= minimum * 1.25:
        return "Düşük"
    return "Normal"


def render_stock_panel(query):
    products = query("SELECT id,product_code,product_name,unit,ideal_cycle,stock,min_stock FROM products ORDER BY product_code")
    movements = query("SELECT id,product_id,product_code,movement_type,quantity,reason,timestamp FROM stock ORDER BY id DESC")

    for column in ("stock", "min_stock", "ideal_cycle"):
        products[column] = pd.to_numeric(products[column], errors="coerce").fillna(0)
    movements["quantity"] = pd.to_numeric(movements["quantity"], errors="coerce").fillna(0)
    movements["_time"] = pd.to_datetime(movements["timestamp"], errors="coerce")
    movements["_date"] = movements["_time"].dt.date
    products["Durum"] = products.apply(_status, axis=1)
    products["Eksik"] = (products["min_stock"] - products["stock"]).clip(lower=0)

    st.subheader("Stok Yönetimi")
    st.markdown("""
    <style>
      .stk-stat{min-height:88px;background:linear-gradient(120deg,#fff,#eef9f5);border:1px solid #d5e9e0;
        border-left:4px solid var(--stk-color);border-radius:9px;padding:11px 13px;box-sizing:border-box}
      .stk-stat small{display:block;color:#48675a;font-size:11px;font-weight:750}.stk-stat b{display:block;color:#07543a;font-size:23px;line-height:33px}
      .stk-stat span{font-size:9px;color:#6b8379}.stk-title{font-size:14px;color:#10583e;font-weight:800;margin-bottom:9px}
      .stk-critical{display:grid;grid-template-columns:55px minmax(0,1fr) 48px;gap:8px;align-items:center;
        border-bottom:1px solid #e2eee8;padding:8px 2px;font-size:10px;color:#355f4c}
      .stk-critical b{font-size:10px}.stk-badge{display:inline-block;padding:3px 7px;border-radius:12px;font-size:9px;font-weight:700}
      .stk-badge-kritik{background:#ffe5e7;color:#d83d49}.stk-badge-düşük{background:#fff1d6;color:#b66c00}.stk-badge-normal{background:#dcf5e8;color:#087a4e}
    </style>
    """, unsafe_allow_html=True)

    metric_area = st.container()
    filters = st.columns([1.05, 1.05, 1.15, 1.75, .9])
    with filters[0]:
        chosen_unit = st.selectbox("Birim", ["Tümü"] + sorted(products["unit"].dropna().astype(str).unique().tolist()), key="stk_unit_v2")
    with filters[1]:
        chosen_status = st.selectbox("Stok durumu", ["Tümü", "Normal", "Düşük", "Kritik"], key="stk_status_v2")
    with filters[2]:
        movement_dates = st.date_input("Hareket tarih aralığı", value=(date.today() - timedelta(days=30), date.today()), key="stk_dates_v2")
    with filters[3]:
        search = st.text_input("Stok ara", placeholder="Kod, ürün adı veya birim...", key="stk_search_v2")
    with filters[4]:
        st.write("")
        if st.button("＋ Yeni Stok Kaydı", type="primary", use_container_width=True, key="stk_new_v2"):
            st.session_state["stock_movement_default"] = "Giriş"
            st.session_state["stock_new_open"] = True

    view = products.copy()
    if chosen_unit != "Tümü":
        view = view[view["unit"].astype(str) == chosen_unit]
    if chosen_status != "Tümü":
        view = view[view["Durum"] == chosen_status]
    if search.strip():
        source = view["product_code"].fillna("").astype(str) + " " + view["product_name"].fillna("").astype(str) + " " + view["unit"].fillna("").astype(str)
        view = view[source.str.contains(search.strip(), case=False, regex=False)]

    critical = products[products["Durum"] == "Kritik"]
    low = products[products["Durum"] == "Düşük"]
    today_moves = int((movements["_date"] == date.today()).sum())
    with metric_area:
        cols = st.columns(5)
        stats = [
            ("Toplam Stok Kalemi", len(products), "Tanımlı ürün", "#168e63"),
            ("Kritik Stok", len(critical), "Minimumun altında", "#ef4b55"),
            ("Düşük Stok", len(low), "Minimuma yakın", "#f0a11a"),
            ("Stokta Olan Kalem", int((products["stock"] > 0).sum()), "Mevcudu bulunan", "#168e63"),
            ("Bugünkü Hareketler", today_moves, "Giriş ve çıkış", "#168e63"),
        ]
        for column, (label, value, note, colour) in zip(cols, stats):
            column.markdown(f'<div class="stk-stat" style="--stk-color:{colour}"><small>{label}</small><b>{value}</b><span>{note}</span></div>', unsafe_allow_html=True)

    if not isinstance(movement_dates, (tuple, list)):
        movement_dates = (movement_dates, movement_dates)
    start_date, end_date = (movement_dates[0], movement_dates[-1]) if movement_dates else (date.today(), date.today())
    period_moves = movements[movements["_time"].between(pd.Timestamp(start_date), pd.Timestamp(end_date) + pd.Timedelta(days=1), inclusive="left")].copy()

    def title(text):
        st.markdown(f'<div class="stk-title">{text}</div>', unsafe_allow_html=True)

    def chart(fig, key, height=220):
        fig.update_layout(height=height, margin=dict(l=8, r=10, t=16, b=30), font=dict(size=10, color="#365e4c"),
                          paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", legend=dict(orientation="h", font=dict(size=9)))
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False}, key=key)

    latest = movements.groupby("product_code", as_index=False).first()[["product_code", "timestamp"]] if not movements.empty else pd.DataFrame(columns=["product_code", "timestamp"])
    table = view.merge(latest, on="product_code", how="left")
    table = table.rename(columns={"product_code": "Kod", "product_name": "Malzeme", "unit": "Birim", "stock": "Mevcut", "min_stock": "Minimum", "timestamp": "Son Hareket"})
    table["Mevcut"] = table["Mevcut"].round(2)
    table["Minimum"] = table["Minimum"].round(2)

    main = st.columns([2.05, 1], gap="small")
    with main[0], st.container(border=True):
        title("Stok Listesi")
        st.caption(f"{len(table)} stok kalemi gösteriliyor")
        st.dataframe(table[["Kod", "Malzeme", "Birim", "Mevcut", "Minimum", "Durum", "Son Hareket"]], hide_index=True, use_container_width=True, height=318)
    with main[1]:
        with st.container(border=True):
            title("Kritik ve Düşük Stoklar")
            alerts = products[products["Durum"].isin(["Kritik", "Düşük"])].sort_values(["Durum", "stock"]).head(6)
            if alerts.empty:
                st.success("Kritik veya düşük stok yok.")
            for row in alerts.itertuples():
                css = "kritik" if row.Durum == "Kritik" else "düşük"
                st.markdown(f'<div class="stk-critical"><b>{row.product_code}</b><span>{row.product_name}<br>{row.stock:g} / min. {row.min_stock:g} {row.unit}</span><i class="stk-badge stk-badge-{css}">{row.Durum}</i></div>', unsafe_allow_html=True)
        with st.container(border=True):
            title("Son Stok Hareketleri")
            recent = period_moves.head(6).rename(columns={"timestamp": "Tarih", "product_code": "Malzeme", "movement_type": "İşlem", "quantity": "Miktar"})
            if recent.empty:
                st.info("Seçili dönemde hareket yok.")
            else:
                st.dataframe(recent[["Tarih", "Malzeme", "İşlem", "Miktar"]], hide_index=True, use_container_width=True, height=205)

    bottom = st.columns([1, 1.18, 1, .95], gap="small")
    with bottom[0], st.container(border=True):
        title("Birim Türlerine Göre Dağılım")
        unit_counts = products.groupby(products["unit"].fillna("Belirsiz")).size().sort_values(ascending=False)
        if unit_counts.empty:
            st.info("Veri yok.")
        else:
            fig = go.Figure(go.Pie(labels=unit_counts.index, values=unit_counts.values, hole=.62, marker_colors=["#07885a", "#20b881", "#ffb51b", "#43a5e8", "#8267d6"]))
            fig.add_annotation(text=f"<b>{len(products)}</b><br><span style='font-size:9px'>kalem</span>", showarrow=False)
            chart(fig, "stk_unit_chart")
    with bottom[1], st.container(border=True):
        title("Aylık Stok Hareketleri")
        valid = movements.dropna(subset=["_time"]).copy()
        if valid.empty:
            st.info("Hareket verisi yok.")
        else:
            valid["Ay"] = valid["_time"].dt.to_period("M").astype(str)
            monthly = valid.groupby(["Ay", "movement_type"])["quantity"].sum().unstack(fill_value=0).tail(8)
            fig = go.Figure()
            for movement_type, colour in (("Giriş", "#128e60"), ("Çıkış", "#f0aa2c")):
                if movement_type in monthly:
                    fig.add_bar(x=monthly.index, y=monthly[movement_type], name=movement_type, marker_color=colour)
            fig.update_layout(barmode="group")
            chart(fig, "stk_monthly_chart")
    with bottom[2], st.container(border=True):
        title("Hareket Türü Dağılımı")
        types = period_moves.groupby("movement_type")["quantity"].sum().sort_values(ascending=False)
        if types.empty:
            st.info("Seçili dönemde hareket yok.")
        else:
            fig = go.Figure(go.Pie(labels=types.index, values=types.values, hole=.62, marker_colors=["#159865", "#f4ae2b", "#499fd8"]))
            fig.add_annotation(text=f"<b>{int(types.sum()):,}</b><br><span style='font-size:9px'>miktar</span>", showarrow=False)
            chart(fig, "stk_type_chart")
    with bottom[3], st.container(border=True):
        title("Hızlı İşlemler")
        if st.button("＋ Stok Girişi", type="primary", use_container_width=True, key="stk_quick_in"):
            st.session_state["stock_movement_default"] = "Giriş"
            st.session_state["stock_new_open"] = True
            st.rerun()
        if st.button("－ Stok Çıkışı", type="primary", use_container_width=True, key="stk_quick_out"):
            st.session_state["stock_movement_default"] = "Çıkış"
            st.session_state["stock_new_open"] = True
            st.rerun()
        export = table[["Kod", "Malzeme", "Birim", "Mevcut", "Minimum", "Durum", "Son Hareket"]].to_csv(index=False).encode("utf-8-sig")
        st.download_button("⇩ Stok Raporu", export, "stok-raporu.csv", "text/csv", use_container_width=True, key="stk_report")
        st.caption("Transfer ve depo takibi için veritabanına konum alanı eklenmelidir.")

    return products
