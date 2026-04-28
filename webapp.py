from flask import Flask, jsonify, render_template_string, request
import serial
import threading
import json

app = Flask(__name__)

# ── Konfiguracja Bluetooth ─────────────────────────────────────────────────────
BLUETOOTH_PORT = "COM6"   # ← zmień na swój port (np. COM5)
# ─────────────────────────────────────────────────────────────────────────────

bt_serial        = None
current_exercise = "Bench Press"
workout_log      = []

# Wynik ostatniego treningu (wypełniany po odebraniu STOP-response z Pi)
last_result = {"reps": 0, "peak": 0.0, "exercise": "", "samples": 0}
result_event = threading.Event()   # sygnał że wynik już przyszedł


# ── Komunikacja z Pi ───────────────────────────────────────────────────────────
def send_cmd(text):
    """Wysyła komendę do Raspberry Pi przez Bluetooth."""
    if bt_serial and bt_serial.is_open:
        bt_serial.write((text + "\n").encode("utf-8"))


def listen_for_result():
    """
    Wątek nasłuchujący odpowiedzi Pi.
    Po 'RECORDING' czeka na linię JSON z wynikami.
    """
    global last_result
    try:
        while bt_serial and bt_serial.is_open:
            line = bt_serial.readline().decode("utf-8", errors="ignore").strip()
            if not line:
                continue
            print(f"[Pi → app] {line}")

            if line == "READY":
                print("Pi gotowe do przyjmowania komend.")
            elif line == "RECORDING":
                print("Pi rozpoczęło zapis.")
                result_event.clear()
            else:
                # Spróbuj sparsować JSON z wynikami
                try:
                    data = json.loads(line)
                    if "reps" in data and "peak" in data:
                        last_result = data
                        workout_log.append({
                            "exercise": data.get("exercise", current_exercise),
                            "reps":     data["reps"],
                            "peak":     data["peak"],
                        })
                        result_event.set()   # odblokuj /app/stop
                except json.JSONDecodeError:
                    print(f"Nieznana odpowiedź Pi: {line}")
    except Exception as e:
        print(f"Błąd wątku nasłuchu: {e}")


# ── Endpointy Flask ────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/app/start", methods=["POST"])
def app_start():
    global bt_serial
    import time as _time

    # Zamknij poprzednie połączenie jeśli istnieje (PermissionError na Windows)
    if bt_serial is not None:
        try:
            if bt_serial.is_open:
                bt_serial.close()
                print("Zamknięto poprzedni port COM.")
        except Exception:
            pass
        bt_serial = None
        _time.sleep(1)  # Windows potrzebuje chwili na zwolnienie portu

    last_err = None
    for attempt in range(1, 4):   # 3 próby z rosnącym opóźnieniem
        try:
            print(f"Otwieranie portu {BLUETOOTH_PORT} (próba {attempt}/3)...")
            bt_serial = serial.Serial(
                port=BLUETOOTH_PORT,
                baudrate=9600,
                timeout=5,
                write_timeout=5,
                dsrdtr=False,
                rtscts=False,
            )
            break   # sukces – wychodzimy z pętli
        except serial.SerialException as e:
            last_err = e
            print(f"  Nieudana próba {attempt}: {e}")
            if bt_serial is not None:
                try:
                    bt_serial.close()
                except Exception:
                    pass
                bt_serial = None
            _time.sleep(attempt * 1.5)   # 1.5 s, 3 s, 4.5 s
    else:
        print(f"BŁĄD – nie udało się otworzyć portu: {last_err}")
        return jsonify({
            "error": (
                f"Nie można otworzyć {BLUETOOTH_PORT} – port zajęty lub niedostępny. "
                "Sprawdź: 1) czy nie masz otwartego innego terminala/programu na tym porcie, "
                "2) czy Bluetooth jest sparowany, "
                f"3) właściwy numer portu w ustawieniu BLUETOOTH_PORT (teraz: {BLUETOOTH_PORT})."
            )
        }), 503

    # Uruchamiamy wątek nasłuchu
    t = threading.Thread(target=listen_for_result, daemon=True)
    t.start()

    # Komenda START z nazwą ćwiczenia
    send_cmd(f"START:{current_exercise}")
    print(f"▶ Wysłano START:{current_exercise}")
    return jsonify({"status": "started", "exercise": current_exercise})


@app.route("/app/stop", methods=["POST"])
def app_stop():
    global bt_serial

    send_cmd("STOP")
    print("⏹ Wysłano STOP – czekam na wyniki z Pi...")

    # Czekamy max 30 s na odpowiedź z Pi
    got_result = result_event.wait(timeout=30)

    if bt_serial and bt_serial.is_open:
        bt_serial.close()

    if not got_result:
        return jsonify({"error": "Brak odpowiedzi z Pi (timeout)"}), 504

    return jsonify({
        "total_reps": last_result["reps"],
        "max_peak":   last_result["peak"],
        "exercise":   last_result.get("exercise", current_exercise),
        "samples":    last_result.get("samples", 0),
    })


