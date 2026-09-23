"""TREX MES verilerini kullanan, salt-okunur üretim karar destek asistanı."""
from __future__ import annotations

import json
import os
import re
import unicodedata
from difflib import get_close_matches
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
Kullanıcı gündelik Türkçe, kısa cümle, yazım hatası veya 'bu/peki/ona' gibi önceki mesaja gönderme kullanabilir; niyetini konuşma bağlamından çıkar.
Samimi ama profesyonel konuş. Aynı kalıp özeti tekrarlama; doğrudan sorulan konuya cevap ver. Basit selamlaşmada araç çağırman gerekmez.
Veriye dayalı soruda uygun aracı seç; belirli makine sorulursa get_machine_analysis kullan. Sonucu önce tek cümlede söyle, ardından yalnızca yararlı kanıt ve uygulanabilir öneriyi ver.
Sayıları ve makine kodlarını belirt. Veri yoksa açıkça söyle, tahmin uydurma. En fazla 280 kelime yaz.
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


def _normalise(text):
    value = str(text).casefold().translate(str.maketrans({"ı": "i", "İ": "i"}))
    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9%]+", " ", value).strip()


def _extract_machine(text, machine_codes):
    normal = _normalise(text)
    compact = normal.replace(" ", "")
    for code in machine_codes:
        if _normalise(code).replace(" ", "") in compact:
            return code
    match = re.search(r"(?:cnc|makine|makina|maikne|tezgah)\s*(?:no|numara)?\s*[- ]?(\d+)", normal)
    if match:
        number = int(match.group(1))
        for code in machine_codes:
            digits = re.findall(r"\d+", str(code))
            if digits and int(digits[-1]) == number:
                return code
    word_numbers = {"bir": 1, "iki": 2, "uc": 3, "dort": 4, "bes": 5, "alti": 6, "yedi": 7, "sekiz": 8, "dokuz": 9}
    for word, number in word_numbers.items():
        if re.search(rf"\b(?:cnc|makine|makina|maikne|tezgah)\s+{word}\b", normal):
            for code in machine_codes:
                digits = re.findall(r"\d+", str(code))
                if digits and int(digits[-1]) == number:
                    return code
    return ""


def _detect_intent(question, has_machine=False):
    normal = _normalise(question)
    tokens = normal.split()
    vocabulary = {
        "uretim", "hedef", "performans", "oee", "makine", "tezgah", "bakim", "ariza", "risk",
        "durus", "kayip", "bekleme", "vardiya", "aksiyon", "geciken", "kalite", "hata", "fire",
        "alarm", "sensor", "sicaklik", "titresim", "basinc", "ozet", "durum", "bugun", "neden",
    }
    corrected = []
    for token in tokens:
        if token in vocabulary or len(token) < 4:
            corrected.append(token)
        else:
            match = get_close_matches(token, vocabulary, n=1, cutoff=.72)
            corrected.append(match[0] if match else token)
    text = " ".join(corrected)
    if any(phrase in text for phrase in ("selam", "merhaba", "gunaydin", "iyi aksam", "nasilsin")):
        return "greeting"
    if any(phrase in text for phrase in ("ne yapabilirsin", "neler yaparsin", "yardim et", "seni nasil")):
        return "help"
    if has_machine or any(phrase in text for phrase in ("bu neden", "bu niye", "neden dusuk", "ona bak", "detay")):
        return "machine"
    scores = {
        "maintenance": sum(word in text for word in ("bakim", "ariza", "risk", "servis")),
        "downtime": sum(word in text for word in ("durus", "kayip", "bekleme", "durdu")),
        "quality": sum(word in text for word in ("kalite", "hata", "fire", "hatali", "saglam")),
        "shift": sum(word in text for word in ("vardiya", "sabah", "aksam", "gece", "operator")),
        "actions": sum(word in text for word in ("aksiyon", "geciken", "sorumlu", "termin", "gorev")),
        "production": sum(word in text for word in ("uretim", "hedef", "performans", "oee", "verim", "geride")),
        "factory": sum(word in text for word in ("ozet", "durum", "bugun", "fabrika", "nasil gidiyor", "sikinti", "kritik")),
    }
    best = max(scores, key=scores.get)
    return best if scores[best] else "factory"


