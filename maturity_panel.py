"""TREX MES açıklanabilir Dijital Fabrika Olgunluk değerlendirmesi."""
from datetime import date, datetime
import html
import json
import uuid

import pandas as pd
import plotly.graph_objects as go
import streamlit as st


WEIGHTS = {
    "Üretim Dijitalleşmesi": 0.15,
    "Makine & Otomasyon": 0.15,
    "Veri & Analitik": 0.15,
    "Kalite & SPC": 0.10,
    "Bakım Yönetimi": 0.10,
    "Enerji & WEMS": 0.10,
    "Sistem Entegrasyonu": 0.10,
    "Sürekli İyileştirme": 0.10,
    "Dijital Yetkinlik": 0.05,
}


def maturity_level(score):
    if score <= 20:
        return 0, "Dijitalleşmemiş", "#d94c4c"
    if score <= 40:
        return 1, "Temel Dijitalleşme", "#e58a2d"
    if score <= 60:
        return 2, "Bağlantılı Sistemler", "#e6b52e"
    if score <= 80:
        return 3, "Entegre Fabrika", "#20a86b"
    return 4, "Akıllı / Optimize Fabrika", "#087847"


def _count(query, table, where=""):
    try:
        frame = query(f"SELECT COUNT(*) AS total FROM {table} {where}")
        return int(frame.iloc[0]["total"]) if not frame.empty else 0
    except Exception:
        return 0


def _scalar(query, sql, default=0):
    try:
        frame = query(sql)
        if frame.empty:
            return default
        value = frame.iloc[0, 0]
        return default if pd.isna(value) else value
    except Exception:
        return default


def _signal(label, value, action, source):
    value = max(0.0, min(float(value), 100.0))
    return {"label": label, "score": value, "action": action, "source": source}


