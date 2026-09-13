from datetime import date
import pandas as pd
import requests
import streamlit as st
from sqlalchemy import text

SRU_GREEN = "#016F54"

# Coach login password. Change this to whatever you want before you start using it.
# This is a basic shared password, not bank-grade security — fine for a private
# team tool, but don't reuse a password you care about elsewhere.
COACH_PASSWORD = "changeme"

# Supabase Storage bucket used for videos and workout attachments.
# Create this bucket once in Supabase (Storage -> New bucket), name it exactly
# this, and mark it Public so uploaded files can be played/downloaded directly.
STORAGE_BUCKET = "media"

# This connects using the [connections.sql] section of your secrets.toml
# (locally) or the Secrets panel in Streamlit Community Cloud (when hosted).
conn = st.connection("sql", type="sql")


def init_db():
    with conn.session as s:
        s.execute(text("""CREATE TABLE IF NOT EXISTS pitchers(
            id SERIAL PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            throws TEXT,
            class_year TEXT,
            pin TEXT)"""))
        s.execute(text("""CREATE TABLE IF NOT EXISTS games(
            id SERIAL PRIMARY KEY,
            game_date TEXT NOT NULL,
            opponent TEXT NOT NULL,
            pitcher_id INTEGER NOT NULL REFERENCES pitchers(id),
            innings REAL DEFAULT 0,
            outs INTEGER DEFAULT 0,
            pitches INTEGER DEFAULT 0,
            balls INTEGER DEFAULT 0,
            strikes INTEGER DEFAULT 0,
            whiffs INTEGER DEFAULT 0,
            strikeouts INTEGER DEFAULT 0,
            walks INTEGER DEFAULT 0,
            hits INTEGER DEFAULT 0,
            home_runs INTEGER DEFAULT 0,
            first_pitch_strikes INTEGER DEFAULT 0,
            early_ahead INTEGER DEFAULT 0,
            avg_velo REAL DEFAULT 0,
            max_velo REAL DEFAULT 0,
            earned_runs INTEGER DEFAULT 0)"""))
        # at_bats is the denominator for First-Pitch Strike % and Early/Ahead %
        # (batters faced), added after the fact — IF NOT EXISTS keeps this safe
        # to run on a database that already has the games table.
        s.execute(text("ALTER TABLE games ADD COLUMN IF NOT EXISTS at_bats INTEGER DEFAULT 0"))
        s.execute(text("""CREATE TABLE IF NOT EXISTS videos(
            id SERIAL PRIMARY KEY,
            pitcher_id INTEGER NOT NULL REFERENCES pitchers(id),
            title TEXT,
            notes TEXT,
            file_path TEXT NOT NULL,
            uploaded_at TIMESTAMP DEFAULT now())"""))
        s.execute(text("""CREATE TABLE IF NOT EXISTS goals(
            id SERIAL PRIMARY KEY,
            pitcher_id INTEGER NOT NULL REFERENCES pitchers(id),
            goal_text TEXT NOT NULL,
            target_date TEXT,
            status TEXT DEFAULT 'In Progress',
            coach_notes TEXT,
            pitcher_notes TEXT,
            created_at TIMESTAMP DEFAULT now())"""))
        s.execute(text("""CREATE TABLE IF NOT EXISTS workouts(
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT,
            file_path TEXT,
            assigned_to INTEGER REFERENCES pitchers(id),
            due_date TEXT,
            created_at TIMESTAMP DEFAULT now())"""))
        s.commit()


# ---------------------------------------------------------------------------
# File storage (Supabase Storage) — used for pitching videos and workout files
# ---------------------------------------------------------------------------
def _storage_creds():
    sb = st.secrets.get("supabase", {})
    return sb.get("url", "").rstrip("/"), sb.get("service_key", "")


def upload_file(uploaded_file, folder):
    """Uploads a Streamlit UploadedFile to Supabase Storage. Returns the
    storage path on success, or None on failure (with an st.error shown)."""
    base_url, service_key = _storage_creds()
    if not base_url or not service_key:
        st.error("File storage isn't configured yet — add [supabase] url/service_key to Secrets.")
        return None
    safe_name = uploaded_file.name.replace(" ", "_")
    path = f"{folder}/{int(pd.Timestamp.now().timestamp())}_{safe_name}"
    resp = requests.post(
        f"{base_url}/storage/v1/object/{STORAGE_BUCKET}/{path}",
        headers={
            "Authorization": f"Bearer {service_key}",
            "apikey": service_key,
            "Content-Type": uploaded_file.type or "application/octet-stream",
        },
        data=uploaded_file.getvalue(),
    )
    if resp.status_code in (200, 201):
        return path
    st.error(f"Upload failed ({resp.status_code}): {resp.text[:200]}")
    return None


def public_url(path):
    base_url, _ = _storage_creds()
    return f"{base_url}/storage/v1/object/public/{STORAGE_BUCKET}/{path}"


def delete_storage_file(path):
    base_url, service_key = _storage_creds()
    if not path:
        return
    requests.delete(
        f"{base_url}/storage/v1/object/{STORAGE_BUCKET}/{path}",
        headers={"Authorization": f"Bearer {service_key}", "apikey": service_key},
    )


def outs_to_ip(total_outs):
    total_outs = int(total_outs or 0)
    full, rem = divmod(total_outs, 3)
    return f"{full}.{rem}"


def get_pitchers():
    return conn.query("SELECT * FROM pitchers ORDER BY name", ttl=0)