def _local_answer(question, tools, history=None):
    allowed = tools.allowed_names()
    machine_code = _extract_machine(question, tools.machine_codes)
    context = st.session_state.get("factory_ai_context", {})
    normal = _normalise(question)
    follow_up = len(normal.split()) <= 7 and any(word in normal for word in ("bu", "peki", "neden", "niye", "daha", "detay", "onda"))
    if not machine_code and follow_up:
        machine_code = context.get("machine", "")
    intent = _detect_intent(question, bool(machine_code))
    if intent == "factory" and follow_up and context.get("intent"):
        intent = context["intent"]
    st.session_state["factory_ai_context"] = {"intent": intent, "machine": machine_code or context.get("machine", "")}

    if intent == "greeting":
        summary = tools.factory_summary()
        return (f"Merhaba 👋 Şu an fabrikada **{summary['production']:,}/{summary['target']:,} adet** üretim ve **%{summary['average_oee_pct']:.1f} ortalama OEE** görüyorum. "
                "İstersen bana günlük konuşur gibi ‘hangi makine sıkıntılı?’, ‘CNC iki niye düşük?’ veya ‘bakımda ne var?’ diye sorabilirsin.", ["get_factory_summary"])
    if intent == "help":
        return ("Üretim hedefini, makine OEE’sini, duruş nedenlerini, bakım risklerini, kalite kayıplarını, vardiyaları ve açık aksiyonları inceleyebilirim. "
                "Resmî cümle kurmana gerek yok; **‘bugün işler nasıl?’**, **‘CNC-03’e ne olmuş?’** ya da **‘en büyük kayıp nerede?’** demen yeterli.", [])

    if intent == "machine":
        if not machine_code:
            return "Hangi makineye bakmamı istersin? Örneğin **CNC-01** ya da sadece **makine iki** diyebilirsin.", []
        item = tools.machine_analysis(machine_code)
        if item.get("error"):
            return f"{machine_code} kodlu makineyi bulamadım. Mevcut makineler: {', '.join(tools.machine_codes)}.", ["get_machine_analysis"]
        components = {"kullanılabilirlik": item["oee"]["availability_pct"], "performans": item["oee"]["performance_pct"], "kalite": item["oee"]["quality_pct"]}
        weakest = min(components, key=components.get)
        target_pct = item["production"] / item["target"] * 100 if item["target"] else 0
        lines = [f"{machine_code}'ye baktım. Şu an **{item['status']}**, üretimi **{item['production']:,}/{item['target']:,} adet** (%{target_pct:.1f}) ve OEE’si **%{item['oee']['oee_pct']:.1f}**.",
                 f"En zayıf tarafı **{weakest} (%{components[weakest]:.1f})**. Kullanılabilirlik %{item['oee']['availability_pct']:.1f}, performans %{item['oee']['performance_pct']:.1f}, kalite %{item['oee']['quality_pct']:.1f}."]
        if item["active_alarms"]:
            alarm = item["active_alarms"][0]
            lines.append(f"Ayrıca açık **{alarm.get('level', '')}** alarmı var: {alarm.get('alarm', '')}.")
        if item["recent_downtime"]:
            minutes = sum(_number(stop.get("duration")) for stop in item["recent_downtime"])
            main_reason = max(item["recent_downtime"], key=lambda stop: _number(stop.get("duration"))).get("reason", "Kayıt yok")
            lines.append(f"Son kayıtlarda **{minutes:.0f} dakika** duruş görünüyor; en büyük neden **{main_reason}**.")
        lines.append(f"Ben olsam önce **{weakest} kaybını** ve açık alarmı doğrular, ardından Makine Detayı ekranından vardiya aksiyonu açardım.")
        return "\n\n".join(lines), ["get_machine_analysis"]

    if intent == "maintenance" and "get_maintenance_risks" in allowed:
        risks = tools.maintenance_risks()["risks"]
        risky = [item for item in risks if item["risk_score"] > 0]
        if not risky:
            return "Şu an belirgin bir bakım riski görünmüyor. Bakım tarihleri, sensörler ve açık alarmlar normal aralıkta.", ["get_maintenance_risks"]
        lead = risky[0]
        details = "\n".join(f"- **{item['machine']} · {item['risk_score']}/100:** {', '.join(item['reasons'])}" for item in risky[:4])
        return f"Bakım tarafında ilk bakacağım makine **{lead['machine']}** olur. Risk puanı **{lead['risk_score']}/100**.\n\n{details}\n\nÖnce en yüksek riskli makine için bakım talebi ve sorumlu ataması açılmalı.", ["get_maintenance_risks"]

    if intent == "downtime" and "get_downtime_analysis" in allowed:
        stop = tools.downtime_analysis()
        reasons = stop["by_reason"][:4]
        if not reasons:
            return "Kayıtlı duruş bulamadım; bu nedenle güvenilir bir kayıp sıralaması yapamıyorum.", ["get_downtime_analysis"]
        detail = "\n".join(f"- **{item['reason']}**: {item['minutes']:.0f} dk / {item['count']} olay" for item in reasons)
        top = reasons[0]
        return f"Toplam **{stop['total_minutes']:.0f} dakika** kayıtlı duruş var. En büyük kayıp **{top['reason']} ({top['minutes']:.0f} dk)**.\n\n{detail}\n\nİlk iyileştirme çalışmasını en yüksek süreli nedene açmak en fazla geri dönüşü sağlar.", ["get_downtime_analysis"]

    if intent == "quality" and "get_quality_summary" in allowed:
        quality = tools.quality_summary()
        defects = quality.get("defects", [])
        detail = "\n".join(f"- **{item['reason']}**: {item['count']} adet" for item in defects[:4]) or "- Hata türü kaydı yok."
        return f"Kalite oranı **%{quality['quality_pct']:.1f}**; son kayıtlarda **{quality['defective']} hatalı ürün** var.\n\n{detail}\n\nÖnce en sık hata türü için 5 Why açıp ilgili makine ve vardiyayla ilişkilendirmek gerekir.", ["get_quality_summary"]

    if intent == "shift" and "compare_shifts" in allowed:
        shifts = tools.compare_shifts()["shifts"]
        if not shifts:
            return "Karşılaştırabileceğim vardiya ataması bulunmuyor.", ["compare_shifts"]
        ranked = sorted(shifts, key=lambda item: item["attainment_pct"], reverse=True)
        detail = "\n".join(f"- **{item['shift']}**: hedef %{item['attainment_pct']:.1f}, OEE %{item['average_oee_pct']:.1f}, {item['production']:,} adet" for item in ranked)
        return f"Şu an hedef gerçekleşmesinde **{ranked[0]['shift']} vardiyası** önde.\n\n{detail}\n\nNot: Bu karşılaştırma makinelerin mevcut vardiya ataması ve anlık sayaçlarına dayanıyor.", ["compare_shifts"]

    if intent == "actions":
        actions = tools.open_actions()
        rows = actions.get("actions", [])
        detail = "\n".join(f"- **{item.get('priority', '')} · {item.get('title', '')}** — {item.get('owner_name') or 'Sorumlu yok'}, termin {item.get('due_at') or 'yok'}" for item in rows[:5]) or "- Açık aksiyon bulunmuyor."
        return f"Toplam **{actions['open_count']} açık**, **{actions['overdue_count']} gecikmiş** aksiyon var. Tahmini toplam kayıp **{actions['estimated_loss']:.1f}**.\n\n{detail}\n\nGeciken ve sorumlusu olmayan kayıtları önce ele almak gerekir.", ["get_open_actions"]

    if intent == "production" and "get_production_performance" in allowed:
        production = tools.production_performance()["machines"]
        ranked = sorted(production, key=lambda item: (item["attainment_pct"], item["oee_pct"]))
        detail = "\n".join(f"- **{item['machine']}**: hedef %{item['attainment_pct']:.1f}, OEE %{item['oee_pct']:.1f}, kayıp odağı {item['largest_oee_loss']}" for item in ranked)
        weak = ranked[0] if ranked else None
        if not weak:
            return "Üretim performansı için makine verisi bulamadım.", ["get_production_performance"]
        st.session_state["factory_ai_context"]["machine"] = weak["machine"]
        return f"Üretimde en fazla dikkat isteyen makine **{weak['machine']}**. Hedefi %{weak['attainment_pct']:.1f}, OEE’si %{weak['oee_pct']:.1f}.\n\n{detail}\n\n‘Peki neden?’ dersen bu makinenin alarm, duruş ve sensör detayına inerim.", ["get_production_performance"]

    summary = tools.factory_summary()
    weak = sorted(summary["machines"], key=lambda item: item["oee_pct"])[0] if summary["machines"] else None
    if weak:
        st.session_state["factory_ai_context"]["machine"] = weak["machine"]
    issue_count = sum(summary["open_alarms"].values()) + summary["late_work_orders"] + summary["overdue_maintenance"] + summary["overdue_actions"]
    tone = "Genel tablo sakin görünüyor" if issue_count == 0 else f"Bugün takip edilmesi gereken **{issue_count} konu** var"
    answer = f"{tone}. Üretim **{summary['production']:,}/{summary['target']:,} adet** (%{summary['target_attainment_pct']:.1f}), ortalama OEE **%{summary['average_oee_pct']:.1f}**. "
    if weak:
        answer += f"İlk bakacağım yer **{weak['machine']}**; OEE’si %{weak['oee_pct']:.1f}. "
    answer += f"Açık alarm {sum(summary['open_alarms'].values())}, geciken iş emri {summary['late_work_orders']}, geciken bakım {summary['overdue_maintenance']}. İstersen bunlardan birine birlikte inelim."
    return answer, ["get_factory_summary"]