def calculate_maturity(query, *, using_postgres=False, api_configured=False):
    """Yalnızca sistemdeki gerçek özellik ve kayıtlardan açıklanabilir skor üretir."""
    # Uzak PostgreSQL/Neon üzerinde her COUNT ayrı bir ağ gidiş-dönüşü demektir.
    # Bütün olgunluk sinyallerini tek snapshot sorgusunda toplayarak modülün ilk
    # açılışını belirgin biçimde hızlandırıyoruz.
    snapshot = query("""
        SELECT
            (SELECT COUNT(*) FROM machines) AS machines,
            (SELECT COUNT(*) FROM production_history) AS production_history,
            (SELECT COUNT(*) FROM work_orders) AS work_orders,
            (SELECT COUNT(*) FROM shifts) AS shifts,
            (SELECT COUNT(*) FROM shift_machine_assignments) AS assignments,
            (SELECT COUNT(*) FROM sensors) AS sensors,
            (SELECT COUNT(*) FROM sensor_history) AS sensor_history,
            (SELECT COUNT(*) FROM alarms) AS alarms,
            (SELECT COUNT(*) FROM alarm_actions) AS alarm_actions,
            (SELECT COUNT(*) FROM quality) AS quality,
            (SELECT COUNT(*) FROM quality WHERE COALESCE(defect_reason,'') NOT IN ('','Hata yok')) AS quality_reasons,
            (SELECT COUNT(*) FROM spc_measurements) AS spc,
            (SELECT COUNT(*) FROM alarms WHERE alarm LIKE 'SPC:%') AS spc_alarms,
            (SELECT COUNT(*) FROM maintenance) AS maintenance,
            (SELECT COUNT(*) FROM maintenance_requests) AS maintenance_requests,
            (SELECT COUNT(*) FROM downtime) AS downtime,
            (SELECT COUNT(*) FROM energy_readings) AS energy,
            (SELECT COUNT(*) FROM users WHERE is_active=1) AS users,
            (SELECT COUNT(DISTINCT role) FROM users WHERE is_active=1) AS roles,
            (SELECT COUNT(*) FROM user_permissions) AS permissions,
            (SELECT COUNT(*) FROM audit_log) AS audit,
            (SELECT COUNT(*) FROM machines WHERE COALESCE(target,0)>0) AS targeted_machines,
            (SELECT COUNT(*) FROM machines WHERE status IN ('Çalışıyor','Beklemede','Arızalı')) AS status_machines,
            (SELECT COUNT(*) FROM machines WHERE planned_time>0 AND ideal_cycle>0) AS oee_ready,
            (SELECT COUNT(*) FROM machines WHERE last_maintenance IS NOT NULL AND next_maintenance IS NOT NULL) AS maintenance_dates,
            (SELECT COUNT(DISTINCT machine_code) FROM energy_readings) AS energy_machines,
            (SELECT COUNT(*) FROM maintenance WHERE maintenance_type LIKE '%Periyodik%' OR maintenance_type LIKE '%Planlı%') AS planned_maintenance,
            (SELECT COUNT(*) FROM five_why_analyses) AS five_why_analyses,
            (SELECT COUNT(*) FROM five_why_analyses WHERE status='Tamamlandı') AS completed_five_why
    """)
    values = snapshot.iloc[0].to_dict() if not snapshot.empty else {}

    def count_value(name):
        value = values.get(name, 0)
        return 0 if pd.isna(value) else int(value)

    machines = count_value("machines")
    production_history = count_value("production_history")
    work_orders = count_value("work_orders")
    shifts = count_value("shifts")
    assignments = count_value("assignments")
    sensors = count_value("sensors")
    sensor_history = count_value("sensor_history")
    alarms = count_value("alarms")
    alarm_actions = count_value("alarm_actions")
    quality = count_value("quality")
    quality_reasons = count_value("quality_reasons")
    spc = count_value("spc")
    spc_alarms = count_value("spc_alarms")
    maintenance = count_value("maintenance")
    maintenance_requests = count_value("maintenance_requests")
    downtime = count_value("downtime")
    energy = count_value("energy")
    users = count_value("users")
    roles = count_value("roles")
    permissions = count_value("permissions")
    audit = count_value("audit")
    targeted_machines = count_value("targeted_machines")
    status_machines = count_value("status_machines")
    oee_ready = count_value("oee_ready")
    maintenance_dates = count_value("maintenance_dates")
    energy_machines = count_value("energy_machines")
    planned_maintenance = count_value("planned_maintenance")
    five_why_analyses = count_value("five_why_analyses")
    completed_five_why = count_value("completed_five_why")
    five_why_score = 100 if completed_five_why else (75 if five_why_analyses else 0)

    categories = {
        "Üretim Dijitalleşmesi": [
            _signal("Üretim miktarı dijital takip", 100 if machines else 0, "Makine üretim sayaçlarını sisteme bağla", f"{machines} makine"),
            _signal("Üretim hedefleri", 100 if machines and targeted_machines == machines else (50 if targeted_machines else 0), "Tüm makineler için üretim hedefi tanımla", f"{targeted_machines}/{machines}"),
            _signal("Dijital iş emirleri", 100 if work_orders else 0, "İş emirlerini dijital olarak oluştur", f"{work_orders} kayıt"),
            _signal("Vardiya yönetimi", 100 if shifts and assignments else (50 if shifts else 0), "Vardiya-makine-operatör atamalarını tamamla", f"{shifts} vardiya · {assignments} atama"),
            _signal("Üretim geçmişi", 100 if production_history >= 20 else (50 if production_history else 0), "Üretim geçmişi toplamayı artır", f"{production_history} kayıt"),
            _signal("Hedef / gerçekleşen analizi", 100 if machines and targeted_machines else 0, "Hedef gerçekleşme KPI'larını etkinleştir", "Dashboard" if machines else "Veri yok"),
        ],
        "Makine & Otomasyon": [
            _signal("Makine envanteri", 100 if machines else 0, "Makine envanterini oluştur", f"{machines} makine"),
            _signal("Canlı makine durumu", 100 if machines and status_machines == machines else 0, "Makine durum sinyallerini bağla", f"{status_machines}/{machines}"),
            _signal("Makine bazlı OEE", 100 if oee_ready else 0, "Planlı süre ve ideal çevrim bilgilerini tamamla", f"{oee_ready} hazır"),
            _signal("Sensör kaydı", 100 if sensors else 0, "PLC/sensör veri kaynağını bağla", f"{sensors} sensör grubu"),
            _signal("Sensör geçmişi", 100 if sensor_history >= 20 else (50 if sensor_history else 0), "Sensör geçmişi toplamayı sürdür", f"{sensor_history} kayıt"),
            _signal("Gerçek zamanlı saha entegrasyonu", 0, "Gerçek PLC/CNC veri bağlantısını devreye al", "Demo/simülasyon"),
        ],
        "Veri & Analitik": [
            _signal("Merkezi veritabanı", 100 if using_postgres else 60, "Neon/PostgreSQL merkezi veritabanını kullan", "Neon PostgreSQL" if using_postgres else "Yerel SQLite"),
            _signal("Geçmiş veri", 100 if production_history and sensor_history else (50 if production_history else 0), "Üretim ve sensör geçmişini birlikte tut", f"{production_history + sensor_history} kayıt"),
            _signal("Yönetim dashboardu", 100, "Dashboard kullanımını yaygınlaştır", "Aktif"),
            _signal("KPI hesaplama", 100 if machines else 0, "KPI veri kaynaklarını tamamla", "OEE · Kalite · OLE"),
            _signal("Trend ve grafikler", 100 if production_history else 50, "Zaman serisi kayıtlarını artır", "Plotly analizleri"),
            _signal("Alarm tabanlı karar desteği", 100 if alarms else 0, "Otomatik alarm kuralları tanımla", f"{alarms} alarm"),
            _signal("Tahminleme", 60 if maintenance and sensor_history else 0, "Tahmine dayalı bakım modelini gerçek veriye bağla", "Risk analizi" if maintenance else "Yok"),
        ],
        "Kalite & SPC": [
            _signal("Hatalı ürün takibi", 100 if quality else 0, "Kalite kayıtlarını dijitalleştir", f"{quality} kayıt"),
            _signal("Hata türleri", 100 if quality_reasons else 0, "Standart hata kodları kullan", f"{quality_reasons} kayıt"),
            _signal("Kalite oranı", 100 if quality else 0, "Üretim-kalite ilişkisini kur", "Hesaplanıyor" if quality else "Yok"),
            _signal("Pareto analizi", 100 if quality_reasons else 0, "Hata nedenlerini sınıflandır", "Aktif" if quality_reasons else "Veri bekliyor"),
            _signal("SPC ölçümleri", 100 if spc >= 20 else (50 if spc else 0), "SPC ölçüm sayısını artır", f"{spc} ölçüm"),
            _signal("Kontrol limitleri", 100 if spc >= 5 else 0, "En az 5 proses ölçümü kaydet", "CL/UCL/LCL" if spc >= 5 else "Veri yetersiz"),
            _signal("SPC alarmı", 100 if spc_alarms else (50 if spc else 0), "Kontrol dışı proses alarm akışını doğrula", f"{spc_alarms} alarm"),
        ],
        "Bakım Yönetimi": [
            _signal("Bakım kayıtları", 100 if maintenance else 0, "Bakım kayıtlarını sisteme gir", f"{maintenance} kayıt"),
            _signal("Sonraki bakım tarihleri", 100 if maintenance_dates else 0, "Makine bakım tarihlerini tamamla", f"{maintenance_dates} makine"),
            _signal("Planlı bakım", 100 if planned_maintenance else 0, "Planlı bakım programı oluştur", f"{planned_maintenance} kayıt"),
            _signal("Arıza geçmişi", 100 if downtime else 0, "Arıza ve duruş nedenlerini kaydet", f"{downtime} duruş"),
            _signal("Dijital bakım talepleri", 100 if maintenance_requests else 0, "Bakım talebi iş akışını kullan", f"{maintenance_requests} talep"),
            _signal("Bakım sorumlusu/aksiyon", 100 if maintenance_requests else 0, "Teknisyen atama ve kapatma akışını kullan", "Aktif" if maintenance_requests else "Yok"),
            _signal("Predictive maintenance", 40 if sensor_history and maintenance else 0, "Tahmine dayalı bakım modelini saha verisiyle doğrula", "Ön analiz" if sensor_history else "Yok"),
        ],
        "Enerji & WEMS": [
            _signal("Elektrik tüketimi", 100 if energy else 0, "Elektrik ölçümlerini topla", f"{energy} kayıt"),
            _signal("Su tüketimi", 0, "Su sayacı entegrasyonu ekle", "Yok"),
            _signal("Doğalgaz / yakıt", 0, "Doğalgaz/yakıt sayaç entegrasyonu ekle", "Yok"),
            _signal("Makine bazlı enerji", 100 if energy_machines else 0, "Enerji verisini makine bazında bağla", f"{energy_machines} makine"),
            _signal("Ürün başına enerji", 100 if energy and production_history else 0, "Enerji-üretim oranını hesapla", "Hesaplanıyor" if energy else "Yok"),
            _signal("Enerji hedefleri", 0, "Makine ve ürün enerji hedefleri tanımla", "Yok"),
            _signal("Enerji alarmı", 0, "Enerji tüketim eşik alarmı oluştur", "Yok"),
        ],
        "Sistem Entegrasyonu": [
            _signal("Merkezi MES veritabanı", 100 if using_postgres else 60, "Merkezi PostgreSQL yapısını kullan", "Merkezi" if using_postgres else "Yerel"),
            _signal("Kalite ↔ Üretim", 100 if quality and machines else 0, "Kalite kayıtlarını üretim emrine bağla", "Bağlı" if quality else "Yok"),
            _signal("Bakım ↔ Makine", 100 if maintenance and machines else 0, "Bakım kayıtlarını makineyle ilişkilendir", "Bağlı" if maintenance else "Yok"),
            _signal("Enerji ↔ Üretim", 100 if energy and production_history else 0, "Enerji ve üretim zaman serilerini eşleştir", "Bağlı" if energy else "Yok"),
            _signal("İş emri ↔ Üretim", 100 if work_orders and production_history else 0, "İş emri gerçekleşmesini üretimden hesapla", "Bağlı" if work_orders else "Yok"),
            _signal("ERP entegrasyonu", 0, "ERP API bağlantısı geliştir", "Yok"),
            _signal("MES API", 100 if api_configured else 50, "API servisini kalıcı sunucuya bağla", "Yapılandırılmış" if api_configured else "Kod hazır"),
        ],
        "Sürekli İyileştirme": [
            _signal("Aksiyon takibi", 100 if alarm_actions else 0, "Alarm ve iyileştirme aksiyonlarını takip et", f"{alarm_actions} aksiyon"),
            _signal("Kök neden verisi", 100 if downtime and quality_reasons else (50 if downtime else 0), "Duruş ve kalite kök nedenlerini standartlaştır", "Duruş + kalite" if quality_reasons else "Kısmi"),
            _signal("5 Why analizi", five_why_score, "5 Why kök neden analizi oluştur ve aksiyonu doğrulayarak kapat", f"{five_why_analyses} analiz · {completed_five_why} tamamlandı" if five_why_analyses else "Henüz yok"),
            _signal("Önce / sonra karşılaştırması", 0, "Kaizen önce/sonra KPI kaydı ekle", "Yok"),
            _signal("Sonuç ölçümü", 50 if audit else 0, "İyileştirme kazançlarını sayısallaştır", f"{audit} denetim kaydı"),
            _signal("Yönetim önerileri", 100 if machines else 0, "Önerileri aksiyon planına dönüştür", "Akıllı Analiz"),
        ],
        "Dijital Yetkinlik": [
            _signal("Aktif kullanıcılar", 100 if users >= 2 else (50 if users else 0), "Sistemi ilgili çalışanlara aç", f"{users} kullanıcı"),
            _signal("Rol çeşitliliği", 100 if roles >= 4 else roles / 4 * 100, "Operatör, bakım ve kalite rollerini tamamla", f"{roles}/4 rol"),
            _signal("Kullanıcı yetkileri", 100 if permissions else 50, "Kullanıcı bazlı modül yetkilerini tanımla", f"{permissions} kayıt"),
            _signal("Sistem kullanım izi", 100 if audit >= 10 else (50 if audit else 0), "Dijital süreç kullanımını artır", f"{audit} işlem"),
            _signal("Dijital eğitim takibi", 0, "Dijital eğitim ve yetkinlik matrisi ekle", "Yok"),
        ],
    }

    category_rows = []
    missing = []
    for name, signals in categories.items():
        score = round(sum(item["score"] for item in signals) / len(signals), 1)
        category_rows.append({"Kategori": name, "Skor": score, "Ağırlık": WEIGHTS[name], "Kontroller": signals})
        for item in signals:
            if item["score"] < 100:
                missing.append({"Kategori": name, "Eksik Alan": item["label"], "Önerilen Aksiyon": item["action"], "Mevcut": item["source"], "Puan": item["score"], "Beklenen Kazanç": round((100 - item["score"]) / len(signals) * WEIGHTS[name], 1)})
    overall = round(sum(row["Skor"] * row["Ağırlık"] for row in category_rows), 1)
    level_number, level_name, colour = maturity_level(overall)
    five_why_gain = round(five_why_score / 6 * WEIGHTS["Sürekli İyileştirme"], 1)
    return {"overall": overall, "level_number": level_number, "level_name": level_name, "colour": colour, "categories": category_rows, "missing": sorted(missing, key=lambda item: (-item["Beklenen Kazanç"], item["Puan"])), "five_why": {"analyses": five_why_analyses, "completed": completed_five_why, "score": five_why_score, "gain": five_why_gain}}


