from flask import Flask, jsonify, render_template_string, request
import serial
import threading
import re
import statistics


SMOOTH_WINDOW = 7        # wygładzanie (tylko nieparzyste: 1, 3, 5, 7)
COOLDOWN_SAMPLES = 10    # minimalna odległość między szczytami — ZMNIEJSZ jeśli wykrywa za mało
THRESHOLD_SENSITIVITY = 0.25  # czułość progu: mniejsza = więcej szczytów (0.1–1.0)
app = Flask(__name__)

# Konfiguracja Bluetooth (Zmień na swój port z Windowsa, np. COM5)
BLUETOOTH_PORT = "COM6" 
bt_serial = None
is_recording = False
current_workout_data = []



workout_log = []
current_exercise = "Bench Press"
THRESHOLD = 1.5 # UWAGA: Zmieniłem z 80 na 1.5. Akcelerometr domyślnie pokazuje ok 1.0g na osi Z w spoczynku.
def moving_average(values, window):
    """Wygładzanie sygnału — zmniejsza szum."""
    result = []
    half = window // 2
    for i in range(len(values)):
        start = max(0, i - half)
        end = min(len(values) - 1, i + half)
        chunk = values[start:end + 1]
        result.append(sum(chunk) / len(chunk))
    return result

def calculate_reps(data):
    if len(data) < 5:
        return 0, 0.0

    axes_signals = {
        "x": [d["accelerometer"][0] for d in data],
        "y": [d["accelerometer"][1] for d in data],
        "z": [d["accelerometer"][2] for d in data],
    }
    variances = {ax: statistics.variance(vals) for ax, vals in axes_signals.items()}
    dominant_axis = max(variances, key=variances.get)
    raw_signal = axes_signals[dominant_axis]
    smoothed = moving_average(raw_signal, SMOOTH_WINDOW)

    mean_val = statistics.mean(smoothed)
    stdev_val = statistics.stdev(smoothed)
    threshold = mean_val + THRESHOLD_SENSITIVITY * stdev_val

    reps = 0
    max_val = max(smoothed)
    cooldown_counter = 0

    for i in range(1, len(smoothed) - 1):
        if cooldown_counter > 0:
            cooldown_counter -= 1
            continue
        if (smoothed[i] > threshold
                and smoothed[i] > smoothed[i-1]
                and smoothed[i] > smoothed[i+1]):
            reps += 1
            cooldown_counter = COOLDOWN_SAMPLES

    return reps, round(max_val, 2)



# --- FUNKCJA ZBIERAJĄCA DANE W TLE ---
def collect_bluetooth_data():
    global is_recording, current_workout_data, bt_serial
    while is_recording:
        try:
            # POMIJAMY sprawdzanie "in_waiting" (Windows często przy BT kłamie)
            # Czytamy bezpośrednio - najwyżej poczeka do końca timeoutu
            line = bt_serial.readline().decode('utf-8', errors='ignore').strip()
            
            if not line:
                continue # Pusta linia, czekamy dalej
                
            # === DRUKUJEMY SUROWE DANE (To pokaże nam prawdę!) ===
            print(f"📡 Otrzymano: {line}")
            
            # Próbujemy wyciągnąć dane w starym formacie z nawiasami [...]
            match = re.search(r'Akcelerometr:\s*\[([^\]]+)\]', line)
            if match:
                coords = match.group(1).split(',')
                z_val = float(coords[2].strip())
                current_workout_data.append({
                    "accelerometer": [float(coords[0]), float(coords[1]), z_val]
                })
            # Jeśli malinka wysyła coś innego (np. słownik albo nawiasy okrągłe)
            else:
                pass # Na razie ignorujemy błąd dopasowania, ważne żebyśmy zobaczyli "Otrzymano:"
                
        except Exception as e:
            if is_recording:
                print(f"Błąd odczytu linii: {e}")

# --- ENDPOINTY FLASK ---

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE, threshold=THRESHOLD)

