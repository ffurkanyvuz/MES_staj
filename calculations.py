# ============================================================
# TREX SANAL FABRİKA - HESAPLAMALAR
# ============================================================


# ============================================================
# OEE HESAPLAMA
# ============================================================

def calculate_oee(machine):
    """
    Makinenin Availability, Performance, Quality ve OEE
    değerlerini hesaplar.
    """

    planned_time = machine["planned_time"]
    downtime = machine["downtime"]
    production = machine["production"]
    defective = machine["defective"]
    ideal_cycle = machine["ideal_cycle"]

    # --------------------------------------------------------
    # Çalışma süresi
    # --------------------------------------------------------

    operating_time = planned_time - downtime

    if operating_time < 0:
        operating_time = 0

    # --------------------------------------------------------
    # Availability
    # --------------------------------------------------------

    if planned_time > 0:
        availability = operating_time / planned_time
    else:
        availability = 0

    availability = min(
        max(availability, 0),
        1
    )

    # --------------------------------------------------------
    # Performance
    # --------------------------------------------------------

    if operating_time > 0:

        raw_performance = (
            ideal_cycle * production
        ) / operating_time

        performance = min(
            raw_performance,
            1
        )

    else:
        performance = 0

    performance = min(
        max(performance, 0),
        1
    )

    # --------------------------------------------------------
    # Quality
    # --------------------------------------------------------

    if production > 0:

        good_production = (
            production - defective
        )

        if good_production < 0:
            good_production = 0

        quality = (
            good_production / production
        )

    else:
        quality = 0

    quality = min(
        max(quality, 0),
        1
    )

    # --------------------------------------------------------
    # OEE
    # --------------------------------------------------------

    oee = (
        availability
        * performance
        * quality
    )

    oee = min(
        max(oee, 0),
        1
    )

    return {
        "operating_time": operating_time,
        "availability": availability,
        "performance": performance,
        "quality": quality,
        "oee": oee
    }


# ============================================================
# ÜRETİM TAMAMLANMA ORANI
# ============================================================

def calculate_completion(production, target):

    if target <= 0:
        return 0

    completion = (
        production / target
    ) * 100

    return min(
        max(completion, 0),
        100
    )


# ============================================================
# KALİTE ORANI
# ============================================================

def calculate_quality_rate(production, defective):

    if production <= 0:
        return 0

    good_production = (
        production - defective
    )

    if good_production < 0:
        good_production = 0

    quality_rate = (
        good_production / production
    ) * 100

    return min(
        max(quality_rate, 0),
        100
    )


# ============================================================
# HATA ORANI
# ============================================================

def calculate_defect_rate(production, defective):

    if production <= 0:
        return 0

    defect_rate = (
        defective / production
    ) * 100

    return min(
        max(defect_rate, 0),
        100
    )


# ============================================================
# DURUŞ ORANI
# ============================================================

def calculate_downtime_rate(
    planned_time,
    downtime
):

    if planned_time <= 0:
        return 0

    downtime_rate = (
        downtime / planned_time
    ) * 100

    return min(
        max(downtime_rate, 0),
        100
    )


# ============================================================
# MAKİNE ÖZETİ
# ============================================================

def get_machine_summary(machine):

    oee = calculate_oee(machine)

    completion = calculate_completion(
        machine["production"],
        machine["target"]
    )

    quality_rate = calculate_quality_rate(
        machine["production"],
        machine["defective"]
    )

    defect_rate = calculate_defect_rate(
        machine["production"],
        machine["defective"]
    )

    downtime_rate = calculate_downtime_rate(
        machine["planned_time"],
        machine["downtime"]
    )

    return {
        "machine": machine["machine"],
        "status": machine["status"],
        "product": machine["product"],
        "operator": machine["operator"],
        "shift": machine["shift"],

        "production": machine["production"],
        "target": machine["target"],
        "defective": machine["defective"],
        "downtime": machine["downtime"],

        "completion": completion,

        "availability": (
            oee["availability"] * 100
        ),

        "performance": (
            oee["performance"] * 100
        ),

        "quality": (
            oee["quality"] * 100
        ),

        "oee": (
            oee["oee"] * 100
        ),

        "quality_rate": quality_rate,

        "defect_rate": defect_rate,

        "downtime_rate": downtime_rate,

        "operating_time": (
            oee["operating_time"]
        )
    }