def add_pitcher(name, throws, year, pin):
    with conn.session as s:
        s.execute(
            text("""INSERT INTO pitchers(name,throws,class_year,pin)
                VALUES(:name,:throws,:year,:pin)
                ON CONFLICT (name) DO NOTHING"""),
            {"name": name.strip(), "throws": throws, "year": year, "pin": str(pin).strip()},
        )
        s.commit()


def check_pitcher_login(name, pin):
    df = conn.query(
        "SELECT id FROM pitchers WHERE name=:name AND pin=:pin",
        params={"name": name, "pin": str(pin).strip()},
        ttl=0,
    )
    return int(df.iloc[0].id) if not df.empty else None


def delete_pitcher(pid):
    """Removes a pitcher and everything tied to them: games, goals, videos
    (including the actual files in storage), and any workouts assigned
    specifically to them (team-wide workouts are left alone)."""
    vids = get_videos(pid)
    for _, v in vids.iterrows():
        delete_storage_file(v.file_path)
    wos = conn.query(
        "SELECT file_path FROM workouts WHERE assigned_to=:pid", params={"pid": pid}, ttl=0
    )
    for _, w in wos.iterrows():
        if w.file_path:
            delete_storage_file(w.file_path)
    with conn.session as s:
        s.execute(text("DELETE FROM videos WHERE pitcher_id=:pid"), {"pid": pid})
        s.execute(text("DELETE FROM goals WHERE pitcher_id=:pid"), {"pid": pid})
        s.execute(text("DELETE FROM workouts WHERE assigned_to=:pid"), {"pid": pid})
        s.execute(text("DELETE FROM games WHERE pitcher_id=:pid"), {"pid": pid})
        s.execute(text("DELETE FROM pitchers WHERE id=:pid"), {"pid": pid})
        s.commit()


def add_game(vals):
    keys = [
        "game_date", "opponent", "pitcher_id", "innings", "outs", "pitches", "balls", "strikes",
        "whiffs", "strikeouts", "walks", "hits", "home_runs", "first_pitch_strikes",
        "early_ahead", "at_bats", "avg_velo", "max_velo", "earned_runs",
    ]
    params = dict(zip(keys, vals))
    with conn.session as s:
        s.execute(
            text(f"""INSERT INTO games({','.join(keys)})
                VALUES({','.join(':' + k for k in keys)})"""),
            params,
        )
        s.commit()


def get_games(pid=None):
    if pid:
        return conn.query(
            """SELECT g.*, p.name AS pitcher FROM games g
            JOIN pitchers p ON g.pitcher_id=p.id WHERE g.pitcher_id=:pid
            ORDER BY g.game_date DESC, g.id DESC""",
            params={"pid": pid},
            ttl=0,
        )
    return conn.query(
        """SELECT g.*, p.name AS pitcher FROM games g
        JOIN pitchers p ON g.pitcher_id=p.id ORDER BY g.game_date DESC, g.id DESC""",
        ttl=0,
    )


def _native(v):
    """Converts a pandas/numpy scalar (as returned by st.data_editor) into a
    plain Python type psycopg2 can bind — numpy.int64/float64 otherwise raise
    'can't adapt type' errors."""
    if pd.isna(v):
        return None
    if hasattr(v, "item"):
        return v.item()
    return v


def delete_game(game_id):
    with conn.session as s:
        s.execute(text("DELETE FROM games WHERE id=:id"), {"id": game_id})
        s.commit()


def update_game(game_id, fields):
    """fields is a dict of column -> new value for that one game row."""
    if not fields:
        return
    params = {k: _native(v) for k, v in fields.items()}
    set_clause = ", ".join(f"{k}=:{k}" for k in params)
    params["id"] = int(game_id)
    with conn.session as s:
        s.execute(text(f"UPDATE games SET {set_clause} WHERE id=:id"), params)
        s.commit()


def update_pitcher(pid, name, throws, class_year, pin):
    with conn.session as s:
        s.execute(
            text("""UPDATE pitchers SET name=:name, throws=:throws,
                class_year=:class_year, pin=:pin WHERE id=:id"""),
            {
                "name": _native(name), "throws": _native(throws),
                "class_year": _native(class_year), "pin": str(_native(pin)),
                "id": int(pid),
            },
        )
        s.commit()


# ---------------------------------------------------------------------------
# Videos
# ---------------------------------------------------------------------------
def add_video(pid, title, notes, file_path):
    with conn.session as s:
        s.execute(
            text("""INSERT INTO videos(pitcher_id,title,notes,file_path)
                VALUES(:pid,:title,:notes,:file_path)"""),
            {"pid": pid, "title": title, "notes": notes, "file_path": file_path},
        )
        s.commit()


def get_videos(pid=None):
    if pid:
        return conn.query(
            "SELECT * FROM videos WHERE pitcher_id=:pid ORDER BY uploaded_at DESC",
            params={"pid": pid}, ttl=0,
        )
    return conn.query(
        """SELECT v.*, p.name AS pitcher FROM videos v
        JOIN pitchers p ON v.pitcher_id=p.id ORDER BY v.uploaded_at DESC""",
        ttl=0,
    )


def delete_video(video_id):
    """Deletes both the DB row and the underlying file in storage, so the
    space actually frees up against the 1 GB storage cap."""
    row = conn.query("SELECT file_path FROM videos WHERE id=:id", params={"id": video_id}, ttl=0)
    if not row.empty:
        delete_storage_file(row.iloc[0].file_path)
    with conn.session as s:
        s.execute(text("DELETE FROM videos WHERE id=:id"), {"id": video_id})
        s.commit()