def _save_assessment(query, execute, result, force=False):
    today = date.today().isoformat()
    existing = query("SELECT id,overall_score FROM digital_maturity_scores WHERE assessment_date=? ORDER BY id DESC LIMIT 1", (today,))
    if not existing.empty and not force:
        assessment_id = int(existing.iloc[0]["id"])
        if abs(float(existing.iloc[0]["overall_score"] or 0) - float(result["overall"])) < .01:
            return assessment_id, False
        # Gün içinde yeni bir yetenek (ör. 5 Why) devreye alındığında eski skoru
        # bırakmak yerine bugünün değerlendirmesini ve kategori ayrıntılarını yenile.
        execute("UPDATE digital_maturity_scores SET overall_score=?,maturity_level=? WHERE id=?", (result["overall"], f'{result["level_number"]} - {result["level_name"]}', assessment_id))
        execute("DELETE FROM digital_maturity_categories WHERE assessment_id=?", (assessment_id,))
    else:
        assessment_id = int(uuid.uuid4().int % 2_000_000_000) or 1
        execute("INSERT INTO digital_maturity_scores(id,assessment_date,overall_score,maturity_level,created_at) VALUES(?,?,?,?,?)", (assessment_id, today, result["overall"], f'{result["level_number"]} - {result["level_name"]}', datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    category_values = []
    category_params = []
    for row in result["categories"]:
        category_values.append("(?,?,?,?,?,?)")
        category_params.extend((int(uuid.uuid4().int % 2_000_000_000) or 1, assessment_id, row["Kategori"], row["Skor"], row["Ağırlık"], json.dumps(row["Kontroller"], ensure_ascii=False)))
    if category_values:
        execute(
            "INSERT INTO digital_maturity_categories(id,assessment_id,category_name,score,weight,details) VALUES " + ",".join(category_values),
            tuple(category_params),
        )
    return assessment_id, True


def render_maturity_panel(query, execute, *, using_postgres=False, api_configured=False):
    result = calculate_maturity(query, using_postgres=using_postgres, api_configured=api_configured)
    _save_assessment(query, execute, result)
    categories = pd.DataFrame([{key: row[key] for key in ["Kategori", "Skor", "Ağırlık"]} for row in result["categories"]])
    strongest = categories.sort_values("Skor", ascending=False).iloc[0]
    weakest = categories.sort_values("Skor").iloc[0]
    history = query("SELECT assessment_date,overall_score,maturity_level FROM digital_maturity_scores ORDER BY assessment_date,id")
    previous = float(history.iloc[-2]["overall_score"]) if len(history) > 1 else result["overall"]
    change = result["overall"] - previous

    st.markdown("""
    <style>
    .dm-hero{display:grid;grid-template-columns:180px 1fr 210px;gap:18px;align-items:center;background:linear-gradient(125deg,#073f2d,#0b8653);color:#fff;border-radius:14px;padding:18px 22px;box-shadow:0 8px 25px rgba(4,84,47,.18);margin-bottom:10px}.dm-score{font-size:2.55rem;font-weight:950}.dm-score small{font-size:.9rem}.dm-level{font-size:1.05rem;font-weight:850}.dm-track{height:10px;background:rgba(255,255,255,.22);border-radius:99px;overflow:hidden;margin:9px 0}.dm-track i{display:block;height:100%;background:#68e3a0;border-radius:99px}.dm-meta{font-size:.7rem;color:#d4f5e2}.dm-kpi{min-height:126px;height:100%;box-sizing:border-box;border:1px solid #cfe6d8;border-radius:13px;background:linear-gradient(135deg,#fff,#f3faf6);padding:13px 15px;box-shadow:0 4px 13px rgba(5,78,44,.05)}.dm-kpi-label{font-size:.67rem;color:#547164;font-weight:800}.dm-kpi-value{font-size:clamp(1.05rem,1.75vw,1.62rem);line-height:1.12;font-weight:900;color:#174b37;margin:10px 0 8px;white-space:normal!important;overflow:visible!important;text-overflow:clip!important;overflow-wrap:anywhere;word-break:normal}.dm-kpi-note{display:inline-block;font-size:.62rem;color:#08794d;background:#e4f7eb;border-radius:99px;padding:3px 7px;font-weight:750}.dm-capability{display:grid;grid-template-columns:50px 1fr 165px;gap:13px;align-items:center;border:1px solid #bfe2ce;border-left:5px solid #0b9857;background:linear-gradient(115deg,#effbf4,#fff);border-radius:11px;padding:11px 14px;margin:10px 0}.dm-cap-icon{width:42px;height:42px;border-radius:11px;background:#0b8f53;color:#fff;display:flex;align-items:center;justify-content:center;font-size:.78rem;font-weight:950}.dm-cap-main b{display:block;color:#104832;font-size:.79rem}.dm-cap-main span{display:block;color:#5e7b6c;font-size:.63rem;margin-top:4px}.dm-cap-score{text-align:right}.dm-cap-score b{display:block;color:#08794d;font-size:1.15rem}.dm-cap-score span{font-size:.59rem;color:#668174}.dm-card{border:1px solid #d6ebdf;border-radius:10px;background:linear-gradient(140deg,#fff,#f3fbf6);padding:11px 12px;min-height:112px}.dm-card b{font-size:.76rem;color:#174c36}.dm-card strong{display:block;font-size:1.35rem;color:#087847;margin:6px 0}.dm-card small{font-size:.64rem;color:#668274}.dm-action{border-left:4px solid var(--priority);background:#fff;border-radius:8px;padding:9px 11px;margin:6px 0;box-shadow:0 2px 8px rgba(5,80,44,.05)}.dm-action b{font-size:.73rem;color:#174b37}.dm-action span{display:block;font-size:.64rem;color:#6b8276;margin-top:3px}.dm-disclaimer{font-size:.68rem;color:#657f72;background:#f5faf7;border:1px solid #deeee5;border-radius:8px;padding:9px 11px;margin-top:8px}@media(max-width:900px){.dm-hero{grid-template-columns:1fr}.dm-card{min-height:auto}.dm-kpi{min-height:105px}.dm-capability{grid-template-columns:44px 1fr}.dm-cap-score{text-align:left;grid-column:2}}
    </style>
    """, unsafe_allow_html=True)
    st.markdown(f'''<div class="dm-hero"><div><div class="dm-score">{result["overall"]:.1f}<small> / 100</small></div><div class="dm-meta">Dijital Fabrika Olgunluğu</div></div><div><div class="dm-level">Seviye {result["level_number"]} · {html.escape(result["level_name"])}</div><div class="dm-track"><i style="width:{result["overall"]}%"></i></div><div class="dm-meta">Son değerlendirme: {date.today():%d.%m.%Y}</div></div><div><div class="dm-meta">Önceki skor</div><b>{previous:.1f}</b><div class="dm-meta">Değişim: {change:+.1f} puan</div></div></div>''', unsafe_allow_html=True)

    kpis = st.columns(4, gap="small")
    kpi_values = [
        ("Olgunluk Skoru", f'{result["overall"]:.1f} / 100', f'Değişim {change:+.1f} puan'),
        ("Seviye", f'{result["level_number"]} · {result["level_name"]}', "Mevcut dijital aşama"),
        ("En Güçlü Alan", str(strongest["Kategori"]), f'Skor %{strongest["Skor"]:.1f}'),
        ("Öncelikli Gelişim", str(weakest["Kategori"]), f'Skor %{weakest["Skor"]:.1f}'),
    ]
    for column, (label, value, note) in zip(kpis, kpi_values):
        column.markdown(f'<div class="dm-kpi"><div class="dm-kpi-label">{html.escape(label)}</div><div class="dm-kpi-value">{html.escape(value)}</div><span class="dm-kpi-note">{html.escape(note)}</span></div>', unsafe_allow_html=True)

    five_why = result["five_why"]
    capability_state = "Aktif" if five_why["analyses"] else "Henüz kullanılmadı"
    st.markdown(f'''<div class="dm-capability"><div class="dm-cap-icon">5W</div><div class="dm-cap-main"><b>5 Why Kök Neden Analizi · {capability_state}</b><span>Sürekli İyileştirme alanı · {five_why["analyses"]} analiz, {five_why["completed"]} tamamlanan ve doğrulanan aksiyon</span></div><div class="dm-cap-score"><b>{five_why["score"]:.0f} / 100</b><span>Toplam olgunluk katkısı +{five_why["gain"]:.1f} puan</span></div></div>''', unsafe_allow_html=True)
    if st.button("5 Why analizlerini aç", key="maturity_open_five_why", use_container_width=True):
        st.session_state["selected_module"] = "🧠 Akıllı Analiz"
        st.rerun()

    radar_col, trend_col = st.columns([1.15, 1], gap="small")
    with radar_col, st.container(border=True):
        st.markdown("#### Dijital Olgunluk Radar Haritası")
        names = categories["Kategori"].tolist()
        values = categories["Skor"].tolist()
        radar = go.Figure(go.Scatterpolar(r=values + [values[0]], theta=names + [names[0]], fill="toself", line=dict(color="#0b9257", width=3), fillcolor="rgba(11,146,87,.22)", name="Skor"))
        radar.update_layout(height=410, margin=dict(l=35,r=35,t=30,b=30), polar=dict(radialaxis=dict(range=[0,100], tickfont=dict(size=8))), showlegend=False, paper_bgcolor="rgba(0,0,0,0)")
        st.plotly_chart(radar, use_container_width=True, config={"displayModeBar":False}, key="digital_maturity_radar")
    with trend_col, st.container(border=True):
        st.markdown("#### Olgunluk Gelişim Trendi")
        history["assessment_date"] = pd.to_datetime(history["assessment_date"], errors="coerce")
        trend = go.Figure(go.Scatter(x=history["assessment_date"], y=history["overall_score"], mode="lines+markers", line=dict(color="#0b9257", width=3), fill="tozeroy", fillcolor="rgba(11,146,87,.12)"))
        trend.update_layout(height=310, margin=dict(l=15,r=10,t=20,b=25), yaxis=dict(range=[0,100], title=None), xaxis_title=None, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
        st.plotly_chart(trend, use_container_width=True, config={"displayModeBar":False}, key="digital_maturity_trend")
        if st.button("Yeni değerlendirmeyi kaydet", type="primary", use_container_width=True, key="save_maturity_assessment"):
            _save_assessment(query, execute, result, force=True)
            st.success("Güncel değerlendirme geçmişe kaydedildi.")
            st.rerun()

    st.markdown("#### Kategori Skorları")
    card_columns = st.columns(3, gap="small")
    for index, row in enumerate(result["categories"]):
        missing_count = sum(1 for item in row["Kontroller"] if item["score"] < 100)
        with card_columns[index % 3]:
            st.markdown(f'''<div class="dm-card"><b>{html.escape(row["Kategori"])}</b><strong>{row["Skor"]:.1f} / 100</strong><div class="dm-track" style="background:#dfeee5"><i style="width:{row["Skor"]}%;background:{result["colour"]}"></i></div><small>Ağırlık %{row["Ağırlık"]*100:.0f} · {missing_count} gelişim maddesi</small></div>''', unsafe_allow_html=True)

    detail_tab, action_tab, roadmap_tab = st.tabs(["Skor Detayları", "Gelişim Alanları", "Dijitalleşme Yol Haritası"])
    with detail_tab:
        selected_category = st.selectbox("Kategori", categories["Kategori"].tolist(), key="maturity_detail_category")
        detail = next(row for row in result["categories"] if row["Kategori"] == selected_category)
        detail_frame = pd.DataFrame(detail["Kontroller"]).rename(columns={"label":"Kontrol", "score":"Puan", "source":"Mevcut Kanıt", "action":"Önerilen Aksiyon"})
        detail_frame["Puan"] = detail_frame["Puan"].map(lambda value: f"{value:.0f}/100")
        st.dataframe(detail_frame[["Kontrol", "Puan", "Mevcut Kanıt", "Önerilen Aksiyon"]], hide_index=True, use_container_width=True, height=330)
    with action_tab:
        st.caption("En yüksek beklenen skor katkısına göre sıralanmıştır.")
        for index, item in enumerate(result["missing"][:10], 1):
            priority = "Kritik" if item["Puan"] == 0 and item["Beklenen Kazanç"] >= 1 else ("Yüksek" if item["Puan"] < 50 else "Orta")
            colour = "#e55258" if priority == "Kritik" else ("#efa52c" if priority == "Yüksek" else "#349bc7")
            st.markdown(f'''<div class="dm-action" style="--priority:{colour}"><b>{index}. {html.escape(item["Önerilen Aksiyon"])}</b><span>{html.escape(item["Kategori"])} · Mevcut: {html.escape(str(item["Mevcut"]))} · Tahmini katkı +{item["Beklenen Kazanç"]:.1f}</span></div>''', unsafe_allow_html=True)
    with roadmap_tab:
        next_level = min(result["level_number"] + 1, 4)
        next_name = maturity_level(81 if next_level == 4 else next_level * 20 + 1)[1]
        roadmap_columns = st.columns([1, .25, 1.5, .25, 1], gap="small")
        roadmap_columns[0].success(f'Mevcut\n\nSeviye {result["level_number"]}\n\n{result["level_name"]}')
        roadmap_columns[1].markdown("<h2 style='text-align:center'>→</h2>", unsafe_allow_html=True)
        roadmap_columns[2].warning("Öncelikler\n\n" + "\n\n".join(item["Önerilen Aksiyon"] for item in result["missing"][:3]))
        roadmap_columns[3].markdown("<h2 style='text-align:center'>→</h2>", unsafe_allow_html=True)
        roadmap_columns[4].success(f'Hedef\n\nSeviye {next_level}\n\n{next_name}')

    st.markdown('<div class="dm-disclaimer">Bu skor, TREX MES içerisindeki üretim, kalite, bakım, enerji, veri ve entegrasyon göstergeleri kullanılarak oluşturulan kurumsal dijital olgunluk göstergesidir. Resmî bir sertifikasyon, SIRI veya MEXT değerlendirmesi değildir.</div>', unsafe_allow_html=True)
    return result

