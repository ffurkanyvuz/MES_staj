"""TREX MES verilerini kullanan, salt-okunur üretim karar destek asistanı."""
from __future__ import annotations

import json
import os
from datetime import date, datetime

import pandas as pd
import requests
import streamlit as st


TOOL_LABELS = {
    "get_factory_summary": "Fabrika genel görünümü",
    "get_machine_analysis": "Makine performansı",
    "get_production_performance": "Üretim ve hedef analizi",
    "get_downtime_analysis": "Duruş kayıp analizi",
    "get_maintenance_risks": "Bakım riskleri",
    "get_quality_summary": "Kalite görünümü",
    "get_open_actions": "Açık aksiyonlar",
    "compare_shifts": "Vardiya karşılaştırması",
}


def _setting(name, default=""):
    value = os.environ.get(name, "").strip()
    if value:
        return value
    try:
        return str(st.secrets.get(name, default)).strip()
    except Exception:
        return default


def _number(value, default=0.0):
    try:
        result = float(value)
        return result if pd.notna(result) else default
    except (TypeError, ValueError):
        return default


def _oee(row):
    planned = max(_number(row.get("planned_time")), 0)
    downtime = max(_number(row.get("downtime")), 0)
    production = max(_number(row.get("production")), 0)
    defective = max(_number(row.get("defective")), 0)
    ideal_cycle = max(_number(row.get("ideal_cycle")), 0)
    operating = max(planned - downtime, 0)
    availability = operating / planned if planned else 0
    performance = min(ideal_cycle * production / operating, 1) if operating else 0
    quality = max(min((production - defective) / production, 1), 0) if production else 0
    return {
        "availability_pct": round(availability * 100, 1),
        "performance_pct": round(performance * 100, 1),
        "quality_pct": round(quality * 100, 1),
        "oee_pct": round(availability * performance * quality * 100, 1),
    }


def _safe_query(query, sql, params=()):
    try:
        return query(sql, params)
    except Exception:
        return pd.DataFrame()