# ============================================================
# TÜM FABRİKA ÖZETİ
# ============================================================

def calculate_factory_summary(machines):

    if not machines:

        return {
            "total_production": 0,
            "total_target": 0,
            "total_defective": 0,
            "total_downtime": 0,
            "average_oee": 0,
            "average_availability": 0,
            "average_performance": 0,
            "average_quality": 0,
            "completion": 0
        }

    total_production = sum(
        machine["production"]
        for machine in machines
    )

    total_target = sum(
        machine["target"]
        for machine in machines
    )

    total_defective = sum(
        machine["defective"]
        for machine in machines
    )

    total_downtime = sum(
        machine["downtime"]
        for machine in machines
    )

    summaries = [
        get_machine_summary(machine)
        for machine in machines
    ]

    average_oee = sum(
        item["oee"]
        for item in summaries
    ) / len(summaries)

    average_availability = sum(
        item["availability"]
        for item in summaries
    ) / len(summaries)

    average_performance = sum(
        item["performance"]
        for item in summaries
    ) / len(summaries)

    average_quality = sum(
        item["quality"]
        for item in summaries
    ) / len(summaries)

    completion = calculate_completion(
        total_production,
        total_target
    )

    return {
        "total_production": total_production,
        "total_target": total_target,
        "total_defective": total_defective,
        "total_downtime": total_downtime,

        "average_oee": average_oee,

        "average_availability": (
            average_availability
        ),

        "average_performance": (
            average_performance
        ),

        "average_quality": (
            average_quality
        ),

        "completion": completion
    }


# ============================================================
# EN İYİ MAKİNE
# ============================================================

def get_best_machine(machines):

    if not machines:
        return None

    best_machine = max(
        machines,
        key=lambda machine:
        calculate_oee(machine)["oee"]
    )

    return best_machine


# ============================================================
# EN PROBLEMLİ MAKİNE
# ============================================================

def get_problem_machine(machines):

    if not machines:
        return None

    problem_machine = min(
        machines,
        key=lambda machine:
        calculate_oee(machine)["oee"]
    )

    return problem_machine


# ============================================================
# DURUŞ TOPLAMI
# ============================================================

def calculate_total_downtime(downtime_data):

    if not downtime_data:
        return 0

    return sum(
        item["duration"]
        for item in downtime_data
    )


# ============================================================
# EN FAZLA DURUŞ NEDENİ
# ============================================================

def get_top_downtime_reason(downtime_data):

    if not downtime_data:
        return None

    reasons = {}

    for item in downtime_data:

        reason = item["reason"]

        if reason not in reasons:
            reasons[reason] = 0

        reasons[reason] += item["duration"]

    top_reason = max(
        reasons,
        key=reasons.get
    )

    return {
        "reason": top_reason,
        "duration": reasons[top_reason]
    }


# ============================================================
# ALARM SAYILARI
# ============================================================

def calculate_alarm_summary(alarms):

    critical = sum(
        1
        for alarm in alarms
        if alarm["level"] == "Kritik"
    )

    warning = sum(
        1
        for alarm in alarms
        if alarm["level"] == "Uyarı"
    )

    info = sum(
        1
        for alarm in alarms
        if alarm["level"] == "Bilgi"
    )

    return {
        "total": len(alarms),
        "critical": critical,
        "warning": warning,
        "info": info
    }


# ============================================================
# SENSÖR DURUMU
# ============================================================

def get_sensor_status(sensor):

    temperature = sensor["temperature"]
    vibration = sensor["vibration"]
    pressure = sensor["pressure"]

    if (
        temperature >= 95
        or vibration >= 10
        or pressure < 4
    ):
        return "Kritik"

    if (
        temperature >= 75
        or vibration >= 4
        or pressure < 5
    ):
        return "Uyarı"

    return "Normal"