def _management_snapshot(tools, summary):
    """API çağrısı yapmadan yönetim toplantısı için öncelik ve veri güveni üretir."""
    priorities = []
    alarm_total = sum(summary["open_alarms"].values())
    critical_alarms = int(summary["open_alarms"].get("Kritik", 0))
    if alarm_total:
        priorities.append({"score": min(100, 55 + critical_alarms * 15), "area": "Alarm", "title": f"{alarm_total} açık alarm", "detail": f"{critical_alarms} kritik alarm için sahip ve müdahale süresi doğrulanmalı.", "module": "Alarm Yönetimi"})
    if summary["overdue_actions"]:
        priorities.append({"score": min(100, 60 + summary["overdue_actions"] * 8), "area": "Aksiyon", "title": f"{summary['overdue_actions']} geciken aksiyon", "detail": "Sorumlu ve yeni termin kararı yönetim toplantısında netleştirilmeli.", "module": "Aksiyon Merkezi"})
    if summary["overdue_maintenance"]:
        priorities.append({"score": min(100, 55 + summary["overdue_maintenance"] * 8), "area": "Bakım", "title": f"{summary['overdue_maintenance']} geciken bakım", "detail": "Arıza riskini azaltmak için bakım sırası kapasite planıyla eşleştirilmeli.", "module": "Bakım Yönetimi"})
    if summary["late_work_orders"]:
        priorities.append({"score": min(100, 50 + summary["late_work_orders"] * 8), "area": "Üretim", "title": f"{summary['late_work_orders']} geciken iş emri", "detail": "Kalan miktar, darboğaz makine ve termin etkisi birlikte değerlendirilmelidir.", "module": "İş Emirleri"})
    for machine in sorted(summary["machines"], key=lambda item: item["oee_pct"])[:2]:
        if machine["oee_pct"] < 65:
            priorities.append({"score": min(100, round(100 - machine["oee_pct"])), "area": "OEE", "title": f"{machine['machine']} · %{machine['oee_pct']:.1f} OEE", "detail": f"{machine['status']} · {machine['production']:,}/{machine['target']:,} adet. En zayıf OEE bileşeni incelenmeli.", "module": "Makine Detayı"})
    priorities = sorted(priorities, key=lambda item: item["score"], reverse=True)[:6]

    machines = tools.machines.copy()
    checks = []
    if not machines.empty:
        for label, column in [("Operatör ataması", "operator"), ("Ürün tanımı", "product"), ("Vardiya ataması", "shift")]:
            complete = machines[column].fillna("").astype(str).str.strip().ne("").mean() * 100
            checks.append({"label": label, "score": round(float(complete))})
        checks.append({"label": "Üretim hedefi", "score": round(float((pd.to_numeric(machines["target"], errors="coerce").fillna(0) > 0).mean() * 100))})
        checks.append({"label": "Planlı süre", "score": round(float((pd.to_numeric(machines["planned_time"], errors="coerce").fillna(0) > 0).mean() * 100))})
        sensors = _safe_query(tools.query, "SELECT machine_code,timestamp FROM sensors")
        sensor_codes = set(sensors["machine_code"].dropna().astype(str)) if not sensors.empty else set()
        checks.append({"label": "Sensör kapsamı", "score": round(len(sensor_codes.intersection(tools.machine_codes)) / len(tools.machine_codes) * 100) if tools.machine_codes else 0})
    confidence = round(sum(item["score"] for item in checks) / len(checks)) if checks else 0
    return priorities, checks, confidence


