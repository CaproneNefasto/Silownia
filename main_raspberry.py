import subprocess
import socket
import time
import json
import atexit
import statistics
from mpu9250_jmdev.registers import *
from mpu9250_jmdev.mpu_9250 import MPU9250

# ── Parametry analizy (identyczne jak w webapp) ──────────────────────────────
SMOOTH_WINDOW        = 7     # okno wygładzania (nieparzyste: 1, 3, 5, 7)
COOLDOWN_SAMPLES     = 10    # minimalna odległość między szczytami
THRESHOLD_SENSITIVITY = 0.25 # czułość progu (0.1–1.0)
SAMPLE_INTERVAL      = 0.05  # 20 Hz – czas między pomiarami [s]
# ─────────────────────────────────────────────────────────────────────────────

agent_process = None


# ── Bluetooth ─────────────────────────────────────────────────────────────────
def przygotuj_bluetooth():
    global agent_process
    print("Konfiguracja modułu Bluetooth...")
    subprocess.run(["sudo", "rfkill", "unblock", "bluetooth"], check=False)
    subprocess.run(["bluetoothctl", "power", "on"],            check=False)
    subprocess.run(["bluetoothctl", "agent", "off"],           stdout=subprocess.DEVNULL)
    subprocess.run(["sudo", "killall", "bt-agent"],            stderr=subprocess.DEVNULL)
    agent_process = subprocess.Popen(["bt-agent", "-c", "NoInputNoOutput"])
    time.sleep(1)
    subprocess.run(["sudo", "chmod", "777", "/var/run/sdp"],   check=False)
    subprocess.run(["sudo", "sdptool", "add", "SP"],           check=False)
    subprocess.run(["bluetoothctl", "discoverable", "on"],     check=False)
    subprocess.run(["bluetoothctl", "pairable",    "on"],      check=False)
    print("Bluetooth WIDOCZNY – możesz parować bez PINu.")


def ukryj_bluetooth():
    global agent_process
    print("\nUkrywanie Bluetooth i zamykanie agenta...")
    subprocess.run(["bluetoothctl", "discoverable", "off"], check=False)
    subprocess.run(["bluetoothctl", "pairable",    "off"], check=False)
    if agent_process:
        agent_process.terminate()


atexit.register(ukryj_bluetooth)


# ── Analiza danych ─────────────────────────────────────────────────────────────
def moving_average(values, window):
    """Wygładzanie sygnału – redukuje szum czujnika."""
    result = []
    half = window // 2
    for i in range(len(values)):
        start = max(0, i - half)
        end   = min(len(values) - 1, i + half)
        chunk = values[start:end + 1]
        result.append(sum(chunk) / len(chunk))
    return result


def calculate_reps(data):
    """
    Zwraca (liczba_powtorzeń, szczyt).
    data – lista słowników {"accelerometer": [x, y, z]}
    """
    if len(data) < 5:
        return 0, 0.0

    axes_signals = {
        "x": [d["accelerometer"][0] for d in data],
        "y": [d["accelerometer"][1] for d in data],
        "z": [d["accelerometer"][2] for d in data],
    }

    # Wybór osi z największą wariancją (dominująca oś ruchu)
    variances    = {ax: statistics.variance(vals) for ax, vals in axes_signals.items()}
    dominant_axis = max(variances, key=variances.get)
    raw_signal   = axes_signals[dominant_axis]
    smoothed     = moving_average(raw_signal, SMOOTH_WINDOW)

    mean_val  = statistics.mean(smoothed)
    stdev_val = statistics.stdev(smoothed)
    threshold = mean_val + THRESHOLD_SENSITIVITY * stdev_val

    reps             = 0
    max_val          = max(smoothed)
    cooldown_counter = 0

    for i in range(1, len(smoothed) - 1):
        if cooldown_counter > 0:
            cooldown_counter -= 1
            continue
        if (smoothed[i] > threshold
                and smoothed[i] > smoothed[i - 1]
                and smoothed[i] > smoothed[i + 1]):
            reps += 1
            cooldown_counter = COOLDOWN_SAMPLES

    return reps, round(max_val, 2)


# ── Pętla główna ───────────────────────────────────────────────────────────────
def send_line(sock, text):
    """Wysyła linię tekstu zakończoną \\n przez Bluetooth."""
    sock.sendall((text + "\n").encode("utf-8"))


def recv_line(sock):
    """Czyta jedną linię (do \\n) ze streamu Bluetooth."""
    buf = b""
    while True:
        ch = sock.recv(1)
        if not ch or ch == b"\n":
            break
        buf += ch
    return buf.decode("utf-8", errors="ignore").strip()


def main():
    # Inicjalizacja czujnika MPU9250
    mpu = MPU9250(
        address_ak=AK8963_ADDRESS,
        address_mpu_master=MPU9050_ADDRESS_68,
        bus=1
    )
    mpu.configure()

    przygotuj_bluetooth()

    # Otwarcie serwera RFCOMM
    server_sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
    server_sock.bind(("00:00:00:00:00:00", 1))
    server_sock.listen(1)

    print("Czekam na połączenie z komputera...")
    client_sock, address = server_sock.accept()
    print(f"Połączono z: {address[0]}")
    ukryj_bluetooth()

    send_line(client_sock, "READY")  # potwierdzenie gotowości dla webapp

    try:
        while True:
            # ── Oczekiwanie na komendę START:<ćwiczenie> ──────────────────────
            cmd = recv_line(client_sock)
            if not cmd:
                continue

            print(f"[CMD] {cmd}")

            if cmd.upper() == "DISCONNECT":
                print("Webapp rozłączył się.")
                break

            if not cmd.upper().startswith("START"):
                send_line(client_sock, json.dumps({"error": "Nieznana komenda"}))
                continue

            # Parsowanie nazwy ćwiczenia (np. "START:Bench Press")
            parts    = cmd.split(":", 1)
            exercise = parts[1].strip() if len(parts) > 1 else "Nieznane"
            print(f"▶ START – ćwiczenie: {exercise}")

            # ── Zbieranie danych ──────────────────────────────────────────────
            workout_data = []
            send_line(client_sock, "RECORDING")  # informacja zwrotna dla webapp

            recording = True
            while recording:
                # Sprawdź, czy w buforze jest komenda STOP (nieblokująco)
                client_sock.setblocking(False)
                try:
                    incoming = recv_line(client_sock)
                    if incoming.upper() == "STOP":
                        print("⏹ STOP – kończę zapis.")
                        recording = False
                except BlockingIOError:
                    pass  # brak danych w buforze, kontynuujemy pomiar
                finally:
                    client_sock.setblocking(True)

                if recording:
                    accel = mpu.readAccelerometerMaster()  # [x, y, z] w g
                    workout_data.append({"accelerometer": list(accel)})
                    time.sleep(SAMPLE_INTERVAL)

            # ── Obliczanie statystyk na Pi ────────────────────────────────────
            print(f"Zebrano {len(workout_data)} próbek. Obliczam statystyki...")
            reps, peak = calculate_reps(workout_data)
            print(f"Wynik: reps={reps}, peak={peak}")

            result = json.dumps({
                "exercise": exercise,
                "reps":     reps,
                "peak":     peak,
                "samples":  len(workout_data)
            })
            send_line(client_sock, result)

    except OSError:
        print("Klient się rozłączył.")
    except KeyboardInterrupt:
        print("\nZakończono przez użytkownika.")
    finally:
        client_sock.close()
        server_sock.close()


if __name__ == "__main__":
    main()
