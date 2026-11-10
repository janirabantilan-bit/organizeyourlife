import calendar
import json
import os
from datetime import datetime, timedelta
from flask import Flask, jsonify, redirect, render_template, request, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_bcrypt import Bcrypt

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key-change-this-in-production'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///scheduler.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
bcrypt = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# ---------------------------------------------------------------------------
# DATABASE MODELS
# ---------------------------------------------------------------------------
class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    schedules = db.relationship('Schedule', backref='user', lazy=True)
    finances = db.relationship('FinanceData', backref='user', uselist=False, lazy=True)

class Schedule(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    series_id = db.Column(db.Integer, nullable=True)
    date = db.Column(db.String(10), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    start_time = db.Column(db.String(5), nullable=False)
    end_time = db.Column(db.String(5), nullable=False)
    done = db.Column(db.Boolean, default=False)
    recurrence = db.Column(db.String(20), default='none')

    def to_dict(self):
        return {
            "id": self.id,
            "series_id": self.series_id,
            "date": self.date,
            "title": self.title,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "done": self.done,
            "recurrence": self.recurrence
        }

class FinanceData(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    data_json = db.Column(db.Text, default='{"incomes":[],"tabs":{},"unassigned":[],"savings":[],"quote":""}')

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

@app.template_filter("format_time")
def format_time_filter(time_str):
    if not time_str:
        return ""
    if time_str == "24:00":
        return "12:00 AM"
    try:
        h, m = map(int, time_str.split(":"))
        period = "PM" if h >= 12 else "AM"
        h = h % 12
        h = h if h else 12
        return f"{h} {period}" if m == 0 else f"{h}:{m:02d} {period}"
    except (ValueError, AttributeError):
        return time_str

# ---------------------------------------------------------------------------
# AUTHENTICATION ROUTES
# ---------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username").strip()
        password = request.form.get("password")
        user = User.query.filter_by(username=username).first()
        if user and bcrypt.check_password_hash(user.password_hash, password):
            login_user(user)
            return redirect(url_for("index"))
        flash("Invalid username or password")
    return render_template("login.html")

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username").strip()
        password = request.form.get("password")
        if User.query.filter_by(username=username).first():
            flash("Username already exists")
            return redirect(url_for("register"))
        hashed_pw = bcrypt.generate_password_hash(password).decode("utf-8")
        new_user = User(username=username, password_hash=hashed_pw)
        db.session.add(new_user)
        db.session.commit()
        login_user(new_user)
        return redirect(url_for("index"))
    return render_template("register.html")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))

# ---------------------------------------------------------------------------
# SCHEDULE ROUTES
# ---------------------------------------------------------------------------
def get_selected_date():
    selected = request.args.get("date", datetime.today().strftime("%Y-%m-%d"))
    try:
        datetime.strptime(selected, "%Y-%m-%d")
    except ValueError:
        selected = datetime.today().strftime("%Y-%m-%d")
    return selected

def build_recurrence_dates(start_date, recurrence):
    rules = {
        "daily": (90, lambda d: True),
        "week": (6, lambda d: True),
        "month": (29, lambda d: True),
        "tue-sat": (90, lambda d: 1 <= d.weekday() <= 5),
        "weekends": (90, lambda d: d.weekday() >= 5),
    }
    if recurrence not in rules:
        return [start_date.strftime("%Y-%m-%d")]
    days_ahead, allowed = rules[recurrence]
    return [(start_date + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days_ahead + 1) if allowed(start_date + timedelta(days=i))]

@app.route("/", methods=["GET", "POST"])
@login_required
def index():
    selected_date = get_selected_date()
    view_mode = request.args.get("view", "daily")

    if request.method == "POST":
        title = request.form.get("title")
        start_time = request.form.get("start_time", "09:00")
        end_time = request.form.get("end_time", "10:00")
        recurrence = request.form.get("recurrence", "none")

        if title:
            start_date = datetime.strptime(selected_date, "%Y-%m-%d")
            series_id = None
            if recurrence != "none":
                max_series = db.session.query(db.func.max(Schedule.series_id)).scalar() or 0
                series_id = max_series + 1

            for d in build_recurrence_dates(start_date, recurrence):
                sch = Schedule(
                    user_id=current_user.id,
                    series_id=series_id,
                    date=d,
                    title=title,
                    start_time=start_time,
                    end_time=end_time,
                    done=False,
                    recurrence=recurrence
                )
                db.session.add(sch)
            db.session.commit()

        return redirect(url_for("index", date=selected_date, view=view_mode))

    all_user_schedules = [s.to_dict() for s in Schedule.query.filter_by(user_id=current_user.id).all()]
    today_str = datetime.today().strftime("%Y-%m-%d")
    sel_dt = datetime.strptime(selected_date, "%Y-%m-%d")

    def day_items_db(date_str):
        prev_date_str = (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        items = []
        for s in all_user_schedules:
            is_overnight = s["start_time"] > s["end_time"]
            if s["date"] == date_str:
                item = s.copy()
                if is_overnight: item["end_time_split"] = "24:00"
                items.append(item)
            elif s["date"] == prev_date_str and is_overnight:
                item = s.copy()
                item["start_time_split"] = "00:00"
                items.append(item)
        return sorted(items, key=lambda x: x.get("start_time_split") or x["start_time"])

    def with_layout(items):
        for item in items:
            start = int(item.get("start_time_split", item["start_time"]).split(":")[0]) * 60 + int(item.get("start_time_split", item["start_time"]).split(":")[1])
            end = int(item.get("end_time_split", item["end_time"]).split(":")[0]) * 60 + int(item.get("end_time_split", item["end_time"]).split(":")[1])
            duration = end - start if end - start > 0 else 15
            item["start_mins"] = start
            item["end_mins"] = end
            item["duration"] = duration
        return items

    active_schedules, week_days, month_weeks = [], [], []
    if view_mode == "daily":
        active_schedules = day_items_db(selected_date)
    elif view_mode == "weekly":
        start_of_week = sel_dt - timedelta(days=(sel_dt.weekday() + 1) % 7)
        for i in range(7):
            day = start_of_week + timedelta(days=i)
            ds = day.strftime("%Y-%m-%d")
            week_days.append({"date": ds, "label": day.strftime("%A"), "day_num": day.day, "is_today": ds == today_str, "items": with_layout(day_items_db(ds))})
    elif view_mode == "monthly":
        cal = calendar.Calendar(firstweekday=6)
        for week in cal.monthdatescalendar(sel_dt.year, sel_dt.month):
            row = []
            for day in week:
                ds = day.strftime("%Y-%m-%d")
                row.append({"date": ds, "day_num": day.day, "in_month": day.month == sel_dt.month, "is_today": ds == today_str, "items": with_layout(day_items_db(ds))})
            month_weeks.append(row)

    return render_template("index.html", schedules=active_schedules, all_schedules=all_user_schedules, week_days=week_days, month_weeks=month_weeks, month_label=sel_dt.strftime("%B %Y"), week_px_per_min=0.6, selected_date=selected_date, today=today_str, view_mode=view_mode)

@app.route("/update/<int:id>", methods=["POST"])
@login_required
def update_schedule(id):
    task = Schedule.query.filter_by(id=id, user_id=current_user.id).first()
    if task:
        if "title" in request.form:
            scope = request.form.get("scope", "one")
            targets = Schedule.query.filter_by(series_id=task.series_id, user_id=current_user.id).all() if (scope == "all" and task.series_id) else [task]
            new_title = request.form.get("title", "").strip() or task.title
            new_start = request.form.get("start_time", task.start_time)
            new_end = request.form.get("end_time", task.end_time)

            for item in targets:
                item.title = new_title
                item.start_time = new_start
                item.end_time = new_end
            task.done = (request.form.get("done") == "on")
        else:
            if "start_time" in request.form: task.start_time = request.form["start_time"]
            if "end_time" in request.form: task.end_time = request.form["end_time"]
        db.session.commit()
    return redirect(url_for("index", date=get_selected_date(), view=request.args.get("view", "daily")))

@app.route("/delete/<int:id>", methods=["POST"])
@login_required
def delete_schedule(id):
    task = Schedule.query.filter_by(id=id, user_id=current_user.id).first()
    if task:
        if request.form.get("scope") == "all" and task.series_id:
            Schedule.query.filter_by(series_id=task.series_id, user_id=current_user.id).delete()
        else:
            db.session.delete(task)
        db.session.commit()
    return redirect(url_for("index", date=get_selected_date(), view=request.args.get("view", "daily")))

# ---------------------------------------------------------------------------
# FINANCE SYNC API
# ---------------------------------------------------------------------------
@app.route("/api/finance", methods=["GET", "POST"])
@login_required
def api_finance():
    fin = FinanceData.query.filter_by(user_id=current_user.id).first()
    if not fin:
        fin = FinanceData(user_id=current_user.id)
        db.session.add(fin)
        db.session.commit()

    if request.method == "POST":
        payload = request.get_json(silent=True)
        if payload is not None:
            import json
            fin.data_json = json.dumps(payload)
            db.session.commit()
            return jsonify(ok=True)
        return jsonify(ok=False), 400

    import json
    return jsonify(json.loads(fin.data_json))

SECTIONS = {"tasks": "Tasks", "goals": "Goals", "finance": "Finance", "dreams": "Dreams"}

@app.route("/<any(tasks, goals, finance, dreams):name>")
@login_required
def section(name):
    return render_template("section.html", active_page=name, page_title=SECTIONS[name])

# ---------------------------------------------------------------------------
# 365 DAY CHALLENGE (Physical Transformation)
# ---------------------------------------------------------------------------
CHALLENGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "challenge_data.json")
CHALLENGE_TOTAL_DAYS = 365
MIN_TO_ADVANCE = 10

CHALLENGE_ITEMS = [
    {"key": "core", "label": "Push ups / Curl ups / Sit ups", "unit": "reps", "core": True, "chips": [5, 10, 25]},
    {"key": "duolingo", "label": "Duolingo", "unit": "", "core": False, "chips": []},
    {"key": "toothbrush", "label": "Toothbrush", "unit": "", "core": False, "chips": []},
    {"key": "vitamins", "label": "Vitamins", "unit": "", "core": False, "chips": []},
    {"key": "bath", "label": "Bath", "unit": "", "core": False, "chips": []},
    {"key": "skincare", "label": "Skin care (after bath care)", "unit": "", "core": False, "chips": []},
    {"key": "bike", "label": "Stationary bike", "unit": "min", "chips": [5, 10, 15], "core": False},
]

CHALLENGE_STAGES = [
    (1, 4, 100, 0, 0, 0, 0, 0, 0),
    (5, 9, 150, 0, 0, 0, 0, 0, 0),
    (10, 14, 150, 1, 0, 0, 0, 0, 0),
    (15, 19, 150, 2, 0, 0, 0, 0, 0),
    (20, 24, 150, 2, 1, 0, 0, 0, 0),
    (25, 29, 150, 2, 1, 1, 0, 0, 0),
    (30, 34, 200, 2, 1, 1, 0, 0, 0),
    (35, 39, 200, 3, 1, 1, 0, 0, 0),
    (40, 44, 200, 3, 1, 1, 1, 0, 0),
    (45, 49, 200, 3, 1, 2, 1, 0, 0),
    (50, 54, 250, 3, 1, 2, 1, 0, 0),
    (55, 59, 250, 4, 1, 2, 1, 0, 0),
    (60, 64, 250, 4, 2, 2, 1, 0, 0),
    (65, 69, 300, 4, 2, 2, 1, 0, 0),
    (70, 74, 300, 5, 2, 2, 1, 0, 0),
    (75, 79, 350, 5, 2, 2, 1, 0, 0),
    (80, 84, 350, 5, 2, 2, 1, 1, 0),
    (85, 90, 350, 6, 2, 2, 1, 1, 0),
    (91, 94, 400, 6, 2, 2, 1, 1, 0),
    (95, 99, 400, 7, 2, 2, 1, 1, 0),
    (100, 101, 400, 7, 2, 2, 1, 1, 30),
]

def build_challenge_targets():
    keys = [it["key"] for it in CHALLENGE_ITEMS]
    targets = []
    for day in range(1, CHALLENGE_TOTAL_DAYS + 1):
        row = CHALLENGE_STAGES[-1]
        for stage in CHALLENGE_STAGES:
            if stage[0] <= day <= stage[1]:
                row = stage
                break
        targets.append(dict(zip(keys, row[2:])))
    return targets

def load_challenge_data():
    try:
        with open(CHALLENGE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data.setdefault("logs", {})
    data.setdefault("paused", {})
    return data

def save_challenge_data(data):
    tmp = CHALLENGE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, CHALLENGE_FILE)

@app.route("/challenge")
@login_required
def challenge():
    data = load_challenge_data()
    return render_template(
        "challenge.html",
        active_page="challenge",
        items=CHALLENGE_ITEMS,
        targets=build_challenge_targets(),
        logs=data["logs"],
        paused=data["paused"],
        min_to_advance=MIN_TO_ADVANCE,
    )

@app.route("/challenge/log", methods=["POST"])
@login_required
def challenge_log():
    payload = request.get_json(silent=True) or {}
    item, day, value = payload.get("item"), payload.get("day"), payload.get("value")
    valid_keys = {it["key"] for it in CHALLENGE_ITEMS}
    if (
        item not in valid_keys
        or not isinstance(day, int) or not 1 <= day <= CHALLENGE_TOTAL_DAYS
        or not isinstance(value, int) or not 0 <= value <= 1000000
    ):
        return jsonify(ok=False, error="bad input"), 400

    data = load_challenge_data()
    item_logs = data["logs"].setdefault(item, {})
    if value > 0:
        item_logs[str(day)] = value
    else:
        item_logs.pop(str(day), None)
    save_challenge_data(data)
    return jsonify(ok=True)

@app.route("/challenge/pause", methods=["POST"])
@login_required
def challenge_pause():
    payload = request.get_json(silent=True) or {}
    day, is_paused = payload.get("day"), payload.get("paused")
    if not isinstance(day, int) or not 1 <= day <= CHALLENGE_TOTAL_DAYS:
        return jsonify(ok=False, error="bad input"), 400

    data = load_challenge_data()
    if is_paused:
        data["paused"][str(day)] = datetime.now().isoformat(timespec="seconds")
    else:
        data["paused"].pop(str(day), None)
    save_challenge_data(data)
    return jsonify(ok=True)

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(host='0.0.0.0', port=5000, debug=True)