def _brief_markdown(summary, priorities, current_name):
    lines = [
        "# TREX MES · Yönetim Toplantısı Brifingi",
        f"**Hazırlanma:** {summary['as_of']}  |  **Hazırlayan:** {current_name or 'TREX Fabrika Asistanı'}",
        "",
        "## Operasyon özeti",
        f"- Üretim: **{summary['production']:,} / {summary['target']:,} adet** (%{summary['target_attainment_pct']:.1f})",
        f"- Ortalama OEE: **%{summary['average_oee_pct']:.1f}**",
        f"- Açık alarm: **{sum(summary['open_alarms'].values())}**",
        f"- Geciken iş emri / bakım / aksiyon: **{summary['late_work_orders']} / {summary['overdue_maintenance']} / {summary['overdue_actions']}**",
        "",
        "## Bugünün öncelikleri",
    ]
    if priorities:
        lines.extend(f"{index}. **[{item['area']}] {item['title']}** — {item['detail']} (Öncelik {item['score']}/100)" for index, item in enumerate(priorities, 1))
    else:
        lines.append("- Kritik operasyon önceliği tespit edilmedi.")
    lines += ["", "## Toplantıda verilecek kararlar", "- İlk üç öncelik için sorumlu ve termin belirleyin.", "- Üretim hedefi üzerindeki tahmini etkiyi doğrulayın.", "- Vardiya devrinde açık kalan aksiyonları takip edin.", "", "_Bu brifing canlı MES kayıtlarından otomatik hazırlanmıştır._"]
    return "\n".join(lines)