class MesReadTools:
    """Modelin erişebildiği sınırlı ve salt-okunur MES veri araçları."""

    def __init__(self, query, role):
        self.query = query
        self.role = role
        self.machines = _safe_query(query, """SELECT machine_code,status,production,target,planned_time,
            downtime,ideal_cycle,defective,product,operator,shift,last_maintenance,next_maintenance
            FROM machines ORDER BY machine_code""")

    def factory_summary(self):
        rows = []
        for _, machine in self.machines.iterrows():
            metrics = _oee(machine)
            rows.append({
                "machine": str(machine.get("machine_code", "")),
                "status": str(machine.get("status", "")),
                "production": int(_number(machine.get("production"))),
                "target": int(_number(machine.get("target"))),
                "oee_pct": metrics["oee_pct"],
            })
        production = sum(item["production"] for item in rows)
        target = sum(item["target"] for item in rows)
        alarms = _safe_query(self.query, "SELECT level,COUNT(*) AS total FROM alarms WHERE acknowledged=0 GROUP BY level")
        orders = _safe_query(self.query, "SELECT status,due_date FROM work_orders WHERE status!='Tamamlandı'")
        actions = _safe_query(self.query, "SELECT status,priority,due_at FROM operational_actions WHERE status NOT IN ('Tamamlandı','Doğrulandı','İptal')")
        maintenance = _safe_query(self.query, "SELECT next_date,status FROM maintenance WHERE status!='Tamamlandı'")
        today = date.today()
        late_orders = 0
        if not orders.empty:
            due = pd.to_datetime(orders["due_date"], errors="coerce")
            late_orders = int(((due.dt.date < today) | (orders["status"] == "Gecikmiş")).sum())
        overdue_maintenance = 0
        if not maintenance.empty:
            due = pd.to_datetime(maintenance["next_date"], errors="coerce")
            overdue_maintenance = int((due.dt.date < today).sum())
        overdue_actions = 0
        if not actions.empty:
            due = pd.to_datetime(actions["due_at"], errors="coerce")
            overdue_actions = int((due.dt.date < today).sum())
        alarm_counts = {str(r["level"]): int(r["total"]) for _, r in alarms.iterrows()} if not alarms.empty else {}
        return {
            "as_of": datetime.now().strftime("%d.%m.%Y %H:%M"),
            "production": production,
            "target": target,
            "target_attainment_pct": round(production / target * 100, 1) if target else 0,
            "average_oee_pct": round(sum(item["oee_pct"] for item in rows) / len(rows), 1) if rows else 0,
            "machine_statuses": {status: sum(1 for item in rows if item["status"] == status) for status in sorted({item["status"] for item in rows})},
            "open_alarms": alarm_counts,
            "late_work_orders": late_orders,
            "overdue_maintenance": overdue_maintenance,
            "open_actions": int(len(actions)),
            "overdue_actions": overdue_actions,
            "machines": rows,
        }

    def machine_analysis(self, machine_code):
        selected = self.machines[self.machines["machine_code"].astype(str).str.upper() == str(machine_code).upper()]
        if selected.empty:
            return {"error": "Makine bulunamadı", "available_machines": self.machine_codes}
        machine = selected.iloc[0]
        code = str(machine["machine_code"])
        alarms = _safe_query(self.query, "SELECT alarm,level,time FROM alarms WHERE machine_code=? AND acknowledged=0 ORDER BY id DESC LIMIT 5", (code,))
        sensors = _safe_query(self.query, "SELECT temperature,vibration,pressure,rpm,timestamp FROM sensors WHERE machine_code=? LIMIT 1", (code,))
        stops = _safe_query(self.query, "SELECT reason,duration,event_at FROM downtime WHERE machine_code=? ORDER BY id DESC LIMIT 10", (code,))
        order = _safe_query(self.query, "SELECT order_no,product,target,produced,priority,status,due_date FROM work_orders WHERE machine_code=? AND status!='Tamamlandı' ORDER BY id DESC LIMIT 1", (code,))
        return {
            "as_of": datetime.now().strftime("%d.%m.%Y %H:%M"),
            "machine": code,
            "status": str(machine.get("status", "")),
            "product": str(machine.get("product", "")),
            "operator": str(machine.get("operator", "")),
            "shift": str(machine.get("shift", "")),
            "production": int(_number(machine.get("production"))),
            "target": int(_number(machine.get("target"))),
            "oee": _oee(machine),
            "next_maintenance": str(machine.get("next_maintenance", "")),
            "active_alarms": alarms.fillna("").to_dict("records"),
            "latest_sensor": sensors.fillna("").to_dict("records"),
            "recent_downtime": stops.fillna("").to_dict("records"),
            "active_work_order": order.fillna("").to_dict("records"),
        }

    def production_performance(self):
        items = []
        for _, machine in self.machines.iterrows():
            production = int(_number(machine.get("production")))
            target = int(_number(machine.get("target")))
            metrics = _oee(machine)
            component_map = {
                "Kullanılabilirlik": metrics["availability_pct"],
                "Performans": metrics["performance_pct"],
                "Kalite": metrics["quality_pct"],
            }
            items.append({
                "machine": str(machine.get("machine_code", "")),
                "product": str(machine.get("product", "")),
                "status": str(machine.get("status", "")),
                "production": production,
                "target": target,
                "remaining": max(target - production, 0),
                "attainment_pct": round(production / target * 100, 1) if target else 0,
                "oee_pct": metrics["oee_pct"],
                "largest_oee_loss": min(component_map, key=component_map.get),
            })
        return {"as_of": datetime.now().strftime("%d.%m.%Y %H:%M"), "machines": items}

    def downtime_analysis(self):
        stops = _safe_query(self.query, "SELECT machine_code,reason,duration,event_at FROM downtime ORDER BY id DESC LIMIT 500")
        if stops.empty:
            return {"total_minutes": 0, "by_reason": [], "by_machine": []}
        stops["duration"] = pd.to_numeric(stops["duration"], errors="coerce").fillna(0).clip(lower=0)
        by_reason = stops.groupby("reason", dropna=False)["duration"].agg(["sum", "count"]).reset_index().sort_values("sum", ascending=False).head(8)
        by_machine = stops.groupby("machine_code", dropna=False)["duration"].agg(["sum", "count"]).reset_index().sort_values("sum", ascending=False)
        return {
            "record_scope": "Son 500 duruş kaydı",
            "total_minutes": round(float(stops["duration"].sum()), 1),
            "by_reason": [{"reason": str(r["reason"]), "minutes": round(float(r["sum"]), 1), "count": int(r["count"])} for _, r in by_reason.iterrows()],
            "by_machine": [{"machine": str(r["machine_code"]), "minutes": round(float(r["sum"]), 1), "count": int(r["count"])} for _, r in by_machine.iterrows()],
        }

    def maintenance_risks(self):
        sensors = _safe_query(self.query, "SELECT machine_code,temperature,vibration,pressure,rpm,timestamp FROM sensors ORDER BY machine_code")
        alarms = _safe_query(self.query, "SELECT machine_code,COUNT(*) AS total FROM alarms WHERE acknowledged=0 GROUP BY machine_code")
        sensor_map = sensors.set_index("machine_code").to_dict("index") if not sensors.empty else {}
        alarm_map = {str(r["machine_code"]): int(r["total"]) for _, r in alarms.iterrows()} if not alarms.empty else {}
        rows = []
        today = pd.Timestamp(date.today())
        for _, machine in self.machines.iterrows():
            code = str(machine["machine_code"])
            risk, reasons = 0, []
            due = pd.to_datetime(machine.get("next_maintenance"), errors="coerce")
            days = int((due.normalize() - today).days) if pd.notna(due) else None
            if days is not None and days < 0:
                risk += 45; reasons.append(f"Bakım {abs(days)} gün gecikmiş")
            elif days is not None and days <= 7:
                risk += 25; reasons.append(f"Bakıma {days} gün kaldı")
            sensor = sensor_map.get(code, {})
            temp = _number(sensor.get("temperature"))
            vibration = _number(sensor.get("vibration"))
            if temp >= 85:
                risk += 20; reasons.append(f"Sıcaklık {temp:.1f}°C")
            if vibration >= 5:
                risk += 20; reasons.append(f"Titreşim {vibration:.1f} mm/s")
            if str(machine.get("status")) == "Arızalı":
                risk += 35; reasons.append("Makine arızalı")
            if alarm_map.get(code, 0):
                risk += min(15, alarm_map[code] * 5); reasons.append(f"{alarm_map[code]} açık alarm")
            rows.append({"machine": code, "risk_score": min(risk, 100), "reasons": reasons or ["Belirgin risk göstergesi yok"], "next_maintenance": str(machine.get("next_maintenance", ""))})
        return {"method": "Bakım tarihi, makine durumu, sensör ve açık alarm göstergeleri", "risks": sorted(rows, key=lambda item: item["risk_score"], reverse=True)}

    def quality_summary(self):
        quality = _safe_query(self.query, "SELECT machine_code,product,produced,defective,defect_reason,timestamp FROM quality ORDER BY id DESC LIMIT 300")
        if quality.empty:
            return {"produced": 0, "defective": 0, "quality_pct": 0, "defects": []}
        quality["produced"] = pd.to_numeric(quality["produced"], errors="coerce").fillna(0)
        quality["defective"] = pd.to_numeric(quality["defective"], errors="coerce").fillna(0)
        produced = float(quality["produced"].sum())
        defective = float(quality["defective"].sum())
        defects = quality.groupby("defect_reason", dropna=False)["defective"].sum().reset_index().sort_values("defective", ascending=False).head(8)
        by_machine = quality.groupby("machine_code")[["produced", "defective"]].sum().reset_index()
        return {
            "record_scope": "Son 300 kalite kaydı",
            "produced": int(produced),
            "defective": int(defective),
            "quality_pct": round((produced - defective) / produced * 100, 1) if produced else 0,
            "defects": [{"reason": str(r["defect_reason"]), "count": int(r["defective"])} for _, r in defects.iterrows()],
            "by_machine": [{"machine": str(r["machine_code"]), "defective": int(r["defective"]), "quality_pct": round((r["produced"] - r["defective"]) / r["produced"] * 100, 1) if r["produced"] else 0} for _, r in by_machine.iterrows()],
        }

    def open_actions(self):
        actions = _safe_query(self.query, """SELECT action_no,source_type,machine_code,title,priority,status,owner_name,due_at,estimated_loss
            FROM operational_actions WHERE status NOT IN ('Tamamlandı','Doğrulandı','İptal')
            ORDER BY CASE priority WHEN 'Kritik' THEN 1 WHEN 'Yüksek' THEN 2 ELSE 3 END,due_at LIMIT 40""")
        if actions.empty:
            return {"open_count": 0, "overdue_count": 0, "estimated_loss": 0, "actions": []}
        due = pd.to_datetime(actions["due_at"], errors="coerce")
        overdue = due.dt.date < date.today()
        return {
            "open_count": int(len(actions)),
            "overdue_count": int(overdue.sum()),
            "estimated_loss": round(float(pd.to_numeric(actions["estimated_loss"], errors="coerce").fillna(0).sum()), 1),
            "actions": actions.fillna("").to_dict("records")[:20],
        }

    def compare_shifts(self):
        rows = []
        for shift, group in self.machines.groupby(self.machines["shift"].fillna("Atanmamış")):
            production = int(pd.to_numeric(group["production"], errors="coerce").fillna(0).sum())
            target = int(pd.to_numeric(group["target"], errors="coerce").fillna(0).sum())
            oees = [_oee(machine)["oee_pct"] for _, machine in group.iterrows()]
            rows.append({
                "shift": str(shift), "machines": int(len(group)), "production": production, "target": target,
                "attainment_pct": round(production / target * 100, 1) if target else 0,
                "average_oee_pct": round(sum(oees) / len(oees), 1) if oees else 0,
                "operators": sorted({str(value) for value in group["operator"].dropna() if str(value).strip()}),
            })
        return {"note": "Karşılaştırma makinelerin mevcut vardiya ataması ve anlık sayaçları üzerinden hesaplanır.", "shifts": rows}

    @property
    def machine_codes(self):
        return self.machines["machine_code"].dropna().astype(str).tolist() if not self.machines.empty else []

    def allowed_names(self):
        common = {"get_factory_summary", "get_machine_analysis", "get_open_actions"}
        role_tools = {
            "admin": set(TOOL_LABELS),
            "operator": {"get_production_performance", "get_downtime_analysis", "compare_shifts"},
            "maintenance": {"get_downtime_analysis", "get_maintenance_risks"},
            "quality": {"get_production_performance", "get_quality_summary"},
        }
        return common | role_tools.get(self.role, set())

    def definitions(self):
        descriptions = {
            "get_factory_summary": "Fabrikanın güncel üretim, hedef, OEE, alarm, bakım, iş emri ve aksiyon özetini getir.",
            "get_machine_analysis": "Belirli bir makinenin OEE bileşenlerini, üretimini, sensörünü, alarmlarını ve son duruşlarını getir.",
            "get_production_performance": "Makine bazında hedef gerçekleşme ve en büyük OEE kaybını getir.",
            "get_downtime_analysis": "Duruş sürelerini neden ve makine bazında analiz et.",
            "get_maintenance_risks": "Bakım tarihi, sensör, alarm ve makine durumuna göre açıklanabilir riskleri getir.",
            "get_quality_summary": "Kalite oranı, hata türleri ve makine bazlı kalite sonuçlarını getir.",
            "get_open_actions": "Açık ve geciken operasyon aksiyonlarını sorumluları ve tahmini kayıplarıyla getir.",
            "compare_shifts": "Vardiyaları üretim, hedef gerçekleşme ve ortalama OEE ile karşılaştır.",
        }
        tools = []
        for name in self.allowed_names():
            properties = {}
            required = []
            if name == "get_machine_analysis":
                properties = {"machine_code": {"type": "string", "description": "Makine kodu. Mevcut kodlar: " + ", ".join(self.machine_codes)}}
                required = ["machine_code"]
            tools.append({
                "type": "function", "name": name, "description": descriptions[name],
                "parameters": {"type": "object", "properties": properties, "required": required, "additionalProperties": False},
                "strict": True,
            })
        return tools

    def call(self, name, arguments):
        if name not in self.allowed_names():
            return {"error": "Bu veri aracı kullanıcı rolü için yetkili değil."}
        functions = {
            "get_factory_summary": self.factory_summary,
            "get_machine_analysis": lambda: self.machine_analysis(arguments.get("machine_code", "")),
            "get_production_performance": self.production_performance,
            "get_downtime_analysis": self.downtime_analysis,
            "get_maintenance_risks": self.maintenance_risks,
            "get_quality_summary": self.quality_summary,
            "get_open_actions": self.open_actions,
            "compare_shifts": self.compare_shifts,
        }
        return functions[name]()