# ---------------------------------------------------------------------------
# Goals
# ---------------------------------------------------------------------------
def add_goal(pid, goal_text, target_date, coach_notes):
    with conn.session as s:
        s.execute(
            text("""INSERT INTO goals(pitcher_id,goal_text,target_date,coach_notes)
                VALUES(:pid,:goal_text,:target_date,:coach_notes)"""),
            {"pid": pid, "goal_text": goal_text, "target_date": target_date, "coach_notes": coach_notes},
        )
        s.commit()


def get_goals(pid=None):
    if pid:
        return conn.query(
            "SELECT * FROM goals WHERE pitcher_id=:pid ORDER BY created_at DESC",
            params={"pid": pid}, ttl=0,
        )
    return conn.query(
        """SELECT g.*, p.name AS pitcher FROM goals g
        JOIN pitchers p ON g.pitcher_id=p.id ORDER BY g.created_at DESC""",
        ttl=0,
    )


def update_goal_status(goal_id, status):
    with conn.session as s:
        s.execute(
            text("UPDATE goals SET status=:status WHERE id=:id"),
            {"status": status, "id": goal_id},
        )
        s.commit()


def update_goal_pitcher_notes(goal_id, notes):
    with conn.session as s:
        s.execute(
            text("UPDATE goals SET pitcher_notes=:notes WHERE id=:id"),
            {"notes": notes, "id": goal_id},
        )
        s.commit()


# ---------------------------------------------------------------------------
# Workouts
# ---------------------------------------------------------------------------
def add_workout(title, description, file_path, assigned_to, due_date):
    with conn.session as s:
        s.execute(
            text("""INSERT INTO workouts(title,description,file_path,assigned_to,due_date)
                VALUES(:title,:description,:file_path,:assigned_to,:due_date)"""),
            {"title": title, "description": description, "file_path": file_path,
             "assigned_to": assigned_to, "due_date": due_date},
        )
        s.commit()


def get_workouts(pid=None):
    if pid:
        return conn.query(
            """SELECT * FROM workouts WHERE assigned_to=:pid OR assigned_to IS NULL
            ORDER BY created_at DESC""",
            params={"pid": pid}, ttl=0,
        )
    return conn.query(
        """SELECT w.*, p.name AS assigned_name FROM workouts w
        LEFT JOIN pitchers p ON w.assigned_to=p.id ORDER BY w.created_at DESC""",
        ttl=0,
    )


def delete_workout(workout_id):
    row = conn.query("SELECT file_path FROM workouts WHERE id=:id", params={"id": workout_id}, ttl=0)
    if not row.empty and row.iloc[0].file_path:
        delete_storage_file(row.iloc[0].file_path)
    with conn.session as s:
        s.execute(text("DELETE FROM workouts WHERE id=:id"), {"id": workout_id})
        s.commit()


def summarize_games(x):
    """Aggregates a set of already-fetched game rows into the standard stat
    dict. Shared by season totals (aggregate) and series totals, so both use
    identical math.

    First-pitch-strike %, Early/Ahead %, BB Rate, and K Rate all use at-bats
    (batters faced) as the denominator, since those are batter-count stats,
    not pitch-count stats. Games logged before at-bats was tracked will show
    0 at-bats and won't contribute to those percentages.
    """
    if x.empty:
        return None
    pitches = max(x.pitches.sum(), 1)
    at_bats = max(x.at_bats.sum(), 1)
    total_outs = int(x.outs.sum())
    return {
        "Games": len(x),
        "IP": outs_to_ip(total_outs),
        "Pitches": int(x.pitches.sum()),
        "At Bats": int(x.at_bats.sum()),
        "Balls": int(x.balls.sum()),
        "Strikes": int(x.strikes.sum()),
        "Strike %": x.strikes.sum() / pitches,
        "Ball %": x.balls.sum() / pitches,
        "First-Pitch Strike %": x.first_pitch_strikes.sum() / at_bats,
        "Early/Ahead %": x.early_ahead.sum() / at_bats,
        "BB Rate": x.walks.sum() / at_bats,
        "K Rate": x.strikeouts.sum() / at_bats,
        "Whiffs": int(x.whiffs.sum()),
        "Strikeouts": int(x.strikeouts.sum()),
        "Walks": int(x.walks.sum()),
        "Hits": int(x.hits.sum()),
        "HR": int(x.home_runs.sum()),
        "Avg Velo": (x.avg_velo * x.pitches).sum() / pitches,
        "Max Velo": x.max_velo.max(),
        "ER": int(x.earned_runs.sum()),
    }


def aggregate(pid):
    return summarize_games(get_games(pid))


def build_series(pid):
    """Groups a pitcher's games into 'series' — runs of games on
    consecutive calendar days (a gap of more than 1 day starts a new
    series), matching how games get logged in 2-3 day sets. Returns a list
    of dicts, most recent series first, each with the day-by-day games and
    one combined summary for the whole series.
    """
    g = get_games(pid)
    if g.empty:
        return []
    g = g.copy()
    g["_date"] = pd.to_datetime(g["game_date"], errors="coerce")
    g = g.sort_values("_date")
    dates = sorted(g["_date"].dropna().unique())
    if not dates:
        return []

    groups = [[dates[0]]]
    for d in dates[1:]:
        if (d - groups[-1][-1]).days <= 1:
            groups[-1].append(d)
        else:
            groups.append([d])

    series_list = []
    for group_dates in groups:
        subset = g[g["_date"].isin(group_dates)]
        daily = []
        for d in group_dates:
            day_games = subset[subset["_date"] == d]
            day_summary = summarize_games(day_games)
            daily.append({"date": d.strftime("%Y-%m-%d"), "summary": day_summary})
        combined = summarize_games(subset)
        series_list.append({
            "start": group_dates[0].strftime("%Y-%m-%d"),
            "end": group_dates[-1].strftime("%Y-%m-%d"),
            "daily": daily,
            "combined": combined,
        })
    series_list.reverse()  # most recent series first
    return series_list


