# ============================================================
# TREX SANAL FABRİKA - RAPORLAMA
# ============================================================

import pandas as pd
from io import BytesIO

from calculations import get_machine_summary


# ============================================================
# MAKİNE RAPORU
# ============================================================

def create_machine_report(machines):
    """
    Tüm makinelerin üretim ve OEE bilgilerini
    Excel'e uygun DataFrame haline getirir.
    """

    rows = []

    for machine in machines:

        summary = get_machine_summary(machine)

        rows.append({
            "Makine": summary["machine"],
            "Durum": summary["status"],
            "Ürün": summary["product"],
            "Operatör": summary["operator"],
            "Vardiya": summary["shift"],

            "Üretim": summary["production"],
            "Hedef": summary["target"],
            "Tamamlanma (%)": round(
                summary["completion"],
                1
            ),

            "Duruş (dk)": summary["downtime"],
            "Çalışma Süresi (dk)": summary[
                "operating_time"
            ],

            "Hatalı Ürün": summary["defective"],
            "Kalite (%)": round(
                summary["quality"],
                1
            ),

            "Availability (%)": round(
                summary["availability"],
                1
            ),

            "Performance (%)": round(
                summary["performance"],
                1
            ),

            "OEE (%)": round(
                summary["oee"],
                1
            )
        })

    return pd.DataFrame(rows)


# ============================================================
# DURUŞ RAPORU
# ============================================================

def create_downtime_report(downtime_data):

    if not downtime_data:
        return pd.DataFrame(
            columns=[
                "Makine",
                "Duruş Nedeni",
                "Süre (dk)"
            ]
        )

    rows = []

    for item in downtime_data:

        rows.append({
            "Makine": item["machine"],
            "Duruş Nedeni": item["reason"],
            "Süre (dk)": item["duration"]
        })

    return pd.DataFrame(rows)


# ============================================================
# ALARM RAPORU
# ============================================================

def create_alarm_report(alarms):

    if not alarms:
        return pd.DataFrame(
            columns=[
                "Makine",
                "Alarm",
                "Seviye",
                "Zaman"
            ]
        )

    rows = []

    for alarm in alarms:

        rows.append({
            "Makine": alarm["machine"],
            "Alarm": alarm["alarm"],
            "Seviye": alarm["level"],
            "Zaman": alarm["time"]
        })

    return pd.DataFrame(rows)


# ============================================================
# İŞ EMRİ RAPORU
# ============================================================

def create_work_order_report(work_orders):

    if not work_orders:
        return pd.DataFrame(
            columns=[
                "İş Emri",
                "Ürün",
                "Makine",
                "Üretim",
                "Hedef",
                "Tamamlanma (%)",
                "Durum"
            ]
        )

    rows = []

    for order in work_orders:

        rows.append({
            "İş Emri": order["work_order"],
            "Ürün": order["product"],
            "Makine": order["machine"],
            "Üretim": order["production"],
            "Hedef": order["target"],
            "Tamamlanma (%)": round(
                order["completion"],
                1
            ),
            "Durum": order["status"]
        })

    return pd.DataFrame(rows)


# ============================================================
# KALİTE RAPORU
# ============================================================

def create_quality_report(quality_data):

    if not quality_data:
        return pd.DataFrame(
            columns=[
                "Makine",
                "Ürün",
                "Hata Türü",
                "Hatalı Adet"
            ]
        )

    rows = []

    for item in quality_data:

        rows.append({
            "Makine": item["machine"],
            "Ürün": item["product"],
            "Hata Türü": item["defect_type"],
            "Hatalı Adet": item["quantity"]
        })

    return pd.DataFrame(rows)


# ============================================================
# BAKIM RAPORU
# ============================================================

def create_maintenance_report(
    maintenance_data
):

    if not maintenance_data:
        return pd.DataFrame(
            columns=[
                "Makine",
                "Bakım Türü",
                "Tarih",
                "Süre (dk)",
                "Açıklama",
                "Durum"
            ]
        )

    rows = []

    for item in maintenance_data:

        rows.append({
            "Makine": item["machine"],
            "Bakım Türü": item[
                "maintenance_type"
            ],
            "Tarih": item["date"],
            "Süre (dk)": item["duration"],
            "Açıklama": item[
                "description"
            ],
            "Durum": item["status"]
        })

    return pd.DataFrame(rows)


# ============================================================
# SENSÖR RAPORU
# ============================================================

def create_sensor_report(sensors):

    if not sensors:
        return pd.DataFrame(
            columns=[
                "Makine",
                "Sıcaklık (°C)",
                "Titreşim (mm/s)",
                "Basınç (bar)",
                "RPM",
                "Zaman"
            ]
        )

    rows = []

    for sensor in sensors:

        rows.append({
            "Makine": sensor["machine"],
            "Sıcaklık (°C)": sensor[
                "temperature"
            ],
            "Titreşim (mm/s)": sensor[
                "vibration"
            ],
            "Basınç (bar)": sensor[
                "pressure"
            ],
            "RPM": sensor["rpm"],
            "Zaman": sensor["time"]
        })

    return pd.DataFrame(rows)


# ============================================================
# EXCEL RAPORU OLUŞTURMA
# ============================================================

def create_excel_report(
    machines,
    downtime_data,
    alarms,
    work_orders,
    quality_data,
    maintenance_data,
    sensors
):
    """
    Tüm MES verilerini tek Excel dosyasında
    farklı sayfalara ayırır.
    """

    machine_df = create_machine_report(
        machines
    )

    downtime_df = create_downtime_report(
        downtime_data
    )

    alarm_df = create_alarm_report(
        alarms
    )

    work_order_df = create_work_order_report(
        work_orders
    )

    quality_df = create_quality_report(
        quality_data
    )

    maintenance_df = create_maintenance_report(
        maintenance_data
    )

    sensor_df = create_sensor_report(
        sensors
    )

    # Bellekte Excel oluştur
    output = BytesIO()

    with pd.ExcelWriter(
        output,
        engine="openpyxl"
    ) as writer:

        machine_df.to_excel(
            writer,
            sheet_name="Makine OEE",
            index=False
        )

        downtime_df.to_excel(
            writer,
            sheet_name="Duruşlar",
            index=False
        )

        alarm_df.to_excel(
            writer,
            sheet_name="Alarmlar",
            index=False
        )

        work_order_df.to_excel(
            writer,
            sheet_name="İş Emirleri",
            index=False
        )

        quality_df.to_excel(
            writer,
            sheet_name="Kalite",
            index=False
        )

        maintenance_df.to_excel(
            writer,
            sheet_name="Bakım",
            index=False
        )

        sensor_df.to_excel(
            writer,
            sheet_name="Sensörler",
            index=False
        )

    output.seek(0)

    return output.getvalue()