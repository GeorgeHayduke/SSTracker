import streamlit as st
import sqlite3
import pandas as pd
import datetime as dt
import matplotlib.pyplot as plt
from streamlit.components.v1 import html as html_component

DB_PATH = "progress.db"

LIFTS = ["Squat", "Bench Press", "Overhead Press", "Deadlift"]
ACCESSORIES = {
    "A": [("Barbell Curl", "3x10"), ("Lat Pulldown", "3x10")],
    "B": [("Triceps Pushdown", "3x12"), ("Barbell Curl", "3x10")],
}

# ---------------- Core logic ----------------

def round_to_5(x):
    return int(round(x / 5.0) * 5)

def default_warmups(work_weight, lift_name):
    """
    Warmups (~50% x5, ~70% x3, ~90% x(1 for DL, 2 otherwise)).
    Deadlift: no 45-lb bar warmup. Others: optional bar sets if <=135.
    """
    ww = max(work_weight, 45)
    is_deadlift = (lift_name == "Deadlift")
    warmups = []
    if not is_deadlift and ww <= 135:
        warmups.append((45, 5))
        warmups.append((45, 5))
    scheme = [(0.50, 5), (0.70, 3), (0.90, 1 if is_deadlift else 2)]
    for p, reps in scheme:
        w = max(45, round_to_5(ww * p))
        if w < ww and (not warmups or warmups[-1][0] != w):
            warmups.append((w, reps))
    return warmups

