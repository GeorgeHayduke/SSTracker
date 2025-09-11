import streamlit as st
import sqlite3
import pandas as pd
import datetime as dt
import matplotlib.pyplot as plt

DB_PATH = "progress.db"

LIFTS = ["Squat", "Bench Press", "Overhead Press", "Deadlift"]
ACCESSORIES = {
    "A": [("Barbell Curl", "3x10"), ("Lat Pulldown", "3x10")],
    "B": [("Triceps Pushdown", "3x12"), ("Barbell Curl", "3x10")],
}

def round_to_5(x):
    return int(round(x / 5.0) * 5)

def default_warmups(work_weight, lift_name):
    """
    Warmup scheme (percent midpoints):
      - ~50% x5
      - ~70% x3
      - ~90% x(1 for deadlift, 2 for others)
    Deadlift: NO 45-lb bar warmup. Others: optional bar sets if ww <= 135.
    """
    ww = max(work_weight, 45)
    is_deadlift = (lift_name == "Deadlift")
    warmups = []

    # Optional bar sets (skip for deadlift; include for others if light)
    if not is_deadlift and ww <= 135:
        warmups.append((45, 5))
        warmups.append((45, 5))

    perc_scheme = [(0.50, 5), (0.70, 3), (0.90, 1 if is_deadlift else 2)]
    for p, reps in perc_scheme:
        w = max(45, round_to_5(ww * p))
        if w < ww and (len(warmups) == 0 or warmups[-1][0] != w):
            warmups.append((w, reps))
    return warmups

