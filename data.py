# ============================================================
# TREX SANAL FABRİKA - DATA
# ============================================================

from datetime import datetime


# ============================================================
# MAKİNELER
# ============================================================

machines = [
    {
        "machine": "CNC-01",
        "status": "Çalışıyor",
        "production": 850,
        "target": 1000,
        "planned_time": 480,
        "downtime": 30,
        "ideal_cycle": 0.50,
        "defective": 20,
        "product": "Mil Parçası",
        "operator": "Ahmet Yılmaz",
        "shift": "Sabah",
        "last_maintenance": "2026-09-10",
        "next_maintenance": "2026-09-20"
    },

    {
        "machine": "CNC-02",
        "status": "Çalışıyor",
        "production": 650,
        "target": 900,
        "planned_time": 480,
        "downtime": 60,
        "ideal_cycle": 0.58,
        "defective": 15,
        "product": "Flanş Parçası",
        "operator": "Mehmet Demir",
        "shift": "Sabah",
        "last_maintenance": "2026-09-08",
        "next_maintenance": "2026-09-18"
    },

    {
        "machine": "CNC-03",
        "status": "Beklemede",
        "production": 500,
        "target": 800,
        "planned_time": 480,
        "downtime": 120,
        "ideal_cycle": 0.60,
        "defective": 30,
        "product": "Gövde Parçası",
        "operator": "Ali Kaya",
        "shift": "Sabah",
        "last_maintenance": "2026-09-05",
        "next_maintenance": "2026-09-16"
    }
]


# ============================================================
# DURUŞ VERİLERİ
# ============================================================

downtime_data = [
    {
        "machine": "CNC-01",
        "reason": "Bakım",
        "duration": 15
    },
    {
        "machine": "CNC-01",
        "reason": "Malzeme Bekleme",
        "duration": 10
    },
    {
        "machine": "CNC-01",
        "reason": "Arıza",
        "duration": 5
    },

    {
        "machine": "CNC-02",
        "reason": "Malzeme Bekleme",
        "duration": 40
    },
    {
        "machine": "CNC-02",
        "reason": "Operatör Bekleme",
        "duration": 20
    },

    {
        "machine": "CNC-03",
        "reason": "Arıza",
        "duration": 80
    },
    {
        "machine": "CNC-03",
        "reason": "Bakım",
        "duration": 40
    }
]


# ============================================================
# ALARMLAR
# ============================================================

alarms = [
    {
        "machine": "CNC-03",
        "alarm": "Motor sıcaklığı yüksek",
        "level": "Kritik",
        "time": "14:32"
    },

    {
        "machine": "CNC-02",
        "alarm": "Titreşim seviyesi yükseldi",
        "level": "Uyarı",
        "time": "14:28"
    },

    {
        "machine": "CNC-01",
        "alarm": "Takım değişimi yaklaşıyor",
        "level": "Bilgi",
        "time": "14:15"
    }
]


# ============================================================
# İŞ EMİRLERİ
# ============================================================

work_orders = [
    {
        "work_order": "WO-2026-001",
        "product": "Mil Parçası",
        "machine": "CNC-01",
        "production": 850,
        "target": 1000,
        "completion": 85.0,
        "status": "Üretimde"
    },

    {
        "work_order": "WO-2026-002",
        "product": "Flanş Parçası",
        "machine": "CNC-02",
        "production": 650,
        "target": 900,
        "completion": 72.2,
        "status": "Üretimde"
    },

    {
        "work_order": "WO-2026-003",
        "product": "Gövde Parçası",
        "machine": "CNC-03",
        "production": 500,
        "target": 800,
        "completion": 62.5,
        "status": "Beklemede"
    }
]


# ============================================================
# SENSÖRLER
# ============================================================

sensors = [
    {
        "machine": "CNC-01",
        "temperature": 68.5,
        "vibration": 2.1,
        "pressure": 6.2,
        "rpm": 1450,
        "time": datetime.now().strftime("%H:%M:%S")
    },

    {
        "machine": "CNC-02",
        "temperature": 74.2,
        "vibration": 3.8,
        "pressure": 5.9,
        "rpm": 1320,
        "time": datetime.now().strftime("%H:%M:%S")
    },

    {
        "machine": "CNC-03",
        "temperature": 87.4,
        "vibration": 8.2,
        "pressure": 5.1,
        "rpm": 0,
        "time": datetime.now().strftime("%H:%M:%S")
    }
]