def _render_chat_tab(tools, summary, api_key, model, current_role):
    connected = bool(api_key)
    mode = f"OpenAI · {model}" if connected else "Yerel akıllı analiz"
    head_left, head_right = st.columns([5, 1])
    with head_left:
        st.markdown(f"""<div class="trex-chat-head"><div class="trex-chat-orb">AI</div><div><strong>Fabrika hakkında bana sor</strong><p>Normal konuşabilirsin; kısa cümleleri, yazım hatalarını ve devam sorularını anlarım.</p></div><span>{mode}</span></div>""", unsafe_allow_html=True)
    with head_right:
        if st.button("Sohbeti temizle", key="clear_factory_chat", use_container_width=True):
            st.session_state.pop("factory_ai_messages", None)
            st.session_state.pop("factory_ai_context", None)
            st.rerun()

    quick_questions = ["Bugün işler nasıl?", "Hangi makineye bakmalıyız?", "Geciken iş var mı?", "En büyük kayıp ne?"]
    if "get_maintenance_risks" in tools.allowed_names():
        quick_questions.append("Bakımda sıkıntı var mı?")
    if "compare_shifts" in tools.allowed_names():
        quick_questions.append("Hangi vardiya daha iyi?")
    if "get_quality_summary" in tools.allowed_names():
        quick_questions.append("Kalitede ne sorun var?")
    button_cols = st.columns(min(4, len(quick_questions)))
    selected_prompt = ""
    for index, question in enumerate(quick_questions):
        if button_cols[index % len(button_cols)].button(question, key=f"ai_quick_{index}", use_container_width=True):
            selected_prompt = question

    if "factory_ai_messages" not in st.session_state:
        st.session_state["factory_ai_messages"] = [{"role": "assistant", "content": "Merhaba 👋 Fabrikayı seninle birlikte inceleyebilirim. Resmî cümle kurmana gerek yok; mesela **‘CNC ikiye ne olmuş?’** ya da **‘bugün nerede sıkıntı var?’** diyebilirsin."}]
    messages = st.session_state["factory_ai_messages"]
    st.markdown('<div class="trex-chat-divider"><span>CANLI MES SOHBETİ</span></div>', unsafe_allow_html=True)
    for message in messages[-10:]:
        avatar = "🦖" if message["role"] == "assistant" else "👤"
        with st.chat_message(message["role"], avatar=avatar):
            st.markdown(message["content"])
            if message.get("sources"):
                st.markdown("".join(f'<span class="trex-ai-source">{TOOL_LABELS.get(source, source)}</span>' for source in message["sources"]), unsafe_allow_html=True)

    typed_prompt = st.chat_input("Normal konuşabilirsin: ‘CNC iki niye düşük?’")
    prompt = selected_prompt or typed_prompt
    if prompt:
        history = [item for item in messages if item["role"] in ("user", "assistant")]
        messages.append({"role": "user", "content": prompt})
        with st.chat_message("user", avatar="👤"):
            st.markdown(prompt)
        with st.chat_message("assistant", avatar="🦖"):
            with st.spinner("MES kayıtlarına bakıyorum..."):
                try:
                    answer, sources = _ask_openai(api_key, model, prompt, history, tools) if connected else _local_answer(prompt, tools, history)
                except Exception as exc:
                    answer, sources = _local_answer(prompt, tools, history)
                    if current_role == "admin":
                        st.warning(f"Yapay zekâ servisine erişilemedi; yerel analiz gösterildi. {str(exc)[:160]}")
            st.markdown(answer)
            if sources:
                st.markdown("".join(f'<span class="trex-ai-source">{TOOL_LABELS.get(source, source)}</span>' for source in dict.fromkeys(sources)), unsafe_allow_html=True)
        messages.append({"role": "assistant", "content": answer, "sources": list(dict.fromkeys(sources))})
        st.session_state["factory_ai_messages"] = messages[-16:]

    footer_cols = st.columns([3, 1])
    footer_cols[0].caption(f"Veri anı: {summary['as_of']} · Yanıtlar yalnızca yetkili MES verilerine dayanır.")
    if not connected and current_role == "admin":
        footer_cols[1].caption("OpenAI anahtarı bekleniyor")