def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    with get_conn() as con:
        cur = con.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS programs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            weeks INTEGER,
            start_date TEXT,
            workout_days_per_week INTEGER,
            increment_lbs INTEGER DEFAULT 5,
            UNIQUE(user_id)
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS lifts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            name TEXT,
            start_weight INTEGER,
            current_weight INTEGER,
            increment_lbs INTEGER DEFAULT 5,
            UNIQUE(user_id, name)
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            date TEXT,
            workout_type TEXT,
            completed INTEGER DEFAULT 0
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS sets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,
            lift_name TEXT,
            set_index INTEGER,
            weight INTEGER,
            reps INTEGER,
            is_warmup INTEGER DEFAULT 0
        )
        """)
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
        cur.execute("""INSERT OR REPLACE INTO programs (id, user_id, weeks, start_date, workout_days_per_week, increment_lbs)
                       VALUES (
                           COALESCE((SELECT id FROM programs WHERE user_id=?), NULL),
                           ?, ?, ?, ?, ?
                       )""", (user_id, user_id, 6, start_date, wkpw, increment_lbs))
        con.commit()

def save_lift(user_id, name, start_weight, increment_lbs):
    with get_conn() as con:
        cur = con.cursor()
        cur.execute("""INSERT OR REPLACE INTO lifts (id, user_id, name, start_weight, current_weight, increment_lbs)
                       VALUES (
                         COALESCE((SELECT id FROM lifts WHERE user_id=? AND name=?), NULL),
                         ?, ?, ?, ?, ?
                       )""", (user_id, name, user_id, name, start_weight, start_weight, increment_lbs))
        con.commit()

def get_program(user_id):
    with get_conn() as con:
        cur = con.cursor()
        cur.execute("SELECT weeks, start_date, workout_days_per_week, increment_lbs FROM programs WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        if row:
            return {"weeks": row[0], "start_date": row[1], "wkpw": row[2], "inc": row[3]}
        return None

def get_lifts(user_id):
    with get_conn() as con:
        cur = con.cursor()
        cur.execute("SELECT name, start_weight, current_weight, increment_lbs FROM lifts WHERE user_id=?", (user_id,))
        rows = cur.fetchall()
        return {r[0]: {"start": r[1], "current": r[2], "inc": r[3]} for r in rows}

def update_lift_weight(user_id, name, new_weight):
    with get_conn() as con:
        cur = con.cursor()
        cur.execute("UPDATE lifts SET current_weight=? WHERE user_id=? AND name=?", (new_weight, user_id, name))
        con.commit()

def schedule_plan(user_id, weeks, start_date, wkpw):
    # Alternate A/B; naive date spacing (2 days between sessions if 3x/week)
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
            cur.execute("INSERT INTO sessions (user_id, date, workout_type, completed) VALUES (?, ?, ?, 0)", (user_id, date_str, t))
        con.commit()

def session_summary(user_id):
    with get_conn() as con:
        cur = con.cursor()
        cur.execute("SELECT id, date, workout_type, completed FROM sessions WHERE user_id=? ORDER BY date", (user_id,))
        rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=["session_id", "date", "type", "done"])
    return df

def generate_sets_for_session(session_id, workout_type, lifts_dict):
    # A: Squat 3x5, Bench 3x5, Deadlift 1x5
    # B: Squat 3x5, Press 3x5,  Deadlift 1x5
    plan = []

    def add_exercise(name, work_weight, work_sets, reps_each):
        wups = default_warmups(work_weight, name)
        for w, r in wups:
            plan.append((name, len(plan), int(w), int(r), 1))
        for _ in range(work_sets):
            plan.append((name, len(plan), int(work_weight), int(reps_each), 0))

    if workout_type == "A":
        add_exercise("Squat", lifts_dict["Squat"]["current"], 3, 5)
        add_exercise("Bench Press", lifts_dict["Bench Press"]["current"], 3, 5)
        add_exercise("Deadlift", lifts_dict["Deadlift"]["current"], 1, 5)
    else:
        add_exercise("Squat", lifts_dict["Squat"]["current"], 3, 5)
        add_exercise("Overhead Press", lifts_dict["Overhead Press"]["current"], 3, 5)
        add_exercise("Deadlift", lifts_dict["Deadlift"]["current"], 1, 5)

    with get_conn() as con:
        cur = con.cursor()
        cur.execute("DELETE FROM sets WHERE session_id=?", (session_id,))
        for (lift_name, idx, w, r, iswu) in plan:
            cur.execute("""INSERT INTO sets (session_id, lift_name, set_index, weight, reps, is_warmup)
                           VALUES (?, ?, ?, ?, ?, ?)""", (session_id, lift_name, idx, int(w), int(r), int(iswu)))
        con.commit()

def mark_complete_and_progress(user_id, session_id):
    # Increment ONLY lifts that had work sets in this session
    with get_conn() as con:
        cur = con.cursor()
        cur.execute("UPDATE sessions SET completed=1 WHERE id=?", (session_id,))
        cur.execute("""SELECT DISTINCT lift_name FROM sets
                       WHERE session_id=? AND is_warmup=0""", (session_id,))
        lifts_in_session = [row[0] for row in cur.fetchall()]
        con.commit()

    lifts = get_lifts(user_id)
    for name in lifts_in_session:
        new_w = round_to_5(lifts[name]["current"] + lifts[name]["inc"])
        update_lift_weight(user_id, name, new_w)

def load_sets(session_id):
    with get_conn() as con:
        cur = con.cursor()
        cur.execute("""SELECT lift_name, set_index, weight, reps, is_warmup
                       FROM sets WHERE session_id=? ORDER BY set_index""", (session_id,))
        rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=["Lift", "Set#", "Weight", "Reps", "Warmup"])
    return df

def history_df(user_id):
    with get_conn() as con:
        cur = con.cursor()
        cur.execute("""SELECT s.date, se.lift_name, MAX(CASE WHEN se.is_warmup=0 THEN se.weight ELSE NULL END) as top_set
                       FROM sessions s
                       JOIN sets se ON se.session_id=s.id
                       WHERE s.user_id=? AND s.completed=1
                       GROUP BY s.date, se.lift_name
                       ORDER BY s.date""", (user_id,))
        rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=["Date", "Lift", "TopSet"])
    return df

def simulate_expected_progress(start_weights, inc, weeks, wkpw):
    """Return expected working weights by week over 6 weeks.
       Simulates A/B sessions, increments only when the lift is trained.
    """
    curr = {k: start_weights[k] for k in start_weights.keys()}
    sessions_per_week = wkpw
    total_sessions = weeks * sessions_per_week
    schedule = [("A" if i % 2 == 0 else "B") for i in range(total_sessions)]

    week_rows = []
    for s_idx, t in enumerate(schedule, start=1):
        if t == "A":
            for lift in ["Squat", "Bench Press", "Deadlift"]:
                curr[lift] = round_to_5(curr[lift] + inc)
        else:
            for lift in ["Squat", "Overhead Press", "Deadlift"]:
                curr[lift] = round_to_5(curr[lift] + inc)
        if s_idx % sessions_per_week == 0:
            week_num = s_idx // sessions_per_week
            week_rows.append({
                "Week": week_num,
                "Squat": curr["Squat"],
                "Bench": curr["Bench Press"],
                "Press": curr["Overhead Press"],
                "Deadlift": curr["Deadlift"],
            })
    return pd.DataFrame(week_rows)

# ---------- UI ----------

st.set_page_config(page_title="Starting Strength MVP", page_icon="🏋️", layout="centered")
st.title("🏋️ Starting Strength MVP (A/B)")

init_db()
user_id = ensure_user()

tab1, tab2, tab3 = st.tabs(["Setup", "Today's Session", "Progress Charts"])

with tab1:
    st.subheader("Program Setup")
    st.markdown("**Program length:** 6 weeks (fixed)")
    wkpw = st.selectbox("Workouts per week", [3, 4], index=0)
    start_date = st.date_input("Start date", dt.date.today())
    inc = st.number_input("Increment per session (lbs)", min_value=2, max_value=10, value=5, step=1)

    st.markdown("### Starting Working Weights")
    col1, col2 = st.columns(2)
    with col1:
        sq = st.number_input("Squat (lbs)", min_value=45, step=5, value=135)
        bp = st.number_input("Bench Press (lbs)", min_value=45, step=5, value=95)
    with col2:
        ohp = st.number_input("Overhead Press (lbs)", min_value=45, step=5, value=65)
        dl = st.number_input("Deadlift (lbs)", min_value=65, step=5, value=155)

    if st.button("Save & Generate Plan"):
        save_program(user_id, start_date.isoformat(), wkpw, inc)
        save_lift(user_id, "Squat", sq, inc)
        save_lift(user_id, "Bench Press", bp, inc)
        save_lift(user_id, "Overhead Press", ohp, inc)
        save_lift(user_id, "Deadlift", dl, inc)
        schedule_plan(user_id, 6, start_date.isoformat(), wkpw)
        st.success("Program saved and sessions scheduled!")

        start_weights = {"Squat": sq, "Bench Press": bp, "Overhead Press": ohp, "Deadlift": dl}
        proj = simulate_expected_progress(start_weights, inc, weeks=6, wkpw=wkpw)
        st.markdown("### Expected Working Weight Progression (End of Each Week)")
        st.dataframe(proj, hide_index=True, use_container_width=True)

    prog = get_program(user_id)
    if prog:
        st.info(f"Current program: {prog['weeks']} weeks from {prog['start_date']} | {prog['wkpw']}x/week | +{prog['inc']} lbs/session")
    lifts = get_lifts(user_id)
    if lifts:
        st.dataframe(pd.DataFrame(lifts).T.rename(columns={"start":"Start","current":"Current","inc":"Inc lbs"}), use_container_width=True)

with tab2:
    st.subheader("Today's Session")
    df = session_summary(user_id)
    if df.empty:
        st.warning("No sessions yet. Go to Setup to create your plan.")
    else:
        upcoming = df[(df["done"]==0)]
        if upcoming.empty:
            st.success("All sessions complete! Head to Progress Charts.")
        else:
            row = upcoming.iloc[0]
            st.write(f"**Date:** {row['date']} | **Workout:** {row['type']}")
            lifts = get_lifts(user_id)
            if st.button("Generate Sets"):
                generate_sets_for_session(int(row["session_id"]), row["type"], lifts)

            sets_df = load_sets(int(row["session_id"]))
            if not sets_df.empty:
                warmups_df = sets_df[sets_df["Warmup"]==1].copy()
                works_df = sets_df[sets_df["Warmup"]==0].copy()

                st.markdown("#### Warmups")
                st.dataframe(
                    warmups_df.style.set_properties(**{
                        "color": "#555",
                        "font-style": "italic",
                        "background-color": "#f5f5f5"
                    }),
                    hide_index=True, use_container_width=True
                )

                st.markdown("#### Work Sets")
                st.dataframe(
                    works_df.style.set_properties(**{
                        "font-weight": "600",
                    }),
                    hide_index=True, use_container_width=True
                )

                st.markdown("#### Suggested Accessories")
                acc = ACCESSORIES[row["type"]]
                for name, sets_reps in acc:
                    st.write(f"- **{name}** — {sets_reps}")

                if st.button("Mark Complete & Progress Weights"):
                    mark_complete_and_progress(user_id, int(row["session_id"]))
                    st.success("Session marked complete and trained lifts incremented!")

with tab3:
    st.subheader("Progress Charts")
    hist = history_df(user_id)
    if hist.empty:
        st.info("No completed sessions yet.")
    else:
        for lift in LIFTS:
            d = hist[hist["Lift"]==lift]
            if d.empty:
                continue
            fig, ax = plt.subplots()
            ax.plot(pd.to_datetime(d["Date"]), d["TopSet"], marker="o")
            ax.set_title(lift)
            ax.set_xlabel("Date")
            ax.set_ylabel("Top Set (lbs)")
            st.pyplot(fig)
        st.dataframe(hist.pivot_table(index="Date", columns="Lift", values="TopSet"), use_container_width=True)