# ============================================================
# OPERATÖRLER
# ============================================================

operators = [
    {
        "id": "OP-001",
        "name": "Ahmet Yılmaz",
        "machine": "CNC-01",
        "shift": "Sabah",
        "status": "Aktif"
    },

    {
        "id": "OP-002",
        "name": "Mehmet Demir",
        "machine": "CNC-02",
        "shift": "Sabah",
        "status": "Aktif"
    },

    {
        "id": "OP-003",
        "name": "Ali Kaya",
        "machine": "CNC-03",
        "shift": "Sabah",
        "status": "Beklemede"
    }
]


# ============================================================
# ÜRÜNLER
# ============================================================

products = [
    {
        "product_code": "PRD-001",
        "product": "Mil Parçası",
        "target_cycle": 0.50,
        "unit": "Adet"
    },

    {
        "product_code": "PRD-002",
        "product": "Flanş Parçası",
        "target_cycle": 0.58,
        "unit": "Adet"
    },

    {
        "product_code": "PRD-003",
        "product": "Gövde Parçası",
        "target_cycle": 0.60,
        "unit": "Adet"
    }
]


# ============================================================
# KALİTE / HATA TÜRLERİ
# ============================================================

quality_data = [
    {
        "machine": "CNC-01",
        "product": "Mil Parçası",
        "defect_type": "Ölçü Hatası",
        "quantity": 8
    },

    {
        "machine": "CNC-01",
        "product": "Mil Parçası",
        "defect_type": "Yüzey Hatası",
        "quantity": 7
    },

    {
        "machine": "CNC-01",
        "product": "Mil Parçası",
        "defect_type": "Çapak",
        "quantity": 5
    },

    {
        "machine": "CNC-02",
        "product": "Flanş Parçası",
        "defect_type": "Ölçü Hatası",
        "quantity": 6
    },

    {
        "machine": "CNC-02",
        "product": "Flanş Parçası",
        "defect_type": "Yüzey Hatası",
        "quantity": 5
    },

    {
        "machine": "CNC-02",
        "product": "Flanş Parçası",
        "defect_type": "Çapak",
        "quantity": 4
    },

    {
        "machine": "CNC-03",
        "product": "Gövde Parçası",
        "defect_type": "Ölçü Hatası",
        "quantity": 12
    },

    {
        "machine": "CNC-03",
        "product": "Gövde Parçası",
        "defect_type": "Yüzey Hatası",
        "quantity": 10
    },

    {
        "machine": "CNC-03",
        "product": "Gövde Parçası",
        "defect_type": "Çapak",
        "quantity": 8
    }
]


# ============================================================
# BAKIM KAYITLARI
# ============================================================

maintenance_data = [
    {
        "machine": "CNC-01",
        "maintenance_type": "Planlı Bakım",
        "date": "2026-09-10",
        "duration": 15,
        "description": "Takım ve yağlama kontrolü",
        "status": "Tamamlandı"
    },

    {
        "machine": "CNC-02",
        "maintenance_type": "Planlı Bakım",
        "date": "2026-09-08",
        "duration": 30,
        "description": "Hidrolik sistem kontrolü",
        "status": "Tamamlandı"
    },

    {
        "machine": "CNC-03",
        "maintenance_type": "Arıza Bakımı",
        "date": "2026-09-15",
        "duration": 80,
        "description": "Motor sıcaklığı nedeniyle bakım",
        "status": "Devam Ediyor"
    }
]


# ============================================================
# VARDİYALAR
# ============================================================

shifts = [
    {
        "shift": "Sabah",
        "start": "08:00",
        "end": "16:00",
        "operator_count": 3
    },

    {
        "shift": "Akşam",
        "start": "16:00",
        "end": "00:00",
        "operator_count": 2
    },

    {
        "shift": "Gece",
        "start": "00:00",
        "end": "08:00",
        "operator_count": 2
    }
]


# ============================================================
# FABRİKA BİLGİSİ
# ============================================================

factory_info = {
    "name": "TREX Sanal Fabrika",
    "location": "Türkiye",
    "department": "CNC Üretim",
    "machine_count": len(machines),
    "shift_count": len(shifts),
    "system_status": "Online"
}