def _render_briefing_tab(summary, priorities, current_name):
    left, right = st.columns([1.55, 1], gap="large")
    with left:
        st.markdown("#### Bugünün öncelik sırası")
        if not priorities:
            st.success("Kritik öncelik görünmüyor. Standart vardiya planıyla devam edilebilir.")
        for index, item in enumerate(priorities, 1):
            colour = "#ef4444" if item["score"] >= 70 else ("#f59e0b" if item["score"] >= 45 else "#18a66a")
            st.markdown(f"""<div class="trex-priority" style="--priority:{colour}"><div class="trex-priority-rank">{index}</div><div><small>{item['area'].upper()} · {item['module']}</small><strong>{item['title']}</strong><p>{item['detail']}</p></div><b>{item['score']}</b></div>""", unsafe_allow_html=True)
    with right:
        st.markdown("#### 10 dakikalık toplantı akışı")
        for minute, title, detail in [("0–2 dk", "Emniyet ve kritik alarmlar", "Kritik alarm, arıza ve duruşları doğrula."), ("2–5 dk", "Hedef ve darboğaz", "En düşük OEE ve geciken iş emrini değerlendir."), ("5–8 dk", "Bakım ve kalite riski", "Geciken bakım ile kalite kaybını sırala."), ("8–10 dk", "Karar ve sahiplik", "İlk üç aksiyona sorumlu ve termin ver.")]:
            st.markdown(f"**{minute} · {title}**  \n{detail}")
        report = _brief_markdown(summary, priorities, current_name)
        st.download_button("Yönetim brifingini indir", report.encode("utf-8-sig"), file_name=f"trex-yonetim-brifingi-{date.today().isoformat()}.md", mime="text/markdown", use_container_width=True)
        st.caption("Brifing API kullanmadan canlı MES verilerinden hazırlanır.")