@app.route('/app/start', methods=['POST'])
def app_start():
    global is_recording, current_workout_data, bt_serial
    
    current_workout_data = [] # Czyścimy dane z poprzedniego treningu
    is_recording = True
    
    try:
        print(f"⏳ Otwieranie portu {BLUETOOTH_PORT}... (to może potrwać kilka sekund)")
        
        # Nowa, kuloodporna konfiguracja dla Bluetooth w Windowsie
        bt_serial = serial.Serial(
            port=BLUETOOTH_PORT, 
            baudrate=9600, 
            timeout=5,          # Dajemy Windowsowi aż 5 sekund na negocjację BT!
            write_timeout=5,
            dsrdtr=False,       # Ignorujemy sprawdzanie fizycznego kabla
            rtscts=False        # Ignorujemy sprzętowe sterowanie przepływem
        )
        
        # Jeśli doszliśmy tutaj, port się otworzył!
        thread = threading.Thread(target=collect_bluetooth_data)
        thread.start()
        print("▶ ROZPOCZĘTO TRENING I NASŁUCH BLUETOOTH!")
        return jsonify({"status": "started"})
        
    except Exception as e:
        is_recording = False
        # TEN PRINT JEST KLUCZOWY - pokaże nam dokładnie, co Windows blokuje
        print(f"\n❌❌❌ KRYTYCZNY BŁĄD PYSERIAL: {e} ❌❌❌\n")
        return jsonify({"error": f"Błąd portu COM: {str(e)}"}), 503

@app.route('/app/stop', methods=['POST'])
def app_stop():
    global is_recording, bt_serial
    
    # Zatrzymujemy nasłuch i zamykamy port Bluetooth
    is_recording = False
    if bt_serial and bt_serial.is_open:
        bt_serial.close()
        
    print(f"⏹ Zakończono trening. Zebrano {len(current_workout_data)} punktów danych.")

    # Analizujemy zebrane dane
    total_reps, peak = calculate_reps(current_workout_data)
    workout_log.append({"exercise": current_exercise, "reps": total_reps, "peak": peak})
    
    return jsonify({
        "total_reps": total_reps, 
        "max_peak": peak, 
        "raw_data": current_workout_data, 
        "threshold": THRESHOLD,
        "exercise": current_exercise
    })

@app.route('/app/log', methods=['GET'])
def get_log():
    return jsonify(workout_log)

@app.route('/app/set_exercise', methods=['POST'])
def set_exercise():
    global current_exercise
    data = request.get_json()
    current_exercise = data.get("exercise", "Bench Press")
    return jsonify({"status": "ok", "exercise": current_exercise})