def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    with get_conn() as con:
        cur = con.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS programs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            weeks INTEGER,
            start_date TEXT,
            workout_days_per_week INTEGER,
            increment_lbs INTEGER DEFAULT 5,
            UNIQUE(user_id))""")
        cur.execute("""CREATE TABLE IF NOT EXISTS lifts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            name TEXT,
            start_weight INTEGER,
            current_weight INTEGER,
            increment_lbs INTEGER DEFAULT 5,
            UNIQUE(user_id, name))""")
        cur.execute("""CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            date TEXT,
            workout_type TEXT,
            completed INTEGER DEFAULT 0)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS sets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,
            lift_name TEXT,
            set_index INTEGER,
            weight INTEGER,
            reps INTEGER,
            is_warmup INTEGER DEFAULT 0)""")
        con.commit()

def ensure_user():
    with get_conn() as con:
        cur = con.cursor()
        cur.execute("SELECT id FROM users LIMIT 1")
        row = cur.fetchone()
        if row is None:
            cur.execute("INSERT INTO users DEFAULT VALUES")
            con.commit()
            return ensure_user()
        return row[0]

def save_program(user_id, start_date, wkpw, increment_lbs):
    with get_conn() as con:
        cur = con.cursor()
        cur.execute("""INSERT OR REPLACE INTO programs
            (id, user_id, weeks, start_date, workout_days_per_week, increment_lbs)
            VALUES (COALESCE((SELECT id FROM programs WHERE user_id=?), NULL),
                    ?, ?, ?, ?, ?)""",
            (user_id, user_id, 6, start_date, wkpw, increment_lbs))
        con.commit()

def save_lift(user_id, name, start_weight, increment_lbs):
    with get_conn() as con:
        cur = con.cursor()
        cur.execute("""INSERT OR REPLACE INTO lifts
            (id, user_id, name, start_weight, current_weight, increment_lbs)
            VALUES (COALESCE((SELECT id FROM lifts WHERE user_id=? AND name=?), NULL),
                    ?, ?, ?, ?, ?)""",
            (user_id, name, user_id, name, start_weight, start_weight, increment_lbs))
        con.commit()

def get_program(user_id):
    with get_conn() as con:
        cur = con.cursor()
        cur.execute("""SELECT weeks, start_date, workout_days_per_week, increment_lbs
                       FROM programs WHERE user_id=?""", (user_id,))
        row = cur.fetchone()
        if row:
            return {"weeks": row[0], "start_date": row[1], "wkpw": row[2], "inc": row[3]}
        return None

def get_lifts(user_id):
    with get_conn() as con:
        cur = con.cursor()
        cur.execute("""SELECT name, start_weight, current_weight, increment_lbs
                       FROM lifts WHERE user_id=?""", (user_id,))
        rows = cur.fetchall()
        return {r[0]: {"start": r[1], "current": r[2], "inc": r[3]} for r in rows}

def update_lift_weight(user_id, name, new_weight):
    with get_conn() as con:
        cur = con.cursor()
        cur.execute("UPDATE lifts SET current_weight=? WHERE user_id=? AND name=?",
                    (new_weight, user_id, name))
        con.commit()

def schedule_plan(user_id, weeks, start_date, wkpw):
    with get_conn() as con:
        cur = con.cursor()
        cur.execute("DELETE FROM sets WHERE session_id IN (SELECT id FROM sessions WHERE user_id=?)", (user_id,))
        cur.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        con.commit()
    start = dt.datetime.fromisoformat(start_date)
    total_sessions = weeks * wkpw
    types = [("A" if i % 2 == 0 else "B") for i in range(total_sessions)]
    dates = []
    d = start
    for _ in range(total_sessions):
        dates.append(d.date().isoformat())
        d += dt.timedelta(days=2 if wkpw==3 else 1)
    with get_conn() as con:
        cur = con.cursor()
        for date_str, t in zip(dates, types):
            cur.execute("INSERT INTO sessions (user_id, date, workout_type, completed) VALUES (?, ?, ?, 0)",
                        (user_id, date_str, t))
        con.commit()

def session_summary(user_id):
    with get_conn() as con:
        cur = con.cursor()
        cur.execute("""SELECT id, date, workout_type, completed
                       FROM sessions WHERE user_id=? ORDER BY date""", (user_id,))
        rows = cur.fetchall()
    return pd.DataFrame(rows, columns=["session_id", "date", "type", "done"])

def generate_sets_for_session(session_id, workout_type, lifts_dict):
    plan = []
    def add_ex(name, ww, sets, reps):
        for w, r in default_warmups(ww, name):
            plan.append((name, len(plan), int(w), int(r), 1))
        for _ in range(sets):
            plan.append((name, len(plan), int(ww), int(reps), 0))
    if workout_type == "A":
        add_ex("Squat",          lifts_dict["Squat"]["current"],         3, 5)
        add_ex("Bench Press",    lifts_dict["Bench Press"]["current"],   3, 5)
        add_ex("Deadlift",       lifts_dict["Deadlift"]["current"],      1, 5)
    else:
        add_ex("Squat",          lifts_dict["Squat"]["current"],         3, 5)
        add_ex("Overhead Press", lifts_dict["Overhead Press"]["current"],3, 5)
        add_ex("Deadlift",       lifts_dict["Deadlift"]["current"],      1, 5)
    with get_conn() as con:
        cur = con.cursor()
        cur.execute("DELETE FROM sets WHERE session_id=?", (session_id,))
        for (lift_name, idx, w, r, iswu) in plan:
            cur.execute("""INSERT INTO sets (session_id, lift_name, set_index, weight, reps, is_warmup)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (session_id, lift_name, idx, int(w), int(r), int(iswu)))
        con.commit()

def mark_complete_and_progress(user_id, session_id):
    with get_conn() as con:
        cur = con.cursor()
        cur.execute("UPDATE sessions SET completed=1 WHERE id=?", (session_id,))
        cur.execute("""SELECT DISTINCT lift_name FROM sets
                       WHERE session_id=? AND is_warmup=0""", (session_id,))
        trained = [row[0] for row in cur.fetchall()]
        con.commit()
    lifts = get_lifts(user_id)
    for name in trained:
        update_lift_weight(user_id, name, round_to_5(lifts[name]["current"] + lifts[name]["inc"]))

def load_sets(session_id):
    with get_conn() as con:
        cur = con.cursor()
        cur.execute("""SELECT lift_name, set_index, weight, reps, is_warmup
                       FROM sets WHERE session_id=? ORDER BY set_index""", (session_id,))
        rows = cur.fetchall()
    return pd.DataFrame(rows, columns=["Lift", "Set#", "Weight", "Reps", "Warmup"])

def history_df(user_id):
    with get_conn() as con:
        cur = con.cursor()
        cur.execute("""SELECT s.date, se.lift_name,
                              MAX(CASE WHEN se.is_warmup=0 THEN se.weight ELSE NULL END) as top_set
                       FROM sessions s
                       JOIN sets se ON se.session_id=s.id
                       WHERE s.user_id=? AND s.completed=1
                       GROUP BY s.date, se.lift_name
                       ORDER BY s.date""", (user_id,))
        rows = cur.fetchall()
    return pd.DataFrame(rows, columns=["Date", "Lift", "TopSet"])