def _extract_text(response):
    pieces = []
    for item in response.get("output", []):
        if item.get("type") == "message":
            for content in item.get("content", []):
                if content.get("type") == "output_text" and content.get("text"):
                    pieces.append(content["text"])
    return "\n".join(pieces).strip()


def _ask_openai(api_key, model, question, history, tools):
    instructions = """Sen TREX MES Fabrika Asistanısın. Yalnızca verilen MES araçlarının döndürdüğü verilere dayan.
Türkçe, yönetici odaklı ve net cevap ver. Önce sonucu söyle; sonra Kanıt, Olası neden ve Önerilen aksiyon başlıklarını kullan.
Sayıları ve makine kodlarını belirt. Veri yoksa açıkça söyle, tahmin uydurma. En fazla 350 kelime yaz.
Sistem salt okunurdur: işlem yaptığını söyleme; gerekiyorsa kullanıcıyı ilgili MES modülüne yönlendir."""
    conversation = []
    for item in history[-6:]:
        conversation.append({"role": item["role"], "content": item["content"]})
    conversation.append({"role": "user", "content": question})
    payload = {"model": model, "instructions": instructions, "input": conversation, "tools": tools.definitions(), "max_output_tokens": 900}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    used = []
    response = requests.post("https://api.openai.com/v1/responses", headers=headers, json=payload, timeout=50)
    if not response.ok:
        try:
            detail = response.json().get("error", {}).get("message", "API isteği başarısız")
        except Exception:
            detail = "API isteği başarısız"
        raise RuntimeError(f"OpenAI API: {detail}")
    data = response.json()
    for _ in range(3):
        calls = [item for item in data.get("output", []) if item.get("type") == "function_call"]
        if not calls:
            return _extract_text(data) or "Bu soru için yanıt üretilemedi.", used
        outputs = []
        for call in calls:
            name = call.get("name", "")
            try:
                arguments = json.loads(call.get("arguments") or "{}")
            except json.JSONDecodeError:
                arguments = {}
            result = tools.call(name, arguments)
            used.append(name)
            outputs.append({"type": "function_call_output", "call_id": call.get("call_id"), "output": json.dumps(result, ensure_ascii=False, default=str)})
        follow_up = {
            "model": model, "previous_response_id": data.get("id"), "input": outputs,
            "tools": tools.definitions(), "max_output_tokens": 900,
        }
        response = requests.post("https://api.openai.com/v1/responses", headers=headers, json=follow_up, timeout=50)
        if not response.ok:
            raise RuntimeError("OpenAI API araç sonucu işlenemedi.")
        data = response.json()
    return _extract_text(data) or "Analiz araç sınırına ulaştı; sorunuzu biraz daraltın.", used