def render_dashboard(pid, name, allow_delete=False):
    s = aggregate(pid)
    if not s:
        st.info("No games recorded yet.")
        return
    st.header(name)
    cols = st.columns(6)
    top = [
        ("IP", s["IP"]),
        ("Strike %", f'{s["Strike %"]:.1%}'),
        ("1st-Pitch Strike %", f'{s["First-Pitch Strike %"]:.1%}'),
        ("Early/Ahead %", f'{s["Early/Ahead %"]:.1%}'),
        ("Ball %", f'{s["Ball %"]:.1%}'),
        ("Whiffs", s["Whiffs"]),
    ]
    for col, (lab, val) in zip(cols, top):
        col.metric(lab, val)
    cols = st.columns(6)
    for col, (lab, val) in zip(
        cols,
        [
            ("K", s["Strikeouts"]),
            ("BB", s["Walks"]),
            ("H", s["Hits"]),
            ("HR", s["HR"]),
            ("Avg Velo", f'{s["Avg Velo"]:.1f}'),
            ("Max Velo", f'{s["Max Velo"]:.1f}'),
        ],
    ):
        col.metric(lab, val)

    st.divider()
    st.subheader("📈 Trends")
    g_asc = get_games(pid).sort_values("game_date").copy()
    g_asc["Strike %"] = g_asc.strikes / g_asc.pitches.replace(0, pd.NA)
    t1, t2 = st.columns(2)
    with t1:
        st.caption("Strike % by game")
        st.line_chart(g_asc.set_index("game_date")[["Strike %"]])
    with t2:
        st.caption("Velocity by game (avg vs. max)")
        st.line_chart(g_asc.set_index("game_date")[["avg_velo", "max_velo"]])

    st.divider()
    st.subheader("Season Totals")
    st.dataframe(
        pd.DataFrame(
            {
                "Metric": [
                    "Games", "IP", "Pitches", "At Bats", "Balls", "Strikes", "Strike %", "Ball %",
                    "First-Pitch Strike %", "Early/Ahead %", "BB Rate", "K Rate", "Whiffs", "Strikeouts", "Walks",
                    "Hits", "HR", "Avg velo", "Max velo", "Earned runs",
                ],
                "Value": [
                    s["Games"], s["IP"], s["Pitches"], s["At Bats"], s["Balls"], s["Strikes"], f'{s["Strike %"]:.1%}',
                    f'{s["Ball %"]:.1%}', f'{s["First-Pitch Strike %"]:.1%}', f'{s["Early/Ahead %"]:.1%}',
                    f'{s["BB Rate"]:.1%}', f'{s["K Rate"]:.1%}',
                    s["Whiffs"], s["Strikeouts"], s["Walks"], s["Hits"],
                    s["HR"], f'{s["Avg Velo"]:.1f}', f'{s["Max Velo"]:.1f}', s["ER"],
                ],
            }
        ),
        use_container_width=True,
        hide_index=True,
    )

    st.divider()
    st.subheader("🗓️ Game Series")
    st.caption("Games are grouped into runs of consecutive days — each series shows a day-by-day breakdown plus one combined total.")
    series_list = build_series(pid)
    if not series_list:
        st.info("No games recorded yet.")
    else:
        for series in series_list:
            label = series["start"] if series["start"] == series["end"] else f'{series["start"]} to {series["end"]}'
            with st.expander(f"Series: {label} ({len(series['daily'])} day(s))", expanded=(series is series_list[0])):
                daily_rows = []
                for day in series["daily"]:
                    ds = day["summary"]
                    daily_rows.append({
                        "Date": day["date"], "IP": ds["IP"], "At Bats": ds["At Bats"],
                        "Strike %": f'{ds["Strike %"]:.1%}', "FPS %": f'{ds["First-Pitch Strike %"]:.1%}',
                        "Early/Ahead %": f'{ds["Early/Ahead %"]:.1%}', "BB Rate": f'{ds["BB Rate"]:.1%}',
                        "K Rate": f'{ds["K Rate"]:.1%}', "Whiffs": ds["Whiffs"], "Max Velo": f'{ds["Max Velo"]:.1f}',
                    })
                st.dataframe(pd.DataFrame(daily_rows), use_container_width=True, hide_index=True)
                c = series["combined"]
                st.markdown("**Combined for this series:**")
                cols = st.columns(6)
                combined_metrics = [
                    ("IP", c["IP"]), ("At Bats", c["At Bats"]), ("Strike %", f'{c["Strike %"]:.1%}'),
                    ("FPS %", f'{c["First-Pitch Strike %"]:.1%}'), ("Early/Ahead %", f'{c["Early/Ahead %"]:.1%}'),
                    ("BB Rate", f'{c["BB Rate"]:.1%}'),
                ]
                for col, (lab, val) in zip(cols, combined_metrics):
                    col.metric(lab, val)
                cols2 = st.columns(6)
                combined_metrics2 = [
                    ("K Rate", f'{c["K Rate"]:.1%}'), ("Whiffs", c["Whiffs"]), ("Strikeouts", c["Strikeouts"]),
                    ("Walks", c["Walks"]), ("Avg Velo", f'{c["Avg Velo"]:.1f}'), ("Max Velo", f'{c["Max Velo"]:.1f}'),
                ]
                for col, (lab, val) in zip(cols2, combined_metrics2):
                    col.metric(lab, val)

    st.divider()
    st.subheader("Game-by-Game")
    g = get_games(pid)
    show = g[
        [
            "game_date", "opponent", "innings", "pitches", "at_bats", "balls", "strikes", "whiffs", "strikeouts",
            "walks", "hits", "home_runs", "first_pitch_strikes", "early_ahead", "avg_velo", "max_velo", "earned_runs",
        ]
    ]
    st.dataframe(show, use_container_width=True, hide_index=True)

    if allow_delete:
        st.caption("Made a mistake entering a game? Delete it here.")
        g_display = g.copy()
        g_display["label"] = g_display.apply(
            lambda r: f"{r.game_date} vs {r.opponent} (id {r.id})", axis=1
        )
        choice = st.selectbox("Select a game to delete", g_display["label"].tolist(), key=f"delgame_select_{pid}")
        if st.button("Delete selected game", key=f"delgame_btn_{pid}"):
            gid = int(choice.split("id ")[1].rstrip(")"))
            delete_game(gid)
            st.success("Game deleted.")
            st.rerun()