# ==========================================
# TUTAJ WKLEJ SWOJĄ ZMIENNĄ HTML_TEMPLATE (cały kod strony HTML)
# HTML_TEMPLATE = """ ... """
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
        button { width: 100%; padding: 14px; margin-top: 10px; border-radius: 10px; border: none; font-weight: bold; cursor: pointer; text-transform: uppercase; }
        .btn-p { background: var(--primary); color: #000; }
        .btn-o { background: transparent; border: 1px solid var(--primary); color: var(--primary); }
        .stat-box { background: #2C2C2E; padding: 15px; border-radius: 10px; flex: 1; border: 1px solid #333; }
        .neon-val { color: var(--primary); font-size: 32px; font-weight: bold; }
        .gold-val { color: var(--accent-yellow); font-size: 32px; font-weight: bold; }
        select { width: 100%; padding: 12px; border-radius: 8px; background: var(--card); color: white; border: 1px solid #333; margin-bottom: 15px; }
        table { width: 100%; font-size: 12px; border-collapse: collapse; margin-top: 10px; }
        tr { border-bottom: 1px solid #333; } td { padding: 8px; }
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
            <option>Bench Press</option><option>Squat</option><option>Deadlift</option>
            <option>Overhead Press</option><option>Lat Pulldown</option>
        </select>

        <div style="display: flex; gap: 10px;">
            <button class="btn-p" id="startBtn" onclick="startWorkout()">START</button>
            <button class="btn-o" id="stopBtn" onclick="stopWorkout()" disabled style="color:var(--error); border-color:var(--error);">STOP</button>
        </div>

        <div id="results" class="hidden">
            <div style="display: flex; gap: 10px; margin-top: 20px;">
                <div class="stat-box">REPS<br><span id="repsVal" class="neon-val">0</span></div>
                <div class="stat-box">PEAK<br><span id="peakVal" class="gold-val">0</span></div>
            </div>
            <canvas id="workoutChart" style="margin: 20px 0;"></canvas>
            <button class="btn-p" style="background: var(--accent-yellow); color:black;" onclick="syncWorkout()">☁ synchronizacja z serwerem</button>
        </div>

        <div style="margin-top: 30px; text-align: left;">
            <h3 style="color: var(--primary)">historia treningów</h3>
            <div id="historyTable"></div>
        </div>
    </div>

    <script>
        const AUTH_SRV = "http://localhost:10000";
        let myChart = null;

        // Przełączanie widoków logowanie/rejestracja
        function toggleAuth(showLogin) {
            document.getElementById('loginForm').className = showLogin ? '' : 'hidden';
            document.getElementById('registerForm').className = showLogin ? 'hidden' : '';
        }

        // Obsługa logowania
        async function handleAuth() {
            const u = document.getElementById('username').value;
            const p = document.getElementById('password').value;
            const res = await fetch(`${AUTH_SRV}/login`, {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({username: u, password: p})
            });
            const data = await res.json();
            if(res.ok) {
                localStorage.setItem('token', data.token);
                localStorage.setItem('user', data.username);
                showApp();
            } else alert(data.error);
        }

        // --- NOWY ENDPOINT REJESTRACJI Z WALIDACJĄ HASŁA ---
        async function handleRegister() {
            const u = document.getElementById('regUsername').value;
            const p = document.getElementById('regPassword').value;
            const p2 = document.getElementById('regPasswordConfirm').value;

            if (p !== p2) {
                alert("Błąd: Hasła nie są identyczne!");
                return;
            }

            const res = await fetch(`${AUTH_SRV}/register`, {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({username: u, password: p})
            });
            const data = await res.json();
            if(res.ok) {
                alert("Konto utworzone pomyślnie");
                toggleAuth(true);
            } else alert(data.error);
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
            await fetch('/app/start', {method: 'POST'});
            document.getElementById('startBtn').disabled = true;
            document.getElementById('stopBtn').disabled = false;
        }

        async function stopWorkout() {
            const res = await fetch('/app/stop', {method: 'POST'});
            const data = await res.json();
            document.getElementById('repsVal').innerText = data.total_reps;
            document.getElementById('peakVal').innerText = data.max_peak;
            document.getElementById('results').classList.remove('hidden');
            document.getElementById('startBtn').disabled = false;
            document.getElementById('stopBtn').disabled = true;
            drawChart(data.raw_data, data.threshold);
        }

        async function syncWorkout() {
            const payload = {
                exercise: document.getElementById('exerciseSelect').value,
                reps: document.getElementById('repsVal').innerText,
                peak: document.getElementById('peakVal').innerText
            };
            const res = await fetch(`${AUTH_SRV}/sync`, {
                method: 'POST',
                headers: {
                    'Authorization': localStorage.getItem('token'),
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(payload)
            });
            if(res.ok) { alert("Zsynchronizowano!"); loadHistory(); }
        }

        async function loadHistory() {
            const res = await fetch(`${AUTH_SRV}/history`, {
                headers: {'Authorization': localStorage.getItem('token')}
            });
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

        function drawChart(rawData, threshold) {
            const ctx = document.getElementById('workoutChart').getContext('2d');
            const z = rawData.map(p => p.accelerometer[2]);
            if(myChart) myChart.destroy();
            myChart = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: z.map((_, i) => i),
                    datasets: [{
                        label: 'Ruch Z', data: z, borderColor: '#00C0DB', tension: 0.4, pointRadius: 0
                    }, {
                        label: 'Próg', data: new Array(z.length).fill(threshold), borderColor: 'red', borderDash: [5, 5], pointRadius: 0
                    }]
                },
                options: { scales: { x: { display: false }, y: { grid: { color: '#333' } } }, plugins: { legend: { display: false } } }
            });
        }

        window.onload = () => { if(localStorage.getItem('token')) showApp(); };
    </script>
</body>
</html>
"""
# ==========================================

if __name__ == "__main__":
    app.run(port=8080, debug=False)