def _render_data_quality_tab(checks, confidence, summary):
    left, right = st.columns([1.35, 1], gap="large")
    with left:
        st.markdown("#### Veri güveni")
        st.markdown(f"""<div class="trex-confidence"><small>ANALİZ GÜVEN SKORU</small><strong>%{confidence}</strong><p>Makine ana verisi, vardiya ataması, hedef ve sensör kapsamı</p></div>""", unsafe_allow_html=True)
        for item in checks:
            st.write(f"**{item['label']}** · %{item['score']}")
            st.progress(item["score"] / 100)
    with right:
        st.markdown("#### Skoru yükseltmek için")
        missing = [item for item in checks if item["score"] < 100]
        if not missing:
            st.success("Temel analiz alanlarının tamamı dolu.")
        for item in sorted(missing, key=lambda value: value["score"]):
            st.markdown(f"- **{item['label']}** kapsamı %{item['score']}. Eksik makine kayıtlarını tamamlayın.")
        st.info("Bu skor yapay zekânın doğruluğunu değil, yanıt verirken kullanabildiği MES verisinin kapsamını gösterir.")
        st.caption(f"Veri anı: {summary['as_of']} · {len(summary['machines'])} makine")


def render_factory_ai(query, *, current_username="", current_name="", current_role="operator"):
    api_key = _setting("OPENAI_API_KEY")
    model = _setting("OPENAI_MODEL", "gpt-5-mini")
    tools = MesReadTools(query, current_role)
    summary = tools.factory_summary()
    connected = bool(api_key)
    priorities, quality_checks, confidence = _management_snapshot(tools, summary)

    st.markdown("""
    <style>
      .trex-ai-hero{background:linear-gradient(120deg,#063d2d 0%,#087c55 58%,#16a56d 100%);border-radius:16px;padding:20px 23px;color:white;box-shadow:0 10px 28px rgba(5,84,57,.16);margin-bottom:12px}
      .trex-ai-hero h2{font-size:23px;margin:0 0 5px;color:white}.trex-ai-hero p{margin:0;color:#d8f5e7;font-size:13px}
      .trex-ai-tags{display:flex;gap:7px;flex-wrap:wrap;margin-top:13px}.trex-ai-tag{background:rgba(255,255,255,.13);border:1px solid rgba(255,255,255,.2);border-radius:20px;padding:5px 10px;font-size:10px;font-weight:750}
      .trex-insight{border:1px solid #d5e8df;border-radius:13px;background:linear-gradient(145deg,#fff,#f3fbf7);padding:15px;margin-bottom:10px;min-height:112px}
      .trex-insight small{color:#648174;font-weight:750}.trex-insight strong{display:block;color:#07553b;font-size:22px;margin:6px 0}.trex-insight p{font-size:11px;color:#48685a;margin:0}
      .trex-ai-source{display:inline-block;background:#e8f7ef;color:#08754f;border-radius:12px;padding:3px 8px;margin:2px;font-size:9px;font-weight:750}
      .trex-chat-head{display:flex;align-items:center;gap:12px;border:1px solid #d5e9df;background:linear-gradient(120deg,#f8fdfa,#eef9f4);border-radius:14px;padding:13px 15px;margin:10px 0 8px}.trex-chat-orb{width:39px;height:39px;border-radius:12px;display:grid;place-items:center;background:linear-gradient(145deg,#087c55,#14ad70);color:white;font-weight:900;box-shadow:0 5px 14px rgba(8,124,85,.2)}.trex-chat-head>div:nth-child(2){flex:1}.trex-chat-head strong{display:block;color:#084d37;font-size:14px}.trex-chat-head p{margin:2px 0 0;color:#668075;font-size:10px}.trex-chat-head>span{background:#daf3e6;color:#08764f;border-radius:14px;padding:5px 9px;font-size:9px;font-weight:800;white-space:nowrap}.trex-chat-divider{display:flex;align-items:center;gap:10px;color:#789085;font-size:8px;font-weight:850;letter-spacing:.12em;margin:14px 0 8px}.trex-chat-divider:before,.trex-chat-divider:after{content:"";height:1px;background:#deebe5;flex:1}
      .trex-ai-kpi{border:1px solid #d8e9e0;border-radius:12px;background:#fff;padding:11px 13px;min-height:82px}.trex-ai-kpi small{font-size:9px;font-weight:800;color:#617b6f;letter-spacing:.04em}.trex-ai-kpi strong{display:block;color:#07543a;font-size:21px;line-height:30px}.trex-ai-kpi span{font-size:10px;color:#6a8377}
      .trex-priority{display:grid;grid-template-columns:30px 1fr 42px;gap:10px;align-items:center;border:1px solid #dcebe4;border-left:4px solid var(--priority);border-radius:11px;padding:10px 12px;margin:8px 0;background:#fff}.trex-priority-rank{width:27px;height:27px;border-radius:50%;display:grid;place-items:center;background:#eef8f3;color:#07543a;font-weight:850}.trex-priority small{font-size:8px;color:#6a8377;font-weight:800}.trex-priority strong{display:block;color:#113f30;font-size:12px;margin:2px 0}.trex-priority p{font-size:10px;color:#5c7469;margin:0}.trex-priority>b{color:var(--priority);font-size:17px;text-align:right}
      .trex-confidence{background:linear-gradient(135deg,#063f2e,#0d9664);border-radius:14px;padding:18px;color:white;margin-bottom:14px}.trex-confidence small{font-size:9px;color:#ccebdd;font-weight:800}.trex-confidence strong{display:block;font-size:34px}.trex-confidence p{font-size:11px;color:#d9f3e7;margin:0}
      [data-testid="stChatMessage"]{border:0;border-radius:14px;padding:8px 12px;margin:5px 0;background:#f5faf7;box-shadow:none}[data-testid="stChatMessage"]:has([data-testid="chatAvatarIcon-user"]){background:#eef8f3;margin-left:12%}[data-testid="stChatInput"]{border:1px solid #bddfce;border-radius:15px;box-shadow:0 7px 22px rgba(8,92,61,.08)}
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

    kpis = st.columns(4)
    kpi_values = [
        ("HEDEF GERÇEKLEŞME", f"%{summary['target_attainment_pct']:.1f}", f"{summary['production']:,} / {summary['target']:,} adet"),
        ("ORTALAMA OEE", f"%{summary['average_oee_pct']:.1f}", f"{len(summary['machines'])} makine"),
        ("AÇIK KRİTİKLER", str(sum(summary['open_alarms'].values()) + summary['overdue_actions']), "Alarm + geciken aksiyon"),
        ("VERİ GÜVENİ", f"%{confidence}", "Analiz kapsam skoru"),
    ]
    for column, (label, value, note) in zip(kpis, kpi_values):
        column.markdown(f'<div class="trex-ai-kpi"><small>{label}</small><strong>{value}</strong><span>{note}</span></div>', unsafe_allow_html=True)

    chat_tab, briefing_tab, quality_tab = st.tabs(["Asistana Sor", "Yönetim Brifingi", "Veri Güveni"])
    with chat_tab:
        _render_chat_tab(tools, summary, api_key, model, current_role)
    with briefing_tab:
        _render_briefing_tab(summary, priorities, current_name)
    with quality_tab:
        _render_data_quality_tab(quality_checks, confidence, summary)