# ---------------------------------------------------------------------------
# Pitcher-facing pages
# ---------------------------------------------------------------------------
def render_pitcher_videos(pid, name):
    st.header("My Videos")
    st.caption("Keep clips short — 50 MB max per file on the free storage plan.")
    with st.form("upload_video"):
        f = st.file_uploader("Upload a clip", type=["mp4", "mov", "m4v", "avi"])
        title = st.text_input("Title (e.g. 'Bullpen 3/4 - fastball')")
        notes = st.text_area("Notes (optional)")
        go = st.form_submit_button("Upload")
    if go:
        if not f:
            st.error("Choose a video file first.")
        else:
            path = upload_file(f, folder=f"videos/pitcher_{pid}")
            if path:
                add_video(pid, title or f.name, notes, path)
                st.success("Uploaded.")
                st.rerun()

    st.divider()
    vids = get_videos(pid)
    if vids.empty:
        st.info("No videos uploaded yet.")
    else:
        for _, v in vids.iterrows():
            st.markdown(f"**{v.title or 'Untitled'}** — {v.uploaded_at}")
            if v.notes:
                st.caption(v.notes)
            st.video(public_url(v.file_path))
            if st.button("Delete this video", key=f"delvid_{v.id}"):
                delete_video(int(v.id))
                st.success("Deleted.")
                st.rerun()
            st.divider()


def render_pitcher_goals(pid, name):
    st.header("My Goals")
    goals = get_goals(pid)
    if goals.empty:
        st.info("No goals set yet — your coach will add some.")
        return
    for _, g in goals.iterrows():
        with st.container(border=True):
            st.markdown(f"**{g.goal_text}**")
            cols = st.columns(2)
            cols[0].caption(f"Status: {g.status}")
            if g.target_date:
                cols[1].caption(f"Target: {g.target_date}")
            if g.coach_notes:
                st.caption(f"Coach notes: {g.coach_notes}")
            note_key = f"pnote_{g.id}"
            note = st.text_area("Your progress notes", value=g.pitcher_notes or "", key=note_key)
            if st.button("Save note", key=f"savenote_{g.id}"):
                update_goal_pitcher_notes(int(g.id), note)
                st.success("Saved.")
                st.rerun()


def render_pitcher_workouts(pid, name):
    st.header("My Workouts")
    workouts = get_workouts(pid)
    if workouts.empty:
        st.info("No workouts assigned yet.")
        return
    for _, w in workouts.iterrows():
        with st.container(border=True):
            st.markdown(f"**{w.title}**")
            if w.due_date:
                st.caption(f"Due: {w.due_date}")
            if w.description:
                st.write(w.description)
            if w.file_path:
                st.markdown(f"[Open attached file]({public_url(w.file_path)})")


# ---------------------------------------------------------------------------
# Coach-facing pages
# ---------------------------------------------------------------------------
def render_coach_videos():
    st.header("Player Videos")
    ps = get_pitchers()
    if ps.empty:
        st.info("Add pitchers first.")
        return
    name = st.selectbox("Filter by pitcher", ["All"] + ps.name.tolist())
    vids = get_videos() if name == "All" else get_videos(int(ps.loc[ps.name == name, "id"].iloc[0]))
    if vids.empty:
        st.info("No videos uploaded yet.")
        return
    for _, v in vids.iterrows():
        who = v.pitcher if "pitcher" in v else name
        st.markdown(f"**{v.title or 'Untitled'}** — {who} — {v.uploaded_at}")
        if v.notes:
            st.caption(v.notes)
        st.video(public_url(v.file_path))
        if st.button("Delete this video", key=f"cdelvid_{v.id}"):
            delete_video(int(v.id))
            st.success("Deleted.")
            st.rerun()
        st.divider()


def render_coach_goals():
    st.header("Player Goals")
    ps = get_pitchers()
    if ps.empty:
        st.info("Add pitchers first.")
        return
    name = st.selectbox("Pitcher", ps.name.tolist())
    pid = int(ps.loc[ps.name == name, "id"].iloc[0])

    with st.form("add_goal"):
        goal_text = st.text_input("New goal")
        target_date = st.date_input("Target date", date.today())
        coach_notes = st.text_area("Coach notes (optional)")
        go = st.form_submit_button("Add Goal")
    if go:
        if not goal_text.strip():
            st.error("Enter a goal.")
        else:
            add_goal(pid, goal_text, str(target_date), coach_notes)
            st.success("Goal added.")
            st.rerun()

    st.divider()
    goals = get_goals(pid)
    if goals.empty:
        st.info("No goals set for this pitcher yet.")
        return
    for _, g in goals.iterrows():
        with st.container(border=True):
            st.markdown(f"**{g.goal_text}**")
            if g.target_date:
                st.caption(f"Target: {g.target_date}")
            if g.pitcher_notes:
                st.caption(f"Pitcher's notes: {g.pitcher_notes}")
            new_status = st.selectbox(
                "Status", ["In Progress", "Complete", "Not Started"],
                index=["In Progress", "Complete", "Not Started"].index(g.status) if g.status in ["In Progress", "Complete", "Not Started"] else 0,
                key=f"status_{g.id}",
            )
            if st.button("Update status", key=f"upd_{g.id}"):
                update_goal_status(int(g.id), new_status)
                st.success("Updated.")
                st.rerun()


