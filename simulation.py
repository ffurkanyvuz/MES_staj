# ============================================================
# TREX SANAL FABRİKA - CANLI SİMÜLASYON
# ============================================================

import random
from datetime import datetime

from data import machines, sensors, alarms


# ============================================================
# MAKİNE ÜRETİM SİMÜLASYONU
# ============================================================

def simulate_production():
    """
    Çalışan makinelerin üretimini artırır.
    Beklemede veya arızalı makineler üretim yapmaz.
    """

    for machine in machines:

        if machine["status"] == "Çalışıyor":

            # Her API çağrısında küçük miktarda üretim
            production_increment = random.randint(1, 5)

            machine["production"] += production_increment

            # Hedefi geçmesini engelle
            if machine["production"] > machine["target"]:
                machine["production"] = machine["target"]


# ============================================================
# SENSÖR SİMÜLASYONU
# ============================================================

def simulate_sensors():
    """
    Makinelerin sıcaklık, titreşim, basınç ve RPM
    değerlerini sanal olarak değiştirir.
    """

    for sensor in sensors:

        machine_name = sensor["machine"]

        # İlgili makineyi bul
        machine = next(
            (
                m
                for m in machines
                if m["machine"] == machine_name
            ),
            None
        )

        if machine is None:
            continue

        # ----------------------------------------------------
        # MAKİNE ÇALIŞIYORSA
        # ----------------------------------------------------

        if machine["status"] == "Çalışıyor":

            sensor["temperature"] += random.uniform(
                -1.0,
                1.5
            )

            sensor["vibration"] += random.uniform(
                -0.15,
                0.20
            )

            sensor["pressure"] += random.uniform(
                -0.10,
                0.10
            )

            sensor["rpm"] += random.randint(
                -30,
                30
            )

        # ----------------------------------------------------
        # MAKİNE BEKLEMEDEYSE
        # ----------------------------------------------------

        elif machine["status"] == "Beklemede":

            sensor["temperature"] += random.uniform(
                -0.5,
                0.3
            )

            sensor["vibration"] += random.uniform(
                -0.05,
                0.05
            )

            sensor["rpm"] = 0

        # ----------------------------------------------------
        # MAKİNE ARIZALIYSA
        # ----------------------------------------------------

        elif machine["status"] == "Arızalı":

            sensor["temperature"] += random.uniform(
                0.5,
                2.0
            )

            sensor["vibration"] += random.uniform(
                0.2,
                0.6
            )

            sensor["pressure"] += random.uniform(
                -0.2,
                0.1
            )

            sensor["rpm"] = 0

        # ----------------------------------------------------
        # SINIRLAR
        # ----------------------------------------------------

        sensor["temperature"] = max(
            20,
            min(sensor["temperature"], 100)
        )

        sensor["vibration"] = max(
            0,
            min(sensor["vibration"], 12)
        )

        sensor["pressure"] = max(
            0,
            min(sensor["pressure"], 8)
        )

        sensor["rpm"] = max(
            0,
            min(sensor["rpm"], 1800)
        )

        sensor["time"] = datetime.now().strftime(
            "%H:%M:%S"
        )


# ============================================================
# MAKİNE DURUM SİMÜLASYONU
# ============================================================

def simulate_machine_status():
    """
    Makinelerin durumlarını rastgele değiştirir.

    Çok sık durum değiştirmemesi için düşük ihtimaller
    kullanılmıştır.
    """

    for machine in machines:

        # ----------------------------------------------------
        # ÇALIŞAN MAKİNE
        # ----------------------------------------------------

        if machine["status"] == "Çalışıyor":

            random_value = random.random()

            # Küçük ihtimalle beklemeye geç
            if random_value < 0.02:

                machine["status"] = "Beklemede"

                machine["downtime"] += 1

            # Çok küçük ihtimalle arızalan
            elif random_value < 0.025:

                machine["status"] = "Arızalı"

                machine["downtime"] += 1

        # ----------------------------------------------------
        # BEKLEYEN MAKİNE
        # ----------------------------------------------------

        elif machine["status"] == "Beklemede":

            random_value = random.random()

            # Beklemeden çalışmaya geçebilir
            if random_value < 0.10:

                machine["status"] = "Çalışıyor"

        # ----------------------------------------------------
        # ARIZALI MAKİNE
        # ----------------------------------------------------

        elif machine["status"] == "Arızalı":

            random_value = random.random()

            # Arızanın giderilme ihtimali
            if random_value < 0.08:

                machine["status"] = "Çalışıyor"

            else:

                machine["downtime"] += 1


