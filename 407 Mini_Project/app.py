from flask import Flask, jsonify, render_template
from flask_sqlalchemy import SQLAlchemy
import tinytuya
import threading
import time
from datetime import datetime
from sqlalchemy.sql import func
from sqlalchemy import text

from flask import send_file
import pandas as pd
from sqlalchemy import text
import io

app = Flask(__name__)

# Tuya Device Info
DEVICE_ID = "bf035aef5b8c5240dbykne"
LOCAL_KEY = "}=rHhdU-JWFeL3CB"
DEVICE_IP = "192.168.10.171"
PROTOCOL_VERSION = "3.5"

device = tinytuya.OutletDevice(DEVICE_ID, DEVICE_IP, LOCAL_KEY)
device.set_version(float(PROTOCOL_VERSION))

# SQLite Config
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///energy_data.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Database Model
class EnergyData(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.String(20))
    watt = db.Column(db.Float)
    voltage = db.Column(db.Float)
    current = db.Column(db.Float)
    kwh = db.Column(db.Float)

with app.app_context():
    db.create_all()

# Polling Function (safe with app context)
def poll_device(interval=10):
    while True:
        try:
            data = device.status()
            dp = data.get("dps", {})

            raw_watt = dp.get("19", 0)
            voltage = dp.get("20", 0)
            current = dp.get("18", 0)

            # Corrected watt value
            watt = raw_watt / 10
            
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            kwh = watt * (interval / 3600) / 1000  # convert to kWh

            with app.app_context():
                entry = EnergyData(
                    timestamp=timestamp,
                    watt=watt,
                    voltage=voltage,
                    current=current,
                    kwh=kwh
                )
                db.session.add(entry)
                db.session.commit()

            print(f"[{timestamp}] W: {watt}W, V: {voltage}V, A: {current}mA, kWh: {kwh:.6f}")
        except Exception as e:
            print("Error fetching data:", e)
        time.sleep(interval)

# Flag to avoid multiple thread start
polling_started = False

# Routes
@app.route('/')
def dashboard():
    global polling_started
    if not polling_started:
        threading.Thread(target=poll_device, daemon=True).start()
        polling_started = True
    return render_template('dashboard.html')

@app.route('/api/data')
def get_data():
    entries = EnergyData.query.order_by(EnergyData.id.desc()).limit(60).all()
    entries.reverse()
    return jsonify([
        {
            "timestamp": e.timestamp,
            "watt": e.watt,
            "voltage": e.voltage,
            "current": e.current
        } for e in entries
    ])

@app.route('/api/total-kwh')
def total_kwh():
    total = db.session.query(db.func.sum(EnergyData.kwh)).scalar() or 0
    return jsonify({"total_kwh": round(total, 4)})

@app.route('/api/stats')
def energy_stats():
    daily = db.session.execute(
        """
        SELECT SUBSTR(timestamp, 1, 10) AS day, SUM(kwh)
        FROM energy_data
        GROUP BY day
        ORDER BY day DESC
        LIMIT 7
        """
    ).fetchall()

    hourly = db.session.execute(
        """
        SELECT SUBSTR(timestamp, 1, 13) AS hour, SUM(kwh)
        FROM energy_data
        GROUP BY hour
        ORDER BY hour DESC
        LIMIT 24
        """
    ).fetchall()

    return jsonify({
        "daily": [{"day": d[0], "kwh": round(d[1], 4)} for d in daily],
        "hourly": [{"hour": h[0], "kwh": round(h[1], 4)} for h in hourly]
    })

@app.route('/api/stats/minutely')
def minutely_stats():
    # Group by minute (first 16 chars of timestamp: 'YYYY-MM-DD HH:MM')
    results = db.session.execute(text("""
    SELECT SUBSTR(timestamp, 1, 16) AS minute, SUM(kwh) AS total_kwh
    FROM energy_data
    GROUP BY minute
    ORDER BY minute DESC
    LIMIT 60
    """)).fetchall()

    # Reverse so oldest to newest
    results = list(reversed(results))

    return jsonify([
        {"minute": r[0], "total_kwh": round(r[1], 6)} for r in results
    ])

@app.route('/export/full-energy-report')
def export_full_energy_report():
    # Fetch full raw energy data
    raw_data = db.session.execute(text("""
        SELECT timestamp, watt, current, voltage, kwh
        FROM energy_data
        ORDER BY timestamp ASC
    """)).fetchall()

    # Scale voltage for the export
    scaled_data = []
    for row in raw_data:
        timestamp, watt, current, voltage, kwh = row
        voltage = voltage / 10  # scale down voltage
        scaled_data.append((timestamp, watt, current, voltage, kwh))

    df_raw = pd.DataFrame(scaled_data, columns=['Timestamp', 'Watt', 'Current', 'Voltage', 'kWh'])
    df_raw['kWh'] = df_raw['kWh'].round(6)

    # Group by each minute for minutely total kWh
    df_raw['Minute'] = df_raw['Timestamp'].astype(str).str.slice(0, 16)
    df_minutely = df_raw.groupby('Minute', as_index=False)['kWh'].sum()
    df_minutely.rename(columns={'kWh': 'Total_kWh'}, inplace=True)
    df_minutely['Total_kWh'] = df_minutely['Total_kWh'].round(6)

    # Write both sheets into one Excel file
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df_raw.drop(columns='Minute').to_excel(writer, index=False, sheet_name='Raw Data')
        df_minutely.to_excel(writer, index=False, sheet_name='Minutely Report')

    output.seek(0)

    return send_file(output,
                     as_attachment=True,
                     download_name='full_energy_report.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

#http://localhost:5000/export/full-energy-report
@app.route('/api/graph-data')
def api_graph_data():
    results = db.session.execute(text("""
        SELECT timestamp, current, watt, voltage
        FROM energy_data
        ORDER BY timestamp DESC
        LIMIT 100
    """)).fetchall()

    data = [{
        'timestamp': row.timestamp.strftime('%H:%M:%S'),
        'current': round(row.current, 2),
        'watt': round(row.watt, 2),
        'voltage': round(row.voltage, 2)
    } for row in results][::-1]  # Reverse to get chronological order

    return jsonify(data)

if __name__ == '__main__':
    app.run(debug=True)