def simulate_expected_progress(start_weights, inc, weeks, wkpw):
    curr = {k: start_weights[k] for k in start_weights}
    total_sessions = weeks * wkpw
    schedule = [("A" if i % 2 == 0 else "B") for i in range(total_sessions)]
    week_rows = []
    for s_idx, t in enumerate(schedule, start=1):
        if t == "A":
            for lift in ["Squat", "Bench Press", "Deadlift"]:
                curr[lift] = round_to_5(curr[lift] + inc)
        else:
            for lift in ["Squat", "Overhead Press", "Deadlift"]:
                curr[lift] = round_to_5(curr[lift] + inc)
        if s_idx % wkpw == 0:
            week_rows.append({
                "Week": s_idx // wkpw,
                "Squat": curr["Squat"],
                "Bench": curr["Bench Press"],
                "Press": curr["Overhead Press"],
                "Deadlift": curr["Deadlift"],
            })
    return pd.DataFrame(week_rows)

# ---------------- HTML builders (mockup integration) ----------------

def _sets_to_html_blocks(sets_df, lift_name, color_class):
    if sets_df is None or sets_df.empty:
        return f"""
        <div class="mb-8">
          <div class="flex items-center mb-3">
            <i data-feather="activity" class="w-5 h-5 {color_class} mr-2"></i>
            <h4 class="font-semibold text-gray-800">{lift_name} - (generate sets above)</h4>
          </div>
          <div class="p-3 rounded bg-gray-50 text-gray-600">Click <b>Generate Sets</b> to populate warmups and work sets.</div>
        </div>"""
    d = sets_df[sets_df["Lift"] == lift_name]
    if d.empty:
        return ""
    ww = int(d[d["Warmup"] == 0]["Weight"].iloc[0]) if not d[d["Warmup"] == 0].empty else int(d["Weight"].iloc[-1])
    wups = d[d["Warmup"] == 1][["Weight", "Reps"]].values.tolist()
    works = d[d["Warmup"] == 0][["Weight", "Reps"]].values.tolist()
    warmups_html = "\n".join([
        f"""<div class="warmup-set bg-gray-50 p-3 rounded flex justify-between items-center">
              <span class="text-gray-600">Warmup: {w} lbs × {r}</span>
              <span class="{color_class}"><i data-feather="check-circle" class="w-5 h-5"></i></span>
            </div>""" for w, r in wups
    ])
    works_html = "\n".join([
        f"""<div class="work-set bg-primary-50 p-3 rounded flex justify-between items-center">
              <span class="font-medium">Work Set: {w} lbs × {r}</span>
              <span class="{color_class}"><i data-feather="check-circle" class="w-5 h-5"></i></span>
            </div>""" for w, r in works
    ])
    return f"""
    <div class="mb-8">
      <div class="flex items-center mb-3">
        <i data-feather="activity" class="w-5 h-5 {color_class} mr-2"></i>
        <h4 class="font-semibold text-gray-800">{lift_name} - {ww} lbs</h4>
      </div>
      <div class="space-y-2">{warmups_html}{works_html}</div>
    </div>"""

def _table_html_from_df(df):
    head = """<thead><tr>
      <th class="px-4 py-2 text-left">Week</th>
      <th class="px-4 py-2 text-left">Squat</th>
      <th class="px-4 py-2 text-left">Bench</th>
      <th class="px-4 py-2 text-left">Press</th>
      <th class="px-4 py-2 text-left">Deadlift</th>
    </tr></thead>"""
    rows = []
    for _, r in df.iterrows():
        rows.append(f"""<tr class="border-t">
          <td class="px-4 py-2">{int(r['Week'])}</td>
          <td class="px-4 py-2">{int(r['Squat'])}</td>
          <td class="px-4 py-2">{int(r['Bench'])}</td>
          <td class="px-4 py-2">{int(r['Press'])}</td>
          <td class="px-4 py-2">{int(r['Deadlift'])}</td>
        </tr>""")
    body = "<tbody>" + "\n".join(rows) + "</tbody>"
    return f"""<table class="w-full text-sm">{head}{body}</table>"""

