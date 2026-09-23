"""TREX MES 5 Why kök neden analizi ve düzeltici faaliyet takibi."""
from datetime import date, datetime, timedelta
import html
import uuid

import pandas as pd
import plotly.graph_objects as go
import streamlit as st


STATUS_ORDER = ["Taslak", "Analiz Ediliyor", "Aksiyon Açık", "Doğrulama", "Tamamlandı"]
PRIORITY_COLOURS = {"Kritik": "#ef454c", "Yüksek": "#f39a17", "Normal": "#168b55", "Düşük": "#4d97d8"}
STATUS_COLOURS = {"Taslak": "#83978d", "Analiz Ediliyor": "#3999df", "Aksiyon Açık": "#ef9a18", "Doğrulama": "#8667dc", "Tamamlandı": "#16a15d"}


def _id():
    return int(uuid.uuid4().int % 2_000_000_000) or 1


def _safe(value):
    return html.escape(str(value or "—"))


def _source_records(sources):
    records = [{"type": "Manuel", "id": 0, "machine": "", "label": "Manuel problem kaydı"}]
    definitions = [
        ("Alarm", sources.get("alarms"), "alarm", "time"),
        ("Duruş", sources.get("stops"), "reason", "event_at"),
        ("Bakım", sources.get("maintenance"), "maintenance_type", "next_date"),
        ("İş Emri", sources.get("orders"), "product", "due_date"),
        ("Kalite", sources.get("quality"), "defect_reason", "timestamp"),
    ]
    for source_type, frame, text_col, date_col in definitions:
        if frame is None or frame.empty:
            continue
        for _, row in frame.head(50).iterrows():
            machine = str(row.get("machine_code") or "—")
            detail = str(row.get(text_col) or source_type)
            event_date = str(row.get(date_col) or "")[:16]
            records.append({
                "type": source_type,
                "id": int(row.get("id") or 0),
                "machine": "" if machine == "—" else machine,
                "description": detail,
                "label": f"{source_type} · {machine} · {detail} · {event_date}",
            })
    return records


def _analysis_metrics(analyses):
    if analyses.empty:
        return 0, 0, 0, 0
    due = pd.to_datetime(analyses["due_date"], errors="coerce")
    open_mask = analyses["status"] != "Tamamlandı"
    overdue = int((open_mask & due.notna() & (due.dt.date < date.today())).sum())
    completed = int((analyses["status"] == "Tamamlandı").sum())
    recurring = int(analyses["root_cause"].fillna("").str.strip().replace("", pd.NA).value_counts().ge(2).sum())
    return len(analyses), int(open_mask.sum()), overdue, recurring