def render_coach_workouts():
    st.header("Workouts")
    ps = get_pitchers()
    with st.form("add_workout"):
        title = st.text_input("Workout title")
        description = st.text_area("Description / instructions")
        f = st.file_uploader("Attach a file (optional — PDF, video, image)", type=None)
        assign_choice = st.selectbox("Assign to", ["Whole Team"] + ps.name.tolist())
        due_date = st.date_input("Due date", date.today())
        go = st.form_submit_button("Post Workout")
    if go:
        if not title.strip():
            st.error("Enter a title.")
        else:
            file_path = None
            if f:
                file_path = upload_file(f, folder="workouts")
            assigned_to = None
            if assign_choice != "Whole Team":
                assigned_to = int(ps.loc[ps.name == assign_choice, "id"].iloc[0])
            add_workout(title, description, file_path, assigned_to, str(due_date))
            st.success("Workout posted.")
            st.rerun()

    st.divider()
    workouts = get_workouts()
    if workouts.empty:
        st.info("No workouts posted yet.")
        return
    for _, w in workouts.iterrows():
        with st.container(border=True):
            who = w.assigned_name if pd.notna(w.assigned_name) else "Whole Team"
            st.markdown(f"**{w.title}** — assigned to: {who}")
            if w.due_date:
                st.caption(f"Due: {w.due_date}")
            if w.description:
                st.write(w.description)
            if w.file_path:
                st.markdown(f"[Open attached file]({public_url(w.file_path)})")
            if st.button("Delete this workout", key=f"delwk_{w.id}"):
                delete_workout(int(w.id))
                st.success("Deleted.")
                st.rerun()


# ---------------------------------------------------------------------------
# Leaderboard — shared by both the coach's "Team Leaderboard" page and each
# pitcher's own "Leaderboard" page, so there's exactly one implementation.
# ---------------------------------------------------------------------------
def _leaderboard_categories(df):
    """Returns {category: (pitcher_name, value)} for a slice of games.
    Every category picks the BEST pitcher — for Whiffs/K/Velo/Early-Ahead%/
    FPS% that's the highest value, but for BB Rate lower is better control,
    so that one picks the lowest value instead (labeled accordingly)."""
    if df.empty:
        return {}
    grouped = df.groupby("pitcher").agg(
        whiffs=("whiffs", "sum"),
        strikeouts=("strikeouts", "sum"),
        walks=("walks", "sum"),
        max_velo=("max_velo", "max"),
        early_ahead=("early_ahead", "sum"),
        first_pitch_strikes=("first_pitch_strikes", "sum"),
        at_bats=("at_bats", "sum"),
    )
    out = {}
    out["Whiffs"] = (grouped["whiffs"].idxmax(), int(grouped["whiffs"].max()))
    out["Velo"] = (grouped["max_velo"].idxmax(), float(grouped["max_velo"].max()))
    rated = grouped[grouped["at_bats"] > 0].copy()
    if not rated.empty:
        rated["k_rate"] = rated["strikeouts"] / rated["at_bats"]
        rated["bb_rate"] = rated["walks"] / rated["at_bats"]
        rated["ea_pct"] = rated["early_ahead"] / rated["at_bats"]
        rated["fps_pct"] = rated["first_pitch_strikes"] / rated["at_bats"]
        out["K Rate"] = (rated["k_rate"].idxmax(), rated["k_rate"].max())
        out["BB Rate (lowest)"] = (rated["bb_rate"].idxmin(), rated["bb_rate"].min())
        out["Early/Ahead %"] = (rated["ea_pct"].idxmax(), rated["ea_pct"].max())
        out["FPS %"] = (rated["fps_pct"].idxmax(), rated["fps_pct"].max())
    return out


def _fmt_leader_value(cat, val):
    if cat == "Velo":
        return f"{val:.1f} mph"
    if cat in ("K Rate", "BB Rate (lowest)", "Early/Ahead %", "FPS %"):
        return f"{val:.1%}"
    return str(val)


LEADERBOARD_CATS = ["Whiffs", "K Rate", "BB Rate (lowest)", "Velo", "Early/Ahead %", "FPS %"]


def _render_leader_row(df):
    leaders = _leaderboard_categories(df)
    if not leaders:
        st.info("No games in this period.")
        return
    cols = st.columns(len(LEADERBOARD_CATS))
    for col, cat in zip(cols, LEADERBOARD_CATS):
        if cat in leaders:
            name, val = leaders[cat]
            col.metric(cat, name, _fmt_leader_value(cat, val))
        else:
            col.metric(cat, "—")


def render_leaderboard():
    st.header("🏆 Team Leaderboard")
    all_games = get_games()
    if all_games.empty:
        st.info("No game data yet.")
        return
    dates = pd.to_datetime(all_games["game_date"], errors="coerce")
    cutoff = pd.Timestamp.now().normalize() - pd.Timedelta(days=7)
    weekly_games = all_games[dates >= cutoff]

    st.subheader("This Week's Leaders")
    st.caption("Last 7 days")
    _render_leader_row(weekly_games)

    st.divider()
    st.subheader("Season Leaders")
    _render_leader_row(all_games)