def _local_answer(question, tools):
    text = question.casefold()
    used = ["get_factory_summary"]
    summary = tools.factory_summary()
    lines = [
        f"**Yönetici özeti:** Üretim {summary['production']:,}/{summary['target']:,} adet ve hedef gerçekleşme %{summary['target_attainment_pct']:.1f}. Ortalama OEE %{summary['average_oee_pct']:.1f}.",
        f"**Kritik görünüm:** {sum(summary['open_alarms'].values())} açık alarm, {summary['late_work_orders']} geciken iş emri, {summary['overdue_maintenance']} geciken bakım ve {summary['overdue_actions']} geciken aksiyon var.",
    ]
    if "bakım" in text or "risk" in text:
        used.append("get_maintenance_risks")
        risks = tools.maintenance_risks()["risks"][:3]
        lines.append("**Öncelikli bakım:** " + "; ".join(f"{item['machine']} · {item['risk_score']}/100 ({', '.join(item['reasons'])})" for item in risks))
    elif "duruş" in text or "kayıp" in text:
        used.append("get_downtime_analysis")
        stop = tools.downtime_analysis()
        reasons = "; ".join(f"{item['reason']}: {item['minutes']:.0f} dk" for item in stop["by_reason"][:3])
        lines.append(f"**Duruş odağı:** Toplam {stop['total_minutes']:.0f} dk. {reasons or 'Neden kaydı bulunamadı.'}")
    elif "vardiya" in text:
        used.append("compare_shifts")
        shifts = tools.compare_shifts()["shifts"]
        lines.append("**Vardiyalar:** " + "; ".join(f"{item['shift']} %{item['attainment_pct']:.1f} hedef, %{item['average_oee_pct']:.1f} OEE" for item in shifts))
    elif "aksiyon" in text or "gecik" in text:
        used.append("get_open_actions")
        actions = tools.open_actions()
        lines.append(f"**Aksiyon yükü:** {actions['open_count']} açık, {actions['overdue_count']} gecikmiş aksiyon; tahmini kayıp {actions['estimated_loss']:.1f}.")
    else:
        weak = sorted(summary["machines"], key=lambda item: item["oee_pct"])[:2]
        lines.append("**Öncelik:** " + "; ".join(f"{item['machine']} OEE %{item['oee_pct']:.1f}, hedef %{(item['production']/item['target']*100 if item['target'] else 0):.1f}" for item in weak))
    lines.append("**Önerilen aksiyon:** En düşük OEE'li makineyi, açık alarmı ve geciken aksiyonu aynı toplantı gündeminde doğrulayın.")
    lines.append("\n*Yapay zekâ anahtarı tanımlanmadığı için bu yanıt yerel MES analiz motoruyla üretildi.*")
    return "\n\n".join(lines), used