def render_five_why_tab(query, execute, *, current_user="", can_manage=False, notifier=None, machines=None, sources=None):
    sources = sources or {}
    machines = machines if machines is not None else pd.DataFrame()
    analyses = query("SELECT * FROM five_why_analyses ORDER BY id DESC")
    users = query("SELECT full_name,username,role FROM users WHERE is_active=1 ORDER BY full_name")
    source_records = _source_records(sources)

    st.markdown("""
    <style>
      .why-hero{background:linear-gradient(125deg,#073f2e,#0e8d56);border-radius:14px;padding:17px 20px;color:#fff;margin:3px 0 10px;display:grid;grid-template-columns:1fr auto;gap:18px;align-items:center;box-shadow:0 8px 22px rgba(5,91,52,.16)}
      .why-hero h3{color:#fff!important;margin:0!important;font-size:1.05rem!important}.why-hero p{margin:5px 0 0;color:#d9f6e7;font-size:.69rem;max-width:720px}.why-hero span{border:1px solid rgba(255,255,255,.28);border-radius:99px;padding:6px 11px;font-size:.62rem;font-weight:800}
      .why-kpi{background:linear-gradient(135deg,#fff,#f1faf5);border:1px solid #d2e9dc;border-top:3px solid var(--c);border-radius:10px;padding:10px 12px;min-height:82px}.why-kpi small{display:block;color:#5a7567;font-size:.62rem;font-weight:800}.why-kpi b{display:block;color:#103f2e;font-size:1.42rem;margin-top:5px}.why-kpi em{font-style:normal;color:#769085;font-size:.57rem}
      .why-card{border:1px solid #d4e8dc;background:#fff;border-radius:10px;padding:11px 13px;margin:7px 0;box-shadow:0 3px 10px rgba(7,72,43,.05)}.why-card-head{display:flex;align-items:center;gap:7px}.why-card-head b{font-size:.77rem;color:#123f2e}.why-pill{font-size:.55rem;font-weight:850;padding:3px 7px;border-radius:99px;color:#fff;background:var(--c)}.why-meta{font-size:.59rem;color:#71877b;margin-top:5px}.why-root{margin-top:7px;background:#f1f8f4;border-left:3px solid #128951;padding:7px 9px;border-radius:6px;color:#345a48;font-size:.65rem}.why-chain{position:relative;margin:8px 0 4px;padding-left:19px}.why-step{position:relative;border-left:2px solid #b8dfc9;padding:0 0 11px 15px}.why-step:before{content:attr(data-step);position:absolute;left:-11px;top:0;width:20px;height:20px;border-radius:50%;background:#118e54;color:#fff;text-align:center;line-height:20px;font-size:.58rem;font-weight:900}.why-step b{display:block;color:#174a36;font-size:.67rem}.why-step span{display:block;color:#5d786a;font-size:.64rem;margin-top:2px}.why-empty{border:1px dashed #bcdaca;border-radius:11px;padding:25px;text-align:center;background:#f7fcf9;color:#557565;font-size:.72rem}
      @media(max-width:800px){.why-hero{grid-template-columns:1fr}.why-hero span{display:none}}
    </style>
    <div class="why-hero"><div><h3>5 Why · Kök Neden ve Aksiyon Merkezi</h3><p>Problemi semptomdan kök nedene kadar izleyin; kalıcı aksiyonu, sorumluyu, termini ve etkinlik doğrulamasını tek yerde yönetin.</p></div><span>PDCA UYUMLU</span></div>
    """, unsafe_allow_html=True)

    total, open_count, overdue, recurring = _analysis_metrics(analyses)
    kpis = st.columns(4, gap="small")
    for column, (label, value, note, colour) in zip(kpis, [
        ("Toplam Analiz", total, "Kayıtlı problem", "#168b55"),
        ("Açık Aksiyon", open_count, "Takip gerektiriyor", "#ef9a18"),
        ("Geciken", overdue, "Termin aşımı", "#ef454c"),
        ("Tekrarlayan Kök Neden", recurring, "En az iki tekrar", "#8667dc"),
    ]):
        column.markdown(f'<div class="why-kpi" style="--c:{colour}"><small>{label}</small><b>{value}</b><em>{note}</em></div>', unsafe_allow_html=True)

    new_tab, tracking_tab, analytics_tab = st.tabs(["Yeni Analiz", "Aksiyon Takibi", "Yönetim Özeti"])
    with new_tab:
        if not can_manage:
            st.info("Yeni analiz oluşturmak için yönetici, bakım veya kalite yetkisi gerekir.")
        else:
            source_labels = [record["label"] for record in source_records]
            source_label = st.selectbox("Analizin başlangıç kaydı", source_labels, key="why_source")
            source = source_records[source_labels.index(source_label)]
            machine_options = machines["machine_code"].dropna().astype(str).tolist() if not machines.empty else []
            default_machine = source.get("machine", "")
            machine_index = machine_options.index(default_machine) if default_machine in machine_options else 0
            with st.form("five_why_create_form", clear_on_submit=True):
                top_a, top_b, top_c = st.columns([1.5, 1, 1])
                title = top_a.text_input("Problem başlığı *", placeholder="Örn. CNC-02 tekrarlayan rulman arızası")
                priority = top_b.selectbox("Öncelik", ["Kritik", "Yüksek", "Normal", "Düşük"], index=2)
                machine_code = top_c.selectbox("Makine", machine_options, index=machine_index) if machine_options else top_c.text_input("Makine")
                description = st.text_area("Problem tanımı *", value=source.get("description", ""), placeholder="Ne oldu, nerede oldu, etkisi neydi?", height=80)
                st.markdown("##### Neden zinciri")
                why_answers = []
                prompts = ["Problem neden oluştu?", "Bu durum neden gerçekleşti?", "Bir önceki neden neden ortaya çıktı?", "Sistem bunu neden önleyemedi?", "Kök neden neden kalıcı hâle geldi?"]
                for index, prompt in enumerate(prompts, 1):
                    why_answers.append(st.text_input(f"{index}. Neden — {prompt}", key=f"why_answer_{index}"))
                action_a, action_b = st.columns(2)
                root_cause = action_a.text_area("Doğrulanmış kök neden *", height=90, placeholder="Kanıtla desteklenen temel neden")
                containment = action_b.text_area("Geçici önlem", height=90, placeholder="Etkileri hemen sınırlamak için alınan önlem")
                corrective = action_a.text_area("Kalıcı düzeltici faaliyet *", height=90, placeholder="Tekrarı önleyecek sistemsel aksiyon")
                verification = action_b.text_area("Etkinlik doğrulaması", height=90, placeholder="Aksiyonun işe yaradığını nasıl ölçeceğiz?")
                owner_options = users["full_name"].fillna(users["username"]).astype(str).tolist() if not users.empty else [current_user or "Atanmadı"]
                foot_a, foot_b, foot_c = st.columns(3)
                owner = foot_a.selectbox("Sorumlu", owner_options)
                due_date = foot_b.date_input("Termin", value=date.today() + timedelta(days=7), min_value=date.today())
                status = foot_c.selectbox("Başlangıç durumu", STATUS_ORDER[1:4])
                submitted = st.form_submit_button("Analizi ve Aksiyonu Kaydet", type="primary", use_container_width=True)
            if submitted:
                filled_whys = [answer.strip() for answer in why_answers if answer.strip()]
                if not title.strip() or not description.strip() or not root_cause.strip() or not corrective.strip() or len(filled_whys) < 2:
                    st.error("Başlık, problem tanımı, en az iki neden, kök neden ve kalıcı faaliyet alanlarını doldurun.")
                else:
                    analysis_id = _id()
                    execute("""INSERT INTO five_why_analyses(id,title,event_type,source_type,source_id,machine_code,event_description,priority,status,owner,due_date,root_cause,containment_action,corrective_action,verification_method,created_by,created_at,completed_at)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (analysis_id, title.strip(), source["type"], source["type"], source["id"], machine_code, description.strip(), priority, status, owner, due_date.isoformat(), root_cause.strip(), containment.strip(), corrective.strip(), verification.strip(), current_user, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), None))
                    placeholders, params = [], []
                    for step_no, answer in enumerate(why_answers, 1):
                        if answer.strip():
                            placeholders.append("(?,?,?,?,?)")
                            params.extend((_id(), analysis_id, step_no, prompts[step_no - 1], answer.strip()))
                    execute("INSERT INTO five_why_steps(id,analysis_id,step_no,question,answer) VALUES " + ",".join(placeholders), tuple(params))
                    if notifier is not None and not users.empty:
                        assigned_user = users[users["full_name"].fillna(users["username"]).astype(str) == owner]
                        if not assigned_user.empty:
                            notifier(
                                recipient_username=str(assigned_user.iloc[0]["username"]),
                                notification_type="5 Why Aksiyonu",
                                priority=priority,
                                title=f"5 Why aksiyonu: {title.strip()}",
                                message=f"{machine_code or 'Genel'} için kalıcı faaliyet atandı. Termin: {due_date:%d.%m.%Y}",
                                machine_code=machine_code,
                                target_module="🧠 Akıllı Analiz",
                                entity_type="five_why",
                                entity_id=analysis_id,
                            )
                    st.success("5 Why analizi oluşturuldu ve aksiyon takibine alındı.")
                    st.rerun()

    with tracking_tab:
        if analyses.empty:
            st.markdown('<div class="why-empty">Henüz 5 Why analizi yok. İlk problemi “Yeni Analiz” sekmesinden kaydedin.</div>', unsafe_allow_html=True)
        else:
            filter_a, filter_b, filter_c = st.columns(3)
            status_filter = filter_a.selectbox("Durum", ["Tümü"] + STATUS_ORDER, key="why_status_filter")
            priority_filter = filter_b.selectbox("Öncelik", ["Tümü", "Kritik", "Yüksek", "Normal", "Düşük"], key="why_priority_filter")
            machine_filter = filter_c.selectbox("Makine", ["Tümü"] + sorted(analyses["machine_code"].dropna().astype(str).unique().tolist()), key="why_machine_filter")
            filtered = analyses.copy()
            if status_filter != "Tümü": filtered = filtered[filtered["status"] == status_filter]
            if priority_filter != "Tümü": filtered = filtered[filtered["priority"] == priority_filter]
            if machine_filter != "Tümü": filtered = filtered[filtered["machine_code"] == machine_filter]
            for _, row in filtered.head(30).iterrows():
                priority_colour = PRIORITY_COLOURS.get(str(row["priority"]), "#168b55")
                due_value = pd.to_datetime(row["due_date"], errors="coerce")
                overdue_text = " · GECİKMİŞ" if row["status"] != "Tamamlandı" and pd.notna(due_value) and due_value.date() < date.today() else ""
                st.markdown(f'<div class="why-card"><div class="why-card-head"><span class="why-pill" style="--c:{priority_colour}">{_safe(row["priority"])}</span><b>#{int(row["id"])} · {_safe(row["title"])}</b></div><div class="why-meta">{_safe(row["machine_code"])} · {_safe(row["event_type"])} · Sorumlu: {_safe(row["owner"])} · Termin: {_safe(row["due_date"])}{overdue_text}</div><div class="why-root"><b>Kök neden:</b> {_safe(row["root_cause"])}</div></div>', unsafe_allow_html=True)
            selected_ids = filtered["id"].astype(int).tolist()
            if selected_ids:
                labels = {int(row["id"]): f'#{int(row["id"])} · {row["title"]}' for _, row in filtered.iterrows()}
                selected_id = st.selectbox("Detay ve durum güncelleme", selected_ids, format_func=lambda value: labels[value], key="why_selected_analysis")
                selected = filtered[filtered["id"] == selected_id].iloc[0]
                steps = query("SELECT step_no,question,answer FROM five_why_steps WHERE analysis_id=? ORDER BY step_no", (int(selected_id),))
                detail_left, detail_right = st.columns([1.25, 1])
                with detail_left, st.container(border=True):
                    st.markdown("##### Neden zinciri")
                    chain = '<div class="why-chain">' + "".join(f'<div class="why-step" data-step="{int(step["step_no"])}"><b>{_safe(step["question"])}</b><span>{_safe(step["answer"])}</span></div>' for _, step in steps.iterrows()) + "</div>"
                    st.markdown(chain, unsafe_allow_html=True)
                with detail_right, st.container(border=True):
                    st.markdown("##### Kalıcı faaliyet")
                    st.write(selected["corrective_action"] or "—")
                    st.caption(f'Doğrulama: {selected["verification_method"] or "Tanımlanmadı"}')
                    if can_manage:
                        with st.form("why_update_form"):
                            current_status = str(selected["status"])
                            new_status = st.selectbox("Durum", STATUS_ORDER, index=STATUS_ORDER.index(current_status) if current_status in STATUS_ORDER else 0)
                            if not users.empty:
                                update_owner_options = users["full_name"].fillna(users["username"]).astype(str).tolist()
                                selected_owner = str(selected["owner"] or "")
                                owner_index = update_owner_options.index(selected_owner) if selected_owner in update_owner_options else 0
                                new_owner = st.selectbox("Sorumlu", update_owner_options, index=owner_index)
                            else:
                                new_owner = st.text_input("Sorumlu", value=str(selected["owner"] or ""))
                            new_due = st.date_input("Termin", value=pd.to_datetime(selected["due_date"], errors="coerce").date() if pd.notna(pd.to_datetime(selected["due_date"], errors="coerce")) else date.today())
                            update = st.form_submit_button("Aksiyonu Güncelle", type="primary", use_container_width=True)
                        if update:
                            completed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S") if new_status == "Tamamlandı" else None
                            execute("UPDATE five_why_analyses SET status=?,owner=?,due_date=?,completed_at=? WHERE id=?", (new_status, new_owner, new_due.isoformat(), completed_at, int(selected_id)))
                            st.success("Aksiyon durumu güncellendi.")
                            st.rerun()

    with analytics_tab:
        if analyses.empty:
            st.info("Yönetim özeti için analiz verisi bekleniyor.")
        else:
            chart_left, chart_right = st.columns(2)
            status_counts = analyses["status"].value_counts().reindex(STATUS_ORDER, fill_value=0)
            status_fig = go.Figure(go.Pie(labels=status_counts.index, values=status_counts.values, hole=.62, marker_colors=[STATUS_COLOURS[item] for item in status_counts.index], textinfo="label+value"))
            status_fig.update_layout(title="Analiz Durum Dağılımı", height=300, margin=dict(l=10, r=10, t=45, b=10), showlegend=False, paper_bgcolor="rgba(0,0,0,0)")
            chart_left.plotly_chart(status_fig, use_container_width=True, config={"displayModeBar": False})
            machine_counts = analyses["machine_code"].fillna("Genel").value_counts().head(8).sort_values()
            machine_fig = go.Figure(go.Bar(x=machine_counts.values, y=machine_counts.index, orientation="h", marker_color="#168b55", text=machine_counts.values, textposition="outside"))
            machine_fig.update_layout(title="En Çok Analiz Açılan Makineler", height=300, margin=dict(l=10, r=30, t=45, b=25), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
            chart_right.plotly_chart(machine_fig, use_container_width=True, config={"displayModeBar": False})
            root_causes = analyses["root_cause"].fillna("").str.strip()
            root_causes = root_causes[root_causes != ""].value_counts().head(10).rename_axis("Kök Neden").reset_index(name="Tekrar")
            st.markdown("##### Tekrarlayan kök nedenler")
            if root_causes.empty:
                st.caption("Henüz sınıflandırılmış kök neden bulunmuyor.")
            else:
                st.dataframe(root_causes, hide_index=True, use_container_width=True)
            export_columns = ["id", "title", "event_type", "machine_code", "priority", "status", "owner", "due_date", "root_cause", "corrective_action", "verification_method", "created_at", "completed_at"]
            st.download_button("5 Why raporunu indir", analyses[export_columns].to_csv(index=False).encode("utf-8-sig"), "trex-5-why-analizleri.csv", "text/csv", use_container_width=True)