def render_setup_html(start_date, wkpw, inc, display_lifts, proj_df, heading="Current Lifts"):
    start_date_str = dt.datetime.fromisoformat(start_date).strftime("%B %d, %Y")
    wk_str = f"{wkpw} days"
    inc_str = f"{inc} lbs/session"
    squat = display_lifts.get("Squat", 0)
    bench = display_lifts.get("Bench Press", 0)
    press = display_lifts.get("Overhead Press", 0)
    dead = display_lifts.get("Deadlift", 0)
    table_html = _table_html_from_df(proj_df) if proj_df is not None and not proj_df.empty else ""
    tpl = r"""
<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Starting Strength MVP - Setup</title>
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://unpkg.com/feather-icons"></script>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<script>
tailwind.config={theme:{extend:{colors:{primary:{50:'#f0f9ff',100:'#e0f2fe',200:'#bae6fd',300:'#7dd3fc',400:'#38bdf8',500:'#0ea5e9',600:'#0284c7',700:'#0369a1',800:'#075985',900:'#0c4a6e'},secondary:{50:'#f5f3ff',100:'#ede9fe',200:'#ddd6fe',300:'#c4b5fd',400:'#a78bfa',500:'#8b5cf6',600:'#7c3aed',700:'#6d28d9',800:'#5b21b6',900:'#4c1d95'},accent:{50:'#fef2f2',100:'#fee2e2',200:'#fecaca',300:'#fca5a5',400:'#f87171',500:'#ef4444',600:'#dc2626',700:'#b91c1c',800:'#991b1b',900:'#7f1d1d'}},fontFamily:{sans:['Inter','sans-serif']}}}}
</script>
<style>
.gradient-bg{background:linear-gradient(135deg,#0ea5e9 0%,#8b5cf6 100%)}
.lift-card{transition:all .3s ease}
.lift-card:hover{transform:translateY(-2px);box-shadow:0 10px 25px -5px rgba(0,0,0,.1)}
</style>
</head>
<body class="bg-gray-50 font-sans">
<div class="min-h-screen flex flex-col">
  <header class="gradient-bg text-white shadow-lg">
    <div class="container mx-auto px-4 py-6">
      <div class="flex items-center justify-between">
        <div class="flex items-center space-x-3">
          <i data-feather="activity" class="w-8 h-8"></i>
          <h1 class="text-2xl font-bold">Starting Strength MVP</h1>
        </div>
        <span class="hidden md:inline px-3 py-2 rounded-lg bg-white/10">Setup</span>
      </div>
    </div>
  </header>

  <main class="flex-grow container mx-auto px-4 py-8">
    <div class="max-w-4xl mx-auto">
      <div class="bg-white rounded-xl shadow-md p-6 mb-8">
        <div class="flex items-center justify-between mb-6">
          <h2 class="text-2xl font-bold text-gray-800">Your Current Program</h2>
          <span class="px-3 py-1 bg-primary-100 text-primary-800 rounded-full text-sm font-medium">Draft / Active</span>
        </div>
        <div class="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
          <div class="bg-gray-50 p-4 rounded-lg">
            <div class="text-gray-500 text-sm font-medium">Start Date</div>
            <div class="text-lg font-semibold">__START__</div>
          </div>
          <div class="bg-gray-50 p-4 rounded-lg">
            <div class="text-gray-500 text-sm font-medium">Workouts/Week</div>
            <div class="text-lg font-semibold">__WKPW__</div>
          </div>
          <div class="bg-gray-50 p-4 rounded-lg">
            <div class="text-gray-500 text-sm font-medium">Increment</div>
            <div class="text-lg font-semibold">__INC__</div>
          </div>
        </div>
        <div class="border-t pt-4">
          <h3 class="text-lg font-semibold mb-3">__LIFTS_HEADING__</h3>
          <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
            <div class="lift-card bg-white border border-gray-200 rounded-lg p-4 shadow-sm">
              <div class="flex items-center space-x-2 mb-2">
                <i data-feather="activity" class="w-5 h-5 text-primary-500"></i>
                <h4 class="font-medium text-gray-700">Squat</h4>
              </div>
              <div class="text-2xl font-bold text-primary-600">__SQ__ lbs</div>
              <div class="text-sm text-gray-500">+__INC_VAL__ lbs next</div>
            </div>
            <div class="lift-card bg-white border border-gray-200 rounded-lg p-4 shadow-sm">
              <div class="flex items-center space-x-2 mb-2">
                <i data-feather="activity" class="w-5 h-5 text-secondary-500"></i>
                <h4 class="font-medium text-gray-700">Bench Press</h4>
              </div>
              <div class="text-2xl font-bold text-secondary-600">__BP__ lbs</div>
              <div class="text-sm text-gray-500">+__INC_VAL__ lbs next</div>
            </div>
            <div class="lift-card bg-white border border-gray-200 rounded-lg p-4 shadow-sm">
              <div class="flex items-center space-x-2 mb-2">
                <i data-feather="activity" class="w-5 h-5 text-accent-500"></i>
                <h4 class="font-medium text-gray-700">Overhead Press</h4>
              </div>
              <div class="text-2xl font-bold text-accent-600">__OHP__ lbs</div>
              <div class="text-sm text-gray-500">+__INC_VAL__ lbs next</div>
            </div>
            <div class="lift-card bg-white border border-gray-200 rounded-lg p-4 shadow-sm">
              <div class="flex items-center space-x-2 mb-2">
                <i data-feather="activity" class="w-5 h-5 text-gray-500"></i>
                <h4 class="font-medium text-gray-700">Deadlift</h4>
              </div>
              <div class="text-2xl font-bold text-gray-700">__DL__ lbs</div>
              <div class="text-sm text-gray-500">+__INC_VAL__ lbs next</div>
            </div>
          </div>
        </div>
      </div>

      <div class="bg-white rounded-xl shadow-md p-6">
        <h3 class="text-lg font-semibold mb-3">Expected Progression (6 Weeks)</h3>
        <div class="rounded-lg border border-gray-200 overflow-hidden">
          __TABLE__
        </div>
        <div class="mt-4 text-sm text-gray-500">
          Use the Streamlit form below to edit, then click <b>Save & Generate Plan</b>.
        </div>
      </div>
    </div>
  </main>

  <footer class="bg-gray-100 border-t border-gray-200 py-6">
    <div class="container mx-auto px-4 flex items-center justify-between">
      <div class="flex items-center space-x-2">
        <i data-feather="activity" class="w-6 h-6 text-primary-600"></i>
        <span class="font-medium">Starting Strength MVP</span>
      </div>
      <div class="text-gray-400 flex space-x-4">
        <i data-feather="github" class="w-5 h-5"></i>
        <i data-feather="twitter" class="w-5 h-5"></i>
      </div>
    </div>
  </footer>
</div>
<script>feather.replace();</script>
</body></html>
"""
    return (tpl
            .replace("__START__", start_date_str)
            .replace("__WKPW__", wk_str)
            .replace("__INC__", inc_str)
            .replace("__LIFTS_HEADING__", heading)
            .replace("__SQ__", str(squat))
            .replace("__BP__", str(bench))
            .replace("__OHP__", str(press))
            .replace("__DL__", str(dead))
            .replace("__INC_VAL__", str(inc))
            .replace("__TABLE__", table_html))