def render_factory_ai(query, *, current_username="", current_name="", current_role="operator"):
    api_key = _setting("OPENAI_API_KEY")
    model = _setting("OPENAI_MODEL", "gpt-5-mini")
    tools = MesReadTools(query, current_role)
    summary = tools.factory_summary()
    connected = bool(api_key)

    st.markdown("""
    <style>
      .trex-ai-hero{background:linear-gradient(120deg,#063d2d 0%,#087c55 58%,#16a56d 100%);border-radius:16px;padding:20px 23px;color:white;box-shadow:0 10px 28px rgba(5,84,57,.16);margin-bottom:12px}
      .trex-ai-hero h2{font-size:23px;margin:0 0 5px;color:white}.trex-ai-hero p{margin:0;color:#d8f5e7;font-size:13px}
      .trex-ai-tags{display:flex;gap:7px;flex-wrap:wrap;margin-top:13px}.trex-ai-tag{background:rgba(255,255,255,.13);border:1px solid rgba(255,255,255,.2);border-radius:20px;padding:5px 10px;font-size:10px;font-weight:750}
      .trex-insight{border:1px solid #d5e8df;border-radius:13px;background:linear-gradient(145deg,#fff,#f3fbf7);padding:15px;margin-bottom:10px;min-height:112px}
      .trex-insight small{color:#648174;font-weight:750}.trex-insight strong{display:block;color:#07553b;font-size:22px;margin:6px 0}.trex-insight p{font-size:11px;color:#48685a;margin:0}
      .trex-ai-source{display:inline-block;background:#e8f7ef;color:#08754f;border-radius:12px;padding:3px 8px;margin:2px;font-size:9px;font-weight:750}
      [data-testid="stChatMessage"]{border:1px solid #dfebe5;border-radius:13px;padding:5px 10px;background:rgba(255,255,255,.75)}
    </style>
    """, unsafe_allow_html=True)
    mode_label = f"OpenAI · {model}" if connected else "Yerel analiz modu"
    st.markdown(f"""
      <div class="trex-ai-hero">
        <h2>TREX Fabrika Asistanı</h2>
        <p>MES verilerini okuyup üretim problemlerini kanıtlarıyla açıklayan yönetici karar desteği</p>
        <div class="trex-ai-tags"><span class="trex-ai-tag">MES verisine bağlı</span><span class="trex-ai-tag">Salt okunur</span><span class="trex-ai-tag">{mode_label}</span><span class="trex-ai-tag">Rol bazlı erişim</span></div>
      </div>
    """, unsafe_allow_html=True)

    left, right = st.columns([2.25, 1], gap="large")
    with right:
        st.markdown("#### Bugünün İçgörüsü")
        risk_machine = min(summary["machines"], key=lambda item: item["oee_pct"], default=None)
        if risk_machine:
            st.markdown(f"""<div class="trex-insight"><small>ÖNCELİKLİ MAKİNE</small><strong>{risk_machine['machine']} · %{risk_machine['oee_pct']:.1f} OEE</strong><p>{risk_machine['status']} · {risk_machine['production']:,}/{risk_machine['target']:,} adet. OEE kaybının bileşenlerini inceleyin.</p></div>""", unsafe_allow_html=True)
        st.markdown(f"""<div class="trex-insight"><small>OPERASYON NABZI</small><strong>%{summary['target_attainment_pct']:.1f} hedef</strong><p>{sum(summary['open_alarms'].values())} açık alarm · {summary['overdue_actions']} geciken aksiyon · {summary['overdue_maintenance']} geciken bakım</p></div>""", unsafe_allow_html=True)
        st.caption(f"Son analiz: {summary['as_of']}")
        with st.expander("Asistan erişimi"):
            st.write("Bu rolde kullanılabilen veri kaynakları:")
            for name in sorted(tools.allowed_names()):
                st.caption(f"• {TOOL_LABELS[name]}")
            if not connected and current_role == "admin":
                st.info("Tam yapay zekâ yanıtları için Streamlit Secrets içine `OPENAI_API_KEY` ekleyin. İsteğe bağlı model: `OPENAI_MODEL`.")

    with left:
        st.markdown("#### Ne bilmek istiyorsunuz?")
        quick_questions = [
            "Bugünün kritik durumlarını özetle",
            "En düşük OEE hangi makinede, neden?",
            "Geciken aksiyonları analiz et",
            "Bakım riski olan makineleri sırala",
            "Vardiyaları karşılaştır",
            "Yönetim toplantısı için kısa özet hazırla",
        ]
        button_cols = st.columns(3)
        selected_prompt = ""
        for index, question in enumerate(quick_questions):
            if button_cols[index % 3].button(question, key=f"ai_quick_{index}", use_container_width=True):
                selected_prompt = question

        if "factory_ai_messages" not in st.session_state:
            st.session_state["factory_ai_messages"] = [{"role": "assistant", "content": "Merhaba. Fabrikadaki üretim, OEE, duruş, bakım, kalite ve aksiyon verilerini inceleyebilirim. Bir yönetici sorusu sorun."}]
        messages = st.session_state["factory_ai_messages"]
        for message in messages[-8:]:
            with st.chat_message(message["role"]):
                st.markdown(message["content"])
                if message.get("sources"):
                    st.markdown("".join(f'<span class="trex-ai-source">{TOOL_LABELS.get(source, source)}</span>' for source in message["sources"]), unsafe_allow_html=True)

        typed_prompt = st.chat_input("Örn. CNC-02 neden hedefin gerisinde?")
        prompt = selected_prompt or typed_prompt
        if prompt:
            history = [item for item in messages if item["role"] in ("user", "assistant")]
            messages.append({"role": "user", "content": prompt})
            with st.chat_message("user"):
                st.markdown(prompt)
            with st.chat_message("assistant"):
                with st.spinner("MES verileri inceleniyor..."):
                    try:
                        if connected:
                            answer, sources = _ask_openai(api_key, model, prompt, history, tools)
                        else:
                            answer, sources = _local_answer(prompt, tools)
                    except Exception as exc:
                        answer, sources = _local_answer(prompt, tools)
                        if current_role == "admin":
                            st.warning(f"Yapay zekâ servisine erişilemedi; yerel analiz gösterildi. {str(exc)[:160]}")
                st.markdown(answer)
                st.markdown("".join(f'<span class="trex-ai-source">{TOOL_LABELS.get(source, source)}</span>' for source in dict.fromkeys(sources)), unsafe_allow_html=True)
            messages.append({"role": "assistant", "content": answer, "sources": list(dict.fromkeys(sources))})
            st.session_state["factory_ai_messages"] = messages[-12:]