# ============================================================
# ALARM OLUŞTURMA
# ============================================================

def generate_alarms():
    """
    Sensör değerlerini kontrol eder ve gerektiğinde
    otomatik alarm oluşturur.
    """

    current_time = datetime.now().strftime(
        "%H:%M:%S"
    )

    # Önce otomatik oluşturulmuş alarmları temizle
    alarms.clear()

    for sensor in sensors:

        machine_name = sensor["machine"]

        temperature = sensor["temperature"]
        vibration = sensor["vibration"]
        pressure = sensor["pressure"]

        # ----------------------------------------------------
        # SICAKLIK ALARMI
        # ----------------------------------------------------

        if temperature >= 85:

            alarms.append({
                "machine": machine_name,
                "alarm": "Motor sıcaklığı kritik seviyede",
                "level": "Kritik",
                "time": current_time
            })

        elif temperature >= 75:

            alarms.append({
                "machine": machine_name,
                "alarm": "Motor sıcaklığı yükseldi",
                "level": "Uyarı",
                "time": current_time
            })

        # ----------------------------------------------------
        # TİTREŞİM ALARMI
        # ----------------------------------------------------

        if vibration >= 7:

            alarms.append({
                "machine": machine_name,
                "alarm": "Titreşim seviyesi kritik",
                "level": "Kritik",
                "time": current_time
            })

        elif vibration >= 4:

            alarms.append({
                "machine": machine_name,
                "alarm": "Titreşim seviyesi yükseldi",
                "level": "Uyarı",
                "time": current_time
            })

        # ----------------------------------------------------
        # BASINÇ ALARMI
        # ----------------------------------------------------

        if pressure < 4:

            alarms.append({
                "machine": machine_name,
                "alarm": "Basınç seviyesi düşük",
                "level": "Kritik",
                "time": current_time
            })

        elif pressure < 5:

            alarms.append({
                "machine": machine_name,
                "alarm": "Basınç seviyesi düşüyor",
                "level": "Uyarı",
                "time": current_time
            })


# ============================================================
# ARIZA KONTROLÜ
# ============================================================

def apply_fault_logic():
    """
    Kritik sensör değerleri oluştuğunda makinenin
    arızalı duruma geçmesini sağlar.
    """

    for sensor in sensors:

        machine_name = sensor["machine"]

        machine = next(
            (
                m
                for m in machines
                if m["machine"] == machine_name
            ),
            None
        )

        if machine is None:
            continue

        # Kritik sıcaklık
        if sensor["temperature"] >= 95:

            machine["status"] = "Arızalı"

        # Kritik titreşim
        elif sensor["vibration"] >= 10:

            machine["status"] = "Arızalı"


# ============================================================
# ANA SİMÜLASYON FONKSİYONU
# ============================================================

def run_simulation():
    """
    Tüm sanal fabrika simülasyonunu tek seferde çalıştırır.

    Sıra:
    1. Makine durumu
    2. Üretim
    3. Sensörler
    4. Kritik arıza kontrolü
    5. Alarmlar
    """

    simulate_machine_status()

    simulate_production()

    simulate_sensors()

    apply_fault_logic()

    generate_alarms()


# ============================================================
# SİSTEM DURUMU
# ============================================================

def get_system_status():

    running = sum(
        1
        for machine in machines
        if machine["status"] == "Çalışıyor"
    )

    waiting = sum(
        1
        for machine in machines
        if machine["status"] == "Beklemede"
    )

    faulty = sum(
        1
        for machine in machines
        if machine["status"] == "Arızalı"
    )

    return {
        "system": "Online",
        "machine_count": len(machines),
        "running": running,
        "waiting": waiting,
        "faulty": faulty,
        "alarm_count": len(alarms),
        "time": datetime.now().strftime(
            "%H:%M:%S"
        )
    }