def render_today_html(program, lifts, session_row, sets_df):
    start_date_str = dt.datetime.fromisoformat(program["start_date"]).strftime("%B %d, %Y")
    workouts_per_week = f'{program["wkpw"]} days'
    increment_str = f'{program["inc"]} lbs/session'
    workout_type = "Workout A" if session_row["type"] == "A" else "Workout B"
    session_date_str = dt.datetime.fromisoformat(session_row["date"]).strftime("%B %d, %Y")
    squat_cur = lifts.get("Squat", {}).get("current", 0)
    bench_cur = lifts.get("Bench Press", {}).get("current", 0)
    press_cur = lifts.get("Overhead Press", {}).get("current", 0)
    dead_cur  = lifts.get("Deadlift", {}).get("current", 0)
    squat_html = _sets_to_html_blocks(sets_df, "Squat", "text-primary-500")
    bench_html = _sets_to_html_blocks(sets_df, "Bench Press", "text-secondary-500")
    press_html = _sets_to_html_blocks(sets_df, "Overhead Press", "text-accent-500")
    dead_html  = _sets_to_html_blocks(sets_df, "Deadlift", "text-gray-500")
    acc_list = ACCESSORIES[session_row["type"]]
    acc_html = "\n".join([
        f"""<div class="flex items-start mb-3">
              <i data-feather="plus" class="w-5 h-5 text-secondary-500 mr-3 mt-1"></i>
              <div><h4 class="font-medium text-gray-800">{name}</h4>
              <p class="text-gray-600">{sets_reps}</p></div>
            </div>""" for name, sets_reps in acc_list
    ])
    template = r"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Starting Strength MVP</title>
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://unpkg.com/feather-icons"></script>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>.gradient-bg{background:linear-gradient(135deg,#0ea5e9 0%,#8b5cf6 100%)}.lift-card{transition:all .3s ease}.lift-card:hover{transform:translateY(-2px);box-shadow:0 10px 25px -5px rgba(0,0,0,.1)}.work-set{border-left:4px solid #0ea5e9}.warmup-set{border-left:4px solid #94a3b8}.tab-active{border-bottom:3px solid #0ea5e9;color:#0ea5e9;font-weight:600}</style>
</head><body class="bg-gray-50 font-sans">
<div class="min-h-screen flex flex-col">
<header class="gradient-bg text-white shadow-lg"><div class="container mx-auto px-4 py-6">
<div class="flex items-center justify-between">
<div class="flex items-center space-x-3"><i data-feather="activity" class="w-8 h-8"></i><h1 class="text-2xl font-bold">Starting Strength MVP</h1></div>
<div class="hidden md:flex items-center space-x-4"><span class="px-3 py-2 rounded-lg bg-white/10">Dashboard</span></div>
</div></div></header>
<main class="flex-grow container mx-auto px-4 py-8">
<div class="max-w-4xl mx-auto">
<div class="bg-white rounded-xl shadow-md p-6 mb-8">
<div class="flex items-center justify-between mb-6"><h2 class="text-2xl font-bold text-gray-800">Your Current Program</h2>
<span class="px-3 py-1 bg-primary-100 text-primary-800 rounded-full text-sm font-medium">Active</span></div>
<div class="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
<div class="bg-gray-50 p-4 rounded-lg"><div class="text-gray-500 text-sm font-medium">Start Date</div><div class="text-lg font-semibold">__START_DATE__</div></div>
<div class="bg-gray-50 p-4 rounded-lg"><div class="text-gray-500 text-sm font-medium">Workouts/Week</div><div class="text-lg font-semibold">__WKPW__</div></div>
<div class="bg-gray-50 p-4 rounded-lg"><div class="text-gray-500 text-sm font-medium">Increment</div><div class="text-lg font-semibold">__INC__</div></div>
</div>
<div class="border-t pt-4">
<h3 class="text-lg font-semibold mb-3">Current Lifts</h3>
<div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
<div class="lift-card bg-white border border-gray-200 rounded-lg p-4 shadow-sm"><div class="flex items-center space-x-2 mb-2"><i data-feather="activity" class="w-5 h-5 text-primary-500"></i><h4 class="font-medium text-gray-700">Squat</h4></div><div class="text-2xl font-bold text-primary-600">__SQUAT__ lbs</div><div class="text-sm text-gray-500">+__INC_VAL__ lbs next</div></div>
<div class="lift-card bg-white border border-gray-200 rounded-lg p-4 shadow-sm"><div class="flex items-center space-x-2 mb-2"><i data-feather="activity" class="w-5 h-5 text-secondary-500"></i><h4 class="font-medium text-gray-700">Bench Press</h4></div><div class="text-2xl font-bold text-secondary-600">__BENCH__ lbs</div><div class="text-sm text-gray-500">+__INC_VAL__ lbs next</div></div>
<div class="lift-card bg-white border border-gray-200 rounded-lg p-4 shadow-sm"><div class="flex items-center space-x-2 mb-2"><i data-feather="activity" class="w-5 h-5 text-accent-500"></i><h4 class="font-medium text-gray-700">Overhead Press</h4></div><div class="text-2xl font-bold text-accent-600">__PRESS__ lbs</div><div class="text-sm text-gray-500">+__INC_VAL__ lbs next</div></div>
<div class="lift-card bg-white border border-gray-200 rounded-lg p-4 shadow-sm"><div class="flex items-center space-x-2 mb-2"><i data-feather="activity" class="w-5 h-5 text-gray-500"></i><h4 class="font-medium text-gray-700">Deadlift</h4></div><div class="text-2xl font-bold text-gray-700">__DEADLIFT__ lbs</div><div class="text-sm text-gray-500">+__INC_VAL__ lbs next</div></div>
</div></div></div>
<div class="flex border-b border-gray-200 mb-6"><span class="px-4 py-3 font-medium text-gray-700 tab-active mr-2">Today's Workout</span><span class="px-4 py-3 font-medium text-gray-500">Program Setup</span><span class="px-4 py-3 font-medium text-gray-500">Progress</span></div>
<div class="bg-white rounded-xl shadow-md overflow-hidden mb-8"><div class="p-6 border-b border-gray-200">
<div class="flex flex-col sm:flex-row sm:items-center sm:justify-between"><div><h2 class="text-xl font-bold text-gray-800">__WORKOUT_TYPE__</h2><p class="text-gray-600">__SESSION_DATE__</p></div>
<span class="mt-3 sm:mt-0 px-4 py-2 bg-primary-600 text-white font-medium rounded-lg">Use the Streamlit buttons below to manage this session</span></div></div>
<div class="p-6"><h3 class="text-lg font-semibold mb-4">Main Lifts</h3>__SQUAT_BLOCK____BENCH_BLOCK____PRESS_BLOCK____DEAD_BLOCK__
<div><h3 class="text-lg font-semibold mb-3">Accessory Work</h3><div class="bg-gray-50 rounded-lg p-4">__ACCESSORIES__</div></div>
</div></div></div></main>
<footer class="bg-gray-100 border-t border-gray-200 py-6"><div class="container mx-auto px-4"><div class="flex items-center space-x-2"><i data-feather="activity" class="w-6 h-6 text-primary-600"></i><span class="font-medium">Starting Strength MVP</span></div></div></footer>
</div><script src="https://unpkg.com/feather-icons"></script><script>feather.replace();</script></body></html>"""
    html_filled = (template
        .replace("__START_DATE__", start_date_str)
        .replace("__WKPW__", workouts_per_week)
        .replace("__INC__", increment_str)
        .replace("__SQUAT__", str(squat_cur))
        .replace("__BENCH__", str(bench_cur))
        .replace("__PRESS__", str(press_cur))
        .replace("__DEADLIFT__", str(dead_cur))
        .replace("__INC_VAL__", str(program["inc"]))
        .replace("__WORKOUT_TYPE__", workout_type)
        .replace("__SESSION_DATE__", session_date_str)
        .replace("__SQUAT_BLOCK__", squat_html)
        .replace("__BENCH_BLOCK__", bench_html)
        .replace("__PRESS_BLOCK__", press_html)
        .replace("__DEAD_BLOCK__", dead_html)
        .replace("__ACCESSORIES__", acc_html))
    return html_filled

# ---------------- Streamlit UI ----------------

st.set_page_config(page_title="Starting Strength MVP", page_icon="🏋️", layout="centered")
st.title("🏋️ Starting Strength MVP (A/B)")
init_db()
user_id = ensure_user()

tab1, tab2, tab3 = st.tabs(["Setup", "Today's Session", "Progress Charts"])

with tab1:
    st.subheader("Program Setup (Styled)")
    # Load existing (or defaults for draft)
    prog_existing = get_program(user_id)
    lifts_existing = get_lifts(user_id)

    # Draft values (use existing if available; else defaults)
    draft_start = (prog_existing["start_date"] if prog_existing else dt.date.today().isoformat())
    draft_wkpw  = (prog_existing["wkpw"]       if prog_existing else 3)
    draft_inc   = (prog_existing["inc"]        if prog_existing else 5)
    draft_lifts = {
        "Squat":          lifts_existing.get("Squat", {}).get("start", 135),
        "Bench Press":    lifts_existing.get("Bench Press", {}).get("start", 95),
        "Overhead Press": lifts_existing.get("Overhead Press", {}).get("start", 65),
        "Deadlift":       lifts_existing.get("Deadlift", {}).get("start", 155),
    }

    # Preview progression from draft values
    proj_df = simulate_expected_progress(
        {"Squat": draft_lifts["Squat"], "Bench Press": draft_lifts["Bench Press"],
         "Overhead Press": draft_lifts["Overhead Press"], "Deadlift": draft_lifts["Deadlift"]},
        draft_inc, weeks=6, wkpw=draft_wkpw
    )

    # If program exists, show current lifts; else show starting weights
    heading = "Current Lifts" if prog_existing else "Starting Working Weights"
    display_lifts = (
        {k: lifts_existing[k]["current"] for k in ["Squat","Bench Press","Overhead Press","Deadlift"] if k in lifts_existing}
        if prog_existing else draft_lifts
    )

    # Render the Tailwind setup mockup (values injected)
    html_component(
        render_setup_html(draft_start, draft_wkpw, draft_inc, display_lifts, proj_df, heading=heading),
        height=900, scrolling=True
    )

    # Streamlit edit form (minimal) + save
    with st.expander("Edit setup values"):
        colA, colB = st.columns(2)
        with colA:
            start_date = st.date_input("Start date", dt.date.fromisoformat(draft_start))
            wkpw = st.selectbox("Workouts per week", [3, 4], index=[3,4].index(draft_wkpw))
            inc = st.number_input("Increment per session (lbs)", min_value=2, max_value=10, value=int(draft_inc), step=1)
        with colB:
            sq = st.number_input("Squat (lbs)", min_value=45, step=5, value=int(draft_lifts["Squat"]))
            bp = st.number_input("Bench Press (lbs)", min_value=45, step=5, value=int(draft_lifts["Bench Press"]))
            ohp = st.number_input("Overhead Press (lbs)", min_value=45, step=5, value=int(draft_lifts["Overhead Press"]))
            dl = st.number_input("Deadlift (lbs)", min_value=65, step=5, value=int(draft_lifts["Deadlift"]))
        if st.button("Save & Generate Plan"):
            save_program(user_id, start_date.isoformat(), int(wkpw), int(inc))
            save_lift(user_id, "Squat", sq, inc)
            save_lift(user_id, "Bench Press", bp, inc)
            save_lift(user_id, "Overhead Press", ohp, inc)
            save_lift(user_id, "Deadlift", dl, inc)
            schedule_plan(user_id, 6, start_date.isoformat(), int(wkpw))
            st.success("Program saved and sessions scheduled! Refresh above to see updated preview.")

with tab2:
    st.subheader("Today's Session")
    df = session_summary(user_id)
    if df.empty:
        st.warning("No sessions yet. Go to Setup to create your plan.")
    else:
        upcoming = df[df["done"] == 0]
        if upcoming.empty:
            st.success("All sessions complete! Head to Progress Charts.")
        else:
            row = upcoming.iloc[0]
            st.write(f"**Date:** {row['date']} | **Workout:** {row['type']}")
            lifts = get_lifts(user_id)
            c1, c2 = st.columns(2)
            with c1:
                if st.button("Generate Sets"):
                    generate_sets_for_session(int(row["session_id"]), row["type"], lifts)
            with c2:
                if st.button("Mark Complete & Progress Weights"):
                    mark_complete_and_progress(user_id, int(row["session_id"]))
                    st.success("Session marked complete and trained lifts incremented!")
            sets_df = load_sets(int(row["session_id"]))
            prog = get_program(user_id)
            html_component(render_today_html(prog, lifts, row, sets_df), height=1600, scrolling=True)

with tab3:
    st.subheader("Progress Charts")
    hist = history_df(user_id)
    if hist.empty:
        st.info("No completed sessions yet.")
    else:
        for lift in LIFTS:
            d = hist[hist["Lift"] == lift]
            if d.empty: continue
            fig, ax = plt.subplots()
            ax.plot(pd.to_datetime(d["Date"]), d["TopSet"], marker="o")
            ax.set_title(lift); ax.set_xlabel("Date"); ax.set_ylabel("Top Set (lbs)")
            st.pyplot(fig)
        st.dataframe(hist.pivot_table(index="Date", columns="Lift", values="TopSet"), use_container_width=True)