@app.route("/app/log", methods=["GET"])
def get_log():
    return jsonify(workout_log)


@app.route("/app/set_exercise", methods=["POST"])
def set_exercise():
    global current_exercise
    data = request.get_json()
    current_exercise = data.get("exercise", "Bench Press")
    return jsonify({"status": "ok", "exercise": current_exercise})


# ── HTML ───────────────────────────────────────────────────────────────────────
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="pl">
<head>
    <meta charset="UTF-8">
    <title>SmartGYM Pro</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        :root {
            --background: #000000;
            --card: #1C1C1E;
            --primary: #00C0DB;
            --text: #FFFFFF;
            --text-muted: #8E8E93;
            --input-bg: #B0BEC5;
            --accent-yellow: #F1C40F;
            --error: #FF453A;
        }
        body { font-family: -apple-system, sans-serif; background: var(--background); color: var(--text); display: flex; flex-direction: column; align-items: center; padding: 20px; }
        .panel { background: var(--card); border-radius: 14px; padding: 24px; width: 100%; max-width: 400px; text-align: center; margin-bottom: 20px; border: 1px solid #333; }
        .hidden { display: none; }
        input { width: 100%; padding: 12px; margin: 8px 0; border-radius: 8px; border: none; background: var(--input-bg); color: #000; font-weight: bold; box-sizing: border-box; }
        button { width: 100%; padding: 14px; margin: 6px 0; border-radius: 10px; border: none; font-size: 16px; font-weight: bold; cursor: pointer; transition: opacity .2s; }
        button:disabled { opacity: .4; cursor: not-allowed; }
        .btn-p { background: var(--primary); color: #000; }
        .btn-o { background: transparent; color: var(--primary); border: 2px solid var(--primary) !important; }
        select { width: 100%; padding: 12px; margin: 8px 0; border-radius: 8px; border: none; background: var(--input-bg); color: #000; font-weight: bold; }
        .stat-box { flex: 1; background: #2C2C2E; border-radius: 10px; padding: 16px; font-size: 13px; color: var(--text-muted); }
        .neon-val { display: block; font-size: 42px; font-weight: 900; color: var(--primary); }
        .gold-val { display: block; font-size: 42px; font-weight: 900; color: var(--accent-yellow); }
        table { width: 100%; border-collapse: collapse; font-size: 13px; }
        td { padding: 8px 4px; border-bottom: 1px solid #2C2C2E; }
        #statusMsg { margin: 8px 0; font-size: 13px; color: var(--text-muted); min-height: 18px; }
    </style>
</head>
<body>

    <div id="authPanel" class="panel">
        <h1 style="color: var(--primary)">SmartGYM Pro</h1>

        <div id="loginForm">
            <h2>Logowanie</h2>
            <input type="text" id="username" placeholder="Login">
            <input type="password" id="password" placeholder="Hasło">
            <button class="btn-p" onclick="handleAuth()">Zaloguj</button>
            <button class="btn-o" onclick="toggleAuth(false)">Nie masz konta? Zarejestruj się</button>
        </div>

        <div id="registerForm" class="hidden">
            <h2>Rejestracja</h2>
            <input type="text" id="regUsername" placeholder="Nowy Login">
            <input type="password" id="regPassword" placeholder="Hasło">
            <input type="password" id="regPasswordConfirm" placeholder="Powtórz Hasło">
            <button class="btn-p" onclick="handleRegister()">Załóż konto</button>
            <button class="btn-o" onclick="toggleAuth(true)">Powrót do logowania</button>
        </div>
    </div>

    <div id="appPanel" class="panel hidden" style="max-width: 600px;">
        <div style="display:flex; justify-content: space-between; align-items: center; margin-bottom: 20px;">
            <p style="font-size: 12px; margin:0;">Zalogowano: <b id="userLabel" style="color:var(--primary)"></b></p>
            <button class="btn-o" style="width: auto; padding: 5px 10px; margin:0; font-size: 10px;" onclick="logout()">Wyloguj</button>
        </div>

        <select id="exerciseSelect" onchange="updateExercise()">
            <option>Bench Press</option>
            <option>Squat</option>
            <option>Deadlift</option>
            <option>Overhead Press</option>
            <option>Lat Pulldown</option>
        </select>

        <div style="display: flex; gap: 10px;">
            <button class="btn-p" id="startBtn" onclick="startWorkout()">START</button>
            <button class="btn-o" id="stopBtn" onclick="stopWorkout()" disabled style="color:var(--error); border-color:var(--error);">STOP</button>
        </div>

        <p id="statusMsg"></p>

        <div id="results" class="hidden">
            <div style="display: flex; gap: 10px; margin-top: 20px;">
                <div class="stat-box">REPS<br><span id="repsVal" class="neon-val">0</span></div>
                <div class="stat-box">PEAK<br><span id="peakVal" class="gold-val">0</span></div>
            </div>
            <p style="font-size: 12px; color: var(--text-muted); margin-top: 8px;">
                Próbki: <span id="samplesVal">–</span> &nbsp;|&nbsp; Ćwiczenie: <span id="exerciseVal">–</span>
            </p>
            <button class="btn-p" style="background: var(--accent-yellow); color:black; margin-top: 12px;" onclick="syncWorkout()">☁ Synchronizacja z serwerem</button>
        </div>

        <div style="margin-top: 30px; text-align: left;">
            <h3 style="color: var(--primary)">Historia treningów</h3>
            <div id="historyTable"></div>
        </div>
    </div>

    <script>
        const AUTH_SRV = "http://localhost:10000";

        function toggleAuth(showLogin) {
            document.getElementById('loginForm').className   = showLogin ? '' : 'hidden';
            document.getElementById('registerForm').className = showLogin ? 'hidden' : '';
        }

        async function handleAuth() {
            const u = document.getElementById('username').value;
            const p = document.getElementById('password').value;
            const res = await fetch(`${AUTH_SRV}/login`, {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({username: u, password: p})
            });
            const data = await res.json();
            if (res.ok) {
                localStorage.setItem('token', data.token);
                localStorage.setItem('user', data.username);
                showApp();
            } else alert(data.error);
        }

        async function handleRegister() {
            const u  = document.getElementById('regUsername').value;
            const p  = document.getElementById('regPassword').value;
            const p2 = document.getElementById('regPasswordConfirm').value;
            if (p !== p2) { alert("Hasła nie są identyczne!"); return; }
            const res = await fetch(`${AUTH_SRV}/register`, {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({username: u, password: p})
            });
            const data = await res.json();
            if (res.ok) { alert("Konto utworzone pomyślnie"); toggleAuth(true); }
            else alert(data.error);
        }

        function showApp() {
            document.getElementById('authPanel').classList.add('hidden');
            document.getElementById('appPanel').classList.remove('hidden');
            document.getElementById('userLabel').innerText = localStorage.getItem('user');
            loadHistory();
        }

        function logout() { localStorage.clear(); location.reload(); }

        async function updateExercise() {
            const ex = document.getElementById('exerciseSelect').value;
            await fetch('/app/set_exercise', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({exercise: ex})
            });
        }

        async function startWorkout() {
            setStatus("Łączę z Pi i rozpoczynam trening...");
            const res = await fetch('/app/start', {method: 'POST'});
            const data = await res.json();
            if (!res.ok) { alert(data.error); setStatus(""); return; }
            document.getElementById('startBtn').disabled = true;
            document.getElementById('stopBtn').disabled  = false;
            document.getElementById('results').classList.add('hidden');
            setStatus("⏺ Trening w toku – Pi zbiera i liczy dane...");
        }

        async function stopWorkout() {
            setStatus("Zatrzymuję – czekam na wyniki z Pi...");
            document.getElementById('stopBtn').disabled = true;
            const res  = await fetch('/app/stop', {method: 'POST'});
            const data = await res.json();
            document.getElementById('startBtn').disabled = false;
            if (!res.ok) { alert(data.error || "Timeout – brak odpowiedzi z Pi"); setStatus(""); return; }

            document.getElementById('repsVal').innerText    = data.total_reps;
            document.getElementById('peakVal').innerText    = data.max_peak;
            document.getElementById('samplesVal').innerText = data.samples;
            document.getElementById('exerciseVal').innerText = data.exercise;
            document.getElementById('results').classList.remove('hidden');
            setStatus("");
        }

        async function syncWorkout() {
            const payload = {
                exercise: document.getElementById('exerciseVal').innerText,
                reps:     document.getElementById('repsVal').innerText,
                peak:     document.getElementById('peakVal').innerText,
            };
            const res = await fetch(`${AUTH_SRV}/sync`, {
                method: 'POST',
                headers: {'Authorization': localStorage.getItem('token'), 'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            });
            if (res.ok) { alert("Zsynchronizowano!"); loadHistory(); }
        }

        async function loadHistory() {
            const res  = await fetch(`${AUTH_SRV}/history`, {headers: {'Authorization': localStorage.getItem('token')}});
            const data = await res.json();
            let html = '<table><tr><td>DATA</td><td>ĆWICZENIE</td><td>REPS</td><td>PEAK</td></tr>';
            data.forEach(h => {
                html += `<tr>
                    <td>${h.date.slice(5, 16)}</td>
                    <td>${h.exercise}</td>
                    <td style="color:var(--primary)">${h.reps}</td>
                    <td style="color:var(--accent-yellow)">${h.peak}</td>
                </tr>`;
            });
            document.getElementById('historyTable').innerHTML = html + '</table>';
        }

        function setStatus(msg) {
            document.getElementById('statusMsg').innerText = msg;
        }

        window.onload = () => { if (localStorage.getItem('token')) showApp(); };
    </script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(port=8080, debug=False)