st.set_page_config(page_title="SRU Pitching Database", page_icon="⚾", layout="wide")
init_db()

st.markdown(
    f"""
    <style>
    div[data-testid="stMetric"] {{
        background: #121815;
        border: 1px solid #24352C;
        border-left: 5px solid {SRU_GREEN};
        border-radius: 8px;
        padding: 12px 14px 6px 14px;
    }}
    div[data-testid="stMetricLabel"] {{
        color: #4FD8AC;
        font-weight: 600;
    }}
    div[data-testid="stMetricValue"] {{
        color: #F2F5F3;
    }}
    .rock-header {{
        background: linear-gradient(90deg, {SRU_GREEN} 0%, #012f24 100%);
        padding: 22px 28px;
        border-radius: 10px;
        margin-bottom: 18px;
    }}
    .rock-header h1 {{
        color: white;
        margin: 0;
        font-size: 2rem;
    }}
    .rock-header p {{
        color: #B9E5D7;
        margin: 4px 0 0 0;
    }}
    </style>
    <div class="rock-header">
        <h1>⚾ SRU Pitching Database</h1>
        <p>Manual game-chart entry • automatic season aggregation • pitcher development dashboard</p>
    </div>
    """,
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------------------
# Login gate: coach gets full access, a pitcher only ever sees their own data.
# ---------------------------------------------------------------------------
if "role" not in st.session_state:
    st.session_state.role = None
    st.session_state.pid = None
    st.session_state.name = None

if st.session_state.role is None:
    st.subheader("Log in")
    who = st.radio("I am a...", ["Pitcher", "Coach"], horizontal=True)
    if who == "Pitcher":
        ps = get_pitchers()
        if ps.empty:
            st.info("No pitchers have been added yet — ask your coach to add you first.")
        else:
            with st.form("pitcher_login"):
                name = st.selectbox("Your name", ps.name.tolist())
                pin = st.text_input("Your PIN", type="password")
                go = st.form_submit_button("Log in")
            if go:
                pid = check_pitcher_login(name, pin)
                if pid:
                    st.session_state.role = "pitcher"
                    st.session_state.pid = pid
                    st.session_state.name = name
                    st.rerun()
                else:
                    st.error("Name/PIN didn't match. Check with your coach.")
    else:
        with st.form("coach_login"):
            pw = st.text_input("Coach password", type="password")
            go = st.form_submit_button("Log in")
        if go:
            if pw == COACH_PASSWORD:
                st.session_state.role = "coach"
                st.rerun()
            else:
                st.error("Wrong password.")
    st.stop()

# ---------------------------------------------------------------------------
# Logged in
# ---------------------------------------------------------------------------
with st.sidebar:
    if st.session_state.role == "coach":
        st.caption("Logged in as **Coach**")
    else:
        st.caption(f"Logged in as **{st.session_state.name}**")
    if st.button("Log out"):
        st.session_state.role = None
        st.session_state.pid = None
        st.session_state.name = None
        st.rerun()

if st.session_state.role == "pitcher":
    pid, name = st.session_state.pid, st.session_state.name
    page = st.sidebar.radio("Navigate", ["My Stats", "My Videos", "My Goals", "My Workouts", "Leaderboard"])
    if page == "My Stats":
        render_dashboard(pid, name)
    elif page == "My Videos":
        render_pitcher_videos(pid, name)
    elif page == "My Goals":
        render_pitcher_goals(pid, name)
    elif page == "My Workouts":
        render_pitcher_workouts(pid, name)
    else:
        render_leaderboard()
    st.stop()

# --- everything below is coach-only ---
page = st.sidebar.radio(
    "Navigate",
    ["Dashboard", "Enter Game", "Add Pitcher", "Game Log", "Team Leaderboard", "Player Videos", "Player Goals", "Workouts"],
)

if page == "Add Pitcher":
    st.header("Add Pitcher")
    with st.form("add"):
        name = st.text_input("Pitcher name")
        throws = st.selectbox("Throws", ["R", "L"])
        year = st.selectbox("Class", ["Freshman", "Sophomore", "Junior", "Senior", "Other"])
        pin = st.text_input("PIN for this pitcher to log in (4 digits, e.g. 1234)")
        ok = st.form_submit_button("Add Pitcher")
    if ok:
        if not name.strip():
            st.error("Enter a name.")
        elif not pin.strip():
            st.error("Set a PIN so this pitcher can log in.")
        else:
            add_pitcher(name, throws, year, pin)
            st.success(f"Added {name}. Share their PIN with them so they can log in.")

    st.divider()
    st.subheader("Edit pitcher info")
    st.caption("Fix a typo'd name, class year, or PIN — edit a cell, then click Save Changes.")
    ps_edit = get_pitchers()
    if not ps_edit.empty:
        edit_cols = ["name", "throws", "class_year", "pin"]
        original_p = ps_edit[["id"] + edit_cols].copy()
        edited_p = st.data_editor(
            original_p, disabled=["id"], use_container_width=True, hide_index=True, key="pitcher_editor"
        )
        if st.button("Save Changes", key="save_pitcher_edits"):
            changed = 0
            for i in range(len(original_p)):
                orig_row = original_p.iloc[i]
                new_row = edited_p.iloc[i]
                if not orig_row[edit_cols].equals(new_row[edit_cols]):
                    update_pitcher(
                        int(orig_row.id), new_row["name"], new_row["throws"],
                        new_row["class_year"], new_row["pin"],
                    )
                    changed += 1
            st.success(f"Saved changes to {changed} pitcher(s).")
            st.rerun()

    st.divider()
    st.subheader("Remove a pitcher")
    st.caption("This deletes the pitcher and everything tied to them — games, goals, videos. Can't be undone.")
    ps_del = get_pitchers()
    if not ps_del.empty:
        del_name = st.selectbox("Pitcher to remove", ps_del.name.tolist(), key="del_pitcher_select")
        confirm = st.checkbox(f"I understand this permanently deletes {del_name} and all their data")
        if st.button("Delete pitcher", key="del_pitcher_btn"):
            if confirm:
                del_pid = int(ps_del.loc[ps_del.name == del_name, "id"].iloc[0])
                delete_pitcher(del_pid)
                st.success(f"Deleted {del_name}.")
                st.rerun()
            else:
                st.error("Check the confirmation box first.")

elif page == "Enter Game":
    st.header("Enter Game")
    ps = get_pitchers()
    if ps.empty:
        st.warning("Add your pitchers first.")
    else:
        with st.form("game"):
            name = st.selectbox("Pitcher", ps.name.tolist())
            pid = int(ps.loc[ps.name == name, "id"].iloc[0])
            gd = st.date_input("Game date", date.today())
            opp = st.text_input("Opponent")
            st.markdown("### Workload")
            a, b, c, d, e = st.columns(5)
            full_ip = a.number_input("Full innings", 0, 15, 1, step=1)
            partial_outs = b.selectbox("+ outs (0, 1, or 2)", [0, 1, 2])
            pitches = c.number_input("Total pitches", 0, 200, 20)
            balls = d.number_input("Balls", 0, 200, 8)
            strikes = e.number_input("Strikes", 0, 200, 12)
            st.markdown("### Results")
            a, b, c, d, e, f = st.columns(6)
            at_bats = a.number_input("At bats faced", 0, 60, 20)
            whiffs = b.number_input("Whiffs", 0, 200, 3)
            ks = c.number_input("Strikeouts", 0, 30, 1)
            walks = d.number_input("Walks", 0, 30, 1)
            hits = e.number_input("Hits", 0, 30, 2)
            hrs = f.number_input("Home runs", 0, 15, 0)
            er = st.number_input("Earned runs", 0, 30, 0)
            st.markdown("### Command / count data")
            st.caption("Both of these are shown as a % of at-bats faced, not total pitches.")
            a, b = st.columns(2)
            fps = a.number_input("First-pitch strikes", 0, 60, 5)
            early_ahead = b.number_input("Early/Ahead (combined)", 0, 200, 17)
            st.markdown("### Velocity")
            a, b = st.columns(2)
            avg = a.number_input("Average velocity", 0.0, 110.0, 90.0, step=0.1)
            mx = b.number_input("Max velocity", 0.0, 110.0, 94.0, step=0.1)
            save = st.form_submit_button("Save Game")
        if save:
            total_outs = full_ip * 3 + partial_outs
            innings_display = full_ip + partial_outs / 10.0
            add_game(
                (str(gd), opp, pid, innings_display, total_outs, pitches, balls, strikes, whiffs, ks, walks, hits, hrs, fps, early_ahead, at_bats, avg, mx, er)
            )
            st.success(f"Game saved ({full_ip}.{partial_outs} IP) and included in season totals.")

elif page == "Dashboard":
    ps = get_pitchers()
    if ps.empty:
        st.info("Add pitchers to get started.")
    else:
        name = st.selectbox("Select pitcher", ps.name.tolist())
        pid = int(ps.loc[ps.name == name, "id"].iloc[0])
        render_dashboard(pid, name, allow_delete=True)

elif page == "Game Log":
    st.header("All Game Data")
    st.caption("Edit any cell directly, then click Save Changes. Editing 'innings' automatically recalculates the underlying out count.")
    g = get_games()
    if g.empty:
        st.info("No games yet.")
    else:
        edit_cols = [
            "game_date", "opponent", "pitcher", "innings", "pitches", "at_bats", "balls", "strikes",
            "whiffs", "strikeouts", "walks", "hits", "home_runs", "first_pitch_strikes",
            "early_ahead", "avg_velo", "max_velo", "earned_runs",
        ]
        original = g[["id"] + edit_cols].copy()
        edited = st.data_editor(
            original,
            disabled=["id", "pitcher"],
            use_container_width=True,
            hide_index=True,
            key="game_log_editor",
        )
        if st.button("Save Changes"):
            changed = 0
            for i in range(len(original)):
                orig_row = original.iloc[i]
                new_row = edited.iloc[i]
                cols_to_check = [c for c in edit_cols if c != "pitcher"]
                if not orig_row[cols_to_check].equals(new_row[cols_to_check]):
                    fields = {c: new_row[c] for c in cols_to_check}
                    try:
                        innings_val = float(fields["innings"])
                        full = int(innings_val)
                        rem = round((innings_val - full) * 10)
                        fields["outs"] = full * 3 + rem
                    except (ValueError, TypeError):
                        st.error(f"Row {i+1}: innings must be like 5.0, 5.1, or 5.2 — skipped.")
                        continue
                    update_game(int(orig_row.id), fields)
                    changed += 1
            st.success(f"Saved changes to {changed} game(s).")
            st.rerun()

elif page == "Player Videos":
    render_coach_videos()

elif page == "Player Goals":
    render_coach_goals()

elif page == "Workouts":
    render_coach_workouts()

else:
    render_leaderboard()
