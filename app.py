import calendar
import json
import os
from datetime import datetime, timedelta
from flask import Flask, Response, jsonify, redirect, render_template, request, url_for, flash, stream_with_context
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_bcrypt import Bcrypt

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key-change-this-in-production'
database_url = os.environ.get('DATABASE_URL', 'sqlite:///scheduler.db')
if database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
# Backup JSON files can contain embedded photos, so allow larger uploads while
# keeping a firm cap on the total request size. Dream videos are stored separately.
app.config['MAX_CONTENT_LENGTH'] = 64 * 1024 * 1024

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

class UserStore(db.Model):
    """Small per-user storage (used by the Goals page)."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    key = db.Column(db.String(40), nullable=False)
    data_json = db.Column(db.Text, nullable=False, default='{}')

class DreamMedia(db.Model):
    id = db.Column(db.String(48), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    kind = db.Column(db.String(10), nullable=False)
    mime_type = db.Column(db.String(40), nullable=False)
    size = db.Column(db.Integer, nullable=False)
    complete = db.Column(db.Boolean, nullable=False, default=False)
    chunks = db.relationship('DreamMediaChunk', backref='media', cascade='all, delete-orphan', order_by='DreamMediaChunk.part_index')

class DreamMediaChunk(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    media_id = db.Column(db.String(48), db.ForeignKey('dream_media.id'), nullable=False, index=True)
    part_index = db.Column(db.Integer, nullable=False)
    data = db.Column(db.LargeBinary, nullable=False)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# Create any missing tables (like UserStore) when the app starts, including on Render.
with app.app_context():
    db.create_all()

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
    is_new = fin is None
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
    data = json.loads(fin.data_json)
    data['_is_empty'] = is_new or not any([
        data.get('incomes'), data.get('tabs'), data.get('unassigned'), data.get('savings'),
        data.get('quote'), data.get('beerMoney'),
        any((wallet.get('amount', 0) or 0) for wallet in data.get('wallets', []) if isinstance(wallet, dict))
    ])
    return jsonify(data)

SECTIONS = {"tasks": "Tasks", "goals": "Goals", "finance": "Finance", "dreams": "Dreams"}

@app.route("/<any(tasks, goals, finance, dreams):name>")
@login_required
def section(name):
    if name == "goals":
        return render_template("goals.html", active_page="goals", initial=load_goals())
    if name == "dreams":
        return render_template("section.html", active_page=name, page_title=SECTIONS[name], initial=load_dreams())
    if name == "tasks":
        return render_template("section.html", active_page=name, page_title=SECTIONS[name])
    return render_template("section.html", active_page=name, page_title=SECTIONS[name])

def clean_tasks(payload):
    cleaned, seen = [], set()
    for task in (payload.get("tasks") or [])[:1000]:
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("id", ""))[:40]
        name = str(task.get("name", "")).strip()[:200]
        try:
            hours = float(task.get("hours", 0))
            x = max(0, min(10000, int(task.get("x", 24))))
            y = max(0, min(10000, int(task.get("y", 24))))
        except (TypeError, ValueError, OverflowError):
            continue
        if not task_id or task_id in seen or not name or not 0 < hours <= 10000:
            continue
        seen.add(task_id)
        number = len(cleaned) + 1
        cleaned.append({
            "id": task_id, "number": number, "name": name, "hours": hours,
            "points": round(hours * 10, 1), "levelGain": round(hours * 0.1, 2),
            "done": bool(task.get("done")), "x": x, "y": y,
        })
    return {"tasks": cleaned}

@app.route("/api/tasks", methods=["GET", "POST"])
@login_required
def api_tasks():
    row = UserStore.query.filter_by(user_id=current_user.id, key="tasks").first()
    if request.method == "GET":
        if row:
            try:
                return jsonify(json.loads(row.data_json))
            except json.JSONDecodeError:
                pass
        return jsonify(tasks=[], is_new=True)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(ok=False, error="bad input"), 400
    data_json = json.dumps(clean_tasks(payload))
    if row:
        row.data_json = data_json
    else:
        db.session.add(UserStore(user_id=current_user.id, key="tasks", data_json=data_json))
    db.session.commit()
    return jsonify(ok=True)

# ---------------------------------------------------------------------------
# GOALS (saved per user)
# ---------------------------------------------------------------------------
def load_goals():
    row = UserStore.query.filter_by(user_id=current_user.id, key="goals").first()
    if row:
        try:
            data = json.loads(row.data_json)
            if isinstance(data, dict) and isinstance(data.get("goals"), list):
                return data
        except json.JSONDecodeError:
            pass
    return {"goals": []}

def clean_goals(payload):
    """Keep only well-formed goals from what the browser sends."""
    goals, seen = [], set()
    for g in (payload.get("goals") or [])[:500]:
        if not isinstance(g, dict):
            continue
        goal_id = str(g.get("id", ""))[:40]
        text = str(g.get("text", "")).strip()[:200]
        date = str(g.get("date", ""))[:10]
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            continue
        if not goal_id or goal_id in seen or not text:
            continue
        seen.add(goal_id)
        try:
            created = int(g.get("created", 0))
        except (TypeError, ValueError):
            created = 0
        goals.append({
            "id": goal_id,
            "text": text,
            "date": date,
            "benefit": str(g.get("benefit", "")).strip()[:400],
            "done": bool(g.get("done")),
            "doneAt": str(g.get("doneAt") or "")[:10],
            "created": created,
        })
    return {"goals": goals}

@app.route("/api/goals", methods=["POST"])
@login_required
def api_goals():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(ok=False, error="bad input"), 400
    data_json = json.dumps(clean_goals(payload))
    row = UserStore.query.filter_by(user_id=current_user.id, key="goals").first()
    if row:
        row.data_json = data_json
    else:
        db.session.add(UserStore(user_id=current_user.id, key="goals", data_json=data_json))
    db.session.commit()
    return jsonify(ok=True)

def load_dreams():
    row = UserStore.query.filter_by(user_id=current_user.id, key="dreams").first()
    if row:
        try:
            data = json.loads(row.data_json)
            if isinstance(data, dict) and isinstance(data.get("dreams"), list):
                return data
        except json.JSONDecodeError:
            pass
    return {"dreams": []}

def clean_dreams(payload, user_id):
    dreams, seen = [], set()
    for dream in (payload.get("dreams") or [])[:500]:
        if not isinstance(dream, dict):
            continue
        dream_id = str(dream.get("id", ""))[:40]
        description = str(dream.get("description", "")).strip()[:1000]
        quote = str(dream.get("quote", "")).strip()[:500]
        media = dream.get("media")
        if media is None:
            media = [{"kind": "image", "src": photo} for photo in dream.get("photos", []) if isinstance(photo, str)]
        if not dream_id or dream_id in seen or not description or not isinstance(media, list):
            continue
        safe_media = []
        video_count = 0
        for item in media[:4]:
            if not isinstance(item, dict):
                continue
            src = item.get("src", "")
            kind = "video" if item.get("kind") == "video" else "image"
            if kind == "video":
                video_count += 1
                if video_count > 2:
                    continue
            media_id = str(item.get("mediaId", ""))[:48]
            if media_id:
                uploaded = DreamMedia.query.filter_by(id=media_id, user_id=user_id, complete=True).first()
                if uploaded and uploaded.kind == kind and src == url_for("dream_media", media_id=media_id):
                    safe_media.append({"kind": kind, "src": src, "mediaId": media_id})
                continue
            allowed = ("data:video/mp4;base64,", "data:video/webm;base64,", "data:video/quicktime;base64,") if kind == "video" else ("data:image/jpeg;base64,", "data:image/png;base64,", "data:image/webp;base64,")
            max_size = 18_000_000 if kind == "video" else 1_400_000
            if isinstance(src, str) and len(src) <= max_size and src.startswith(allowed):
                safe_media.append({"kind": kind, "src": src})
        seen.add(dream_id)
        dreams.append({"id": dream_id, "description": description, "quote": quote, "media": safe_media})
    return {"dreams": dreams}

@app.route("/api/dreams", methods=["GET", "POST"])
@login_required
def api_dreams():
    if request.method == "GET":
        return jsonify(load_dreams())
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(ok=False, error="bad input"), 400
    cleaned = clean_dreams(payload, current_user.id)
    data_json = json.dumps(cleaned)
    if len(data_json) > 30_000_000:
        return jsonify(ok=False, error="Dream media is too large. Try a shorter or smaller video."), 413
    row = UserStore.query.filter_by(user_id=current_user.id, key="dreams").first()
    if row:
        row.data_json = data_json
    else:
        db.session.add(UserStore(user_id=current_user.id, key="dreams", data_json=data_json))
    referenced = {m["mediaId"] for d in cleaned["dreams"] for m in d["media"] if m.get("mediaId")}
    for uploaded in DreamMedia.query.filter_by(user_id=current_user.id).all():
        if uploaded.id not in referenced:
            upload_id = get_multipart_upload_id(uploaded.id)
            client = r2_client()
            if upload_id and client:
                if uploaded.complete:
                    client.delete_object(Bucket=r2_bucket_name(), Key=dream_media_key(uploaded))
                else:
                    client.abort_multipart_upload(Bucket=r2_bucket_name(), Key=dream_media_key(uploaded), UploadId=upload_id)
            db.session.delete(uploaded)
    db.session.commit()
    return jsonify(ok=True)

DREAM_UPLOAD_CHUNK_SIZE = 8 * 1024 * 1024
DREAM_VIDEO_MAX_SIZE = 100 * 1024 * 1024
DREAM_MEDIA_TYPES = {
    "video/mp4": "video", "video/webm": "video", "video/quicktime": "video",
    "image/jpeg": "image", "image/png": "image", "image/webp": "image",
}

def r2_client():
    required = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME")
    if not all(os.environ.get(name) for name in required):
        return None
    import boto3
    return boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )

def dream_media_key(media):
    return f"dreams/{media.user_id}/{media.id}"

def get_multipart_upload_id(media_id):
    marker = DreamMediaChunk.query.filter_by(media_id=media_id, part_index=-1).first()
    return marker.data.decode("utf-8") if marker else None

def r2_bucket_name():
    return os.environ.get("R2_BUCKET_NAME", "")

@app.route("/api/dream-media/start", methods=["POST"])
@login_required
def dream_media_start():
    payload = request.get_json(silent=True) or {}
    mime_type = str(payload.get("mimeType", ""))
    kind = DREAM_MEDIA_TYPES.get(mime_type)
    try:
        size = int(payload.get("size", 0))
    except (TypeError, ValueError):
        size = 0
    max_size = DREAM_VIDEO_MAX_SIZE if kind == "video" else 20 * 1024 * 1024
    if not kind or size < 1 or size > max_size:
        return jsonify(ok=False, error="Video must be 100 MB or smaller, and use MP4, WebM, or MOV format."), 400
    client = r2_client()
    if not client:
        return jsonify(ok=False, error="Video storage is not configured yet. Add the four R2 settings in Render first."), 503
    import secrets
    media_id = secrets.token_urlsafe(24)
    media = DreamMedia(id=media_id, user_id=current_user.id, kind=kind, mime_type=mime_type, size=size, complete=False)
    upload = client.create_multipart_upload(Bucket=r2_bucket_name(), Key=dream_media_key(media), ContentType=mime_type)
    db.session.add(media)
    db.session.commit()
    db.session.add(DreamMediaChunk(media_id=media_id, part_index=-1, data=upload["UploadId"].encode("utf-8")))
    db.session.commit()
    return jsonify(ok=True, mediaId=media_id, chunkSize=DREAM_UPLOAD_CHUNK_SIZE)

@app.route("/api/dream-media/<media_id>/chunk/<int:part_index>", methods=["POST"])
@login_required
def dream_media_chunk(media_id, part_index):
    media = DreamMedia.query.filter_by(id=media_id, user_id=current_user.id, complete=False).first_or_404()
    data = request.get_data(cache=False)
    if not data or len(data) > DREAM_UPLOAD_CHUNK_SIZE:
        return jsonify(ok=False, error="Invalid upload chunk."), 400
    upload_id = get_multipart_upload_id(media_id)
    if not upload_id:
        return jsonify(ok=False, error="This video upload is no longer available. Start it again."), 400
    expected_index = DreamMediaChunk.query.filter(DreamMediaChunk.media_id == media_id, DreamMediaChunk.part_index >= 0).count()
    if part_index != expected_index or expected_index * DREAM_UPLOAD_CHUNK_SIZE + len(data) > media.size:
        return jsonify(ok=False, error="Upload chunk is out of order or too large."), 400
    expected_length = min(DREAM_UPLOAD_CHUNK_SIZE, media.size - expected_index * DREAM_UPLOAD_CHUNK_SIZE)
    if len(data) != expected_length:
        return jsonify(ok=False, error="The video chunk size is invalid."), 400
    result = r2_client().upload_part(Bucket=r2_bucket_name(), Key=dream_media_key(media), UploadId=upload_id, PartNumber=part_index + 1, Body=data)
    part_data = json.dumps({"etag": result["ETag"], "size": len(data)}).encode("utf-8")
    db.session.add(DreamMediaChunk(media_id=media_id, part_index=part_index, data=part_data))
    db.session.commit()
    return jsonify(ok=True, received=len(data))

@app.route("/api/dream-media/<media_id>/finish", methods=["POST"])
@login_required
def dream_media_finish(media_id):
    media = DreamMedia.query.filter_by(id=media_id, user_id=current_user.id, complete=False).first_or_404()
    upload_id = get_multipart_upload_id(media_id)
    chunks = DreamMediaChunk.query.filter(DreamMediaChunk.media_id == media_id, DreamMediaChunk.part_index >= 0).order_by(DreamMediaChunk.part_index).all()
    parts = [json.loads(chunk.data.decode("utf-8")) for chunk in chunks]
    if sum(part["size"] for part in parts) != media.size or not parts or not upload_id:
        return jsonify(ok=False, error="The video upload is incomplete. Please try again."), 400
    client = r2_client()
    client.complete_multipart_upload(
        Bucket=r2_bucket_name(), Key=dream_media_key(media), UploadId=upload_id,
        MultipartUpload={"Parts": [{"ETag": part["etag"], "PartNumber": index + 1} for index, part in enumerate(parts)]},
    )
    for chunk in chunks:
        db.session.delete(chunk)
    media.complete = True
    db.session.commit()
    return jsonify(ok=True, mediaId=media_id, src=url_for("dream_media", media_id=media_id))

@app.route("/api/dream-media/<media_id>/abort", methods=["POST"])
@login_required
def dream_media_abort(media_id):
    media = DreamMedia.query.filter_by(id=media_id, user_id=current_user.id, complete=False).first()
    if media:
        upload_id = get_multipart_upload_id(media_id)
        client = r2_client()
        if upload_id and client:
            client.abort_multipart_upload(Bucket=r2_bucket_name(), Key=dream_media_key(media), UploadId=upload_id)
        db.session.delete(media)
        db.session.commit()
    return jsonify(ok=True)

@app.route("/api/dream-media/<media_id>")
@login_required
def dream_media(media_id):
    media = DreamMedia.query.filter_by(id=media_id, user_id=current_user.id, complete=True).first_or_404()
    if get_multipart_upload_id(media_id):
        client = r2_client()
        if not client:
            return jsonify(ok=False, error="Video storage is not configured."), 503
        signed_url = client.generate_presigned_url(
            "get_object", Params={"Bucket": r2_bucket_name(), "Key": dream_media_key(media)}, ExpiresIn=3600
        )
        return redirect(signed_url, code=302)
    range_header = request.headers.get("Range", "")
    if range_header.startswith("bytes=") and "," not in range_header:
        try:
            start_text, end_text = range_header[6:].split("-", 1)
            start = int(start_text) if start_text else max(0, media.size - int(end_text))
            end = int(end_text) if end_text and start_text else media.size - 1
            if start > end or start >= media.size:
                return Response(status=416, headers={"Content-Range": f"bytes */{media.size}"})
            end = min(end, media.size - 1)
            status = 206
        except ValueError:
            start, end, status = 0, media.size - 1, 200
    else:
        start, end, status = 0, media.size - 1, 200
    first_part, last_part = start // DREAM_UPLOAD_CHUNK_SIZE, end // DREAM_UPLOAD_CHUNK_SIZE
    def stream_chunks():
        for index in range(first_part, last_part + 1):
            chunk = DreamMediaChunk.query.filter_by(media_id=media_id, part_index=index).first()
            if not chunk:
                continue
            offset = index * DREAM_UPLOAD_CHUNK_SIZE
            left, right = max(0, start - offset), min(len(chunk.data), end - offset + 1)
            yield chunk.data[left:right]
    headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1), "Cache-Control": "private, max-age=3600"}
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{media.size}"
    return Response(stream_with_context(stream_chunks()), status=status, mimetype=media.mime_type, headers=headers)

# Spreadsheet-style Cash Flow page (reads the same finance data as the Finance tab)
@app.route("/cashflow")
@login_required
def cashflow():
    return render_template("cashflow.html", active_page="finance")

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
    row = UserStore.query.filter_by(user_id=current_user.id, key="challenge").first()
    if row:
        try:
            data = json.loads(row.data_json)
        except json.JSONDecodeError:
            data = {}
        data.setdefault("logs", {})
        data.setdefault("paused", {})
        return data

    # Read the old shared JSON file as a compatibility fallback. New saves go
    # into the signed-in user's database record so they persist across devices.
    if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite:"):
        try:
            with open(CHALLENGE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
    else:
        data = {}
    data.setdefault("logs", {})
    data.setdefault("paused", {})
    return data

def save_challenge_data(data):
    row = UserStore.query.filter_by(user_id=current_user.id, key="challenge").first()
    data_json = json.dumps({"logs": data.get("logs", {}), "paused": data.get("paused", {})})
    if row:
        row.data_json = data_json
    else:
        db.session.add(UserStore(user_id=current_user.id, key="challenge", data_json=data_json))
    db.session.commit()

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


# ---------------------------------------------------------------------------
# RESTORE DATA FROM THE TWO BROWSER-DOWNLOADED BACKUP FILES
# ---------------------------------------------------------------------------
RESTORE_MARKER_KEY = "restore_backup_v1"
RESTORE_MAX_FILE_SIZE = 30 * 1024 * 1024

def parse_restore_upload(upload, label):
    if not upload or not upload.filename:
        raise ValueError(f"Choose the {label} JSON backup file.")
    raw = upload.stream.read(RESTORE_MAX_FILE_SIZE + 1)
    if len(raw) > RESTORE_MAX_FILE_SIZE:
        raise ValueError(f"The {label} backup is larger than 30 MB.")
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError(f"The {label} file is not valid JSON.")
    if not isinstance(value, dict):
        raise ValueError(f"The {label} backup has an unexpected format.")
    return value

def save_user_store(user_id, key, value):
    row = UserStore.query.filter_by(user_id=user_id, key=key).first()
    data_json = json.dumps(value)
    if row:
        row.data_json = data_json
    else:
        db.session.add(UserStore(user_id=user_id, key=key, data_json=data_json))

def restore_finance_is_empty(data):
    if not isinstance(data, dict):
        return True
    return not any([
        data.get("incomes"), data.get("tabs"), data.get("unassigned"),
        data.get("savings"), data.get("quote"), data.get("beerMoney"),
        any((wallet.get("amount", 0) or 0) for wallet in data.get("wallets", []) if isinstance(wallet, dict)),
    ])

def restore_account_is_empty(user_id):
    if Schedule.query.filter_by(user_id=user_id).first():
        return False
    finance = FinanceData.query.filter_by(user_id=user_id).first()
    if finance:
        try:
            if not restore_finance_is_empty(json.loads(finance.data_json or "{}")):
                return False
        except json.JSONDecodeError:
            return False
    for row in UserStore.query.filter_by(user_id=user_id).all():
        if row.key == RESTORE_MARKER_KEY:
            return False
        try:
            data = json.loads(row.data_json)
        except json.JSONDecodeError:
            return False
        if row.key == "tasks" and data.get("tasks"):
            return False
        if row.key == "goals" and data.get("goals"):
            return False
        if row.key == "dreams" and data.get("dreams"):
            return False
        if row.key == "challenge" and (data.get("logs") or data.get("paused")):
            return False
    return not DreamMedia.query.filter_by(user_id=user_id).first()

def clean_restore_schedules(payload):
    if not isinstance(payload, list) or len(payload) > 10000:
        raise ValueError("The calendar backup is missing or contains too many items.")
    cleaned = []
    valid_recurrences = {"none", "daily", "week", "month", "tue-sat", "weekends"}
    for item in payload:
        if not isinstance(item, dict):
            continue
        date = str(item.get("date", ""))[:10]
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            continue
        title = str(item.get("title", "")).strip()[:200]
        start_time = str(item.get("start_time", ""))[:5]
        end_time = str(item.get("end_time", ""))[:5]
        try:
            datetime.strptime(start_time, "%H:%M")
            datetime.strptime(end_time, "%H:%M")
        except ValueError:
            continue
        recurrence = str(item.get("recurrence", "none"))
        if not title or recurrence not in valid_recurrences:
            continue
        try:
            series_id = int(item["series_id"]) if item.get("series_id") is not None else None
        except (TypeError, ValueError, OverflowError):
            series_id = None
        cleaned.append({
            "series_id": series_id,
            "date": date,
            "title": title,
            "start_time": start_time,
            "end_time": end_time,
            "done": bool(item.get("done")),
            "recurrence": recurrence,
        })
    return cleaned

@app.route("/restore-data", methods=["GET", "POST"])
@login_required
def restore_data():
    result = None
    error = None
    if request.method == "POST":
        try:
            if request.form.get("confirm_restore") != "yes":
                raise ValueError("Check the confirmation box before importing the backups.")
            browser_backup = parse_restore_upload(request.files.get("browser_backup"), "browser")
            server_backup = parse_restore_upload(request.files.get("server_backup"), "server")
            local_storage = browser_backup.get("localStorage")
            if not isinstance(local_storage, dict):
                raise ValueError("The browser backup does not contain the expected localStorage data.")
            if not isinstance(server_backup.get("calendar"), list):
                raise ValueError("The server backup is missing its calendar list.")
            goals = server_backup.get("goals", [])
            dreams = server_backup.get("dreams", [])
            challenge = server_backup.get("challenge", {})
            if not isinstance(goals, list) or not isinstance(dreams, list) or not isinstance(challenge, dict):
                raise ValueError("The server backup contains data in an unexpected format.")

            if not restore_account_is_empty(current_user.id):
                raise ValueError("This account already has saved information. To prevent overwriting or duplicating it, restore only works on an empty account.")

            finance = None
            if isinstance(local_storage.get("fData"), str):
                try:
                    finance = json.loads(local_storage["fData"])
                except json.JSONDecodeError:
                    raise ValueError("The browser backup's Finance data is invalid JSON.")
                if not isinstance(finance, dict):
                    raise ValueError("The browser backup's Finance data is not in the expected format.")
            else:
                raise ValueError("The browser backup does not contain Finance data (fData). Nothing was imported.")

            task_data = None
            if isinstance(local_storage.get("tGameData"), str):
                try:
                    task_data = json.loads(local_storage["tGameData"])
                except json.JSONDecodeError:
                    raise ValueError("The browser backup's Tasks data is invalid JSON.")
                if not isinstance(task_data, dict):
                    raise ValueError("The browser backup's Tasks data is not in the expected format.")
            else:
                raise ValueError("The browser backup does not contain Tasks data (tGameData). Nothing was imported.")

            schedules = clean_restore_schedules(server_backup["calendar"])
            cleaned_goals = clean_goals({"goals": goals})
            cleaned_dreams = clean_dreams({"dreams": dreams}, current_user.id)
            if len(schedules) != len(server_backup["calendar"]):
                raise ValueError("Some calendar entries could not be read safely. Nothing was imported.")
            if len(cleaned_goals["goals"]) != len(goals):
                raise ValueError("Some goals could not be read safely. Nothing was imported.")
            if len(cleaned_dreams["dreams"]) != len(dreams):
                raise ValueError("Some dreams could not be read safely. Nothing was imported.")
            original_media_count = sum(
                len(dream.get("media") if isinstance(dream.get("media"), list) else (dream.get("photos") or []))
                for dream in dreams if isinstance(dream, dict)
            )
            restored_media_count = sum(len(dream.get("media", [])) for dream in cleaned_dreams["dreams"])
            skipped_media_count = max(0, original_media_count - restored_media_count)
            cleaned_challenge = {
                "logs": challenge.get("logs") if isinstance(challenge.get("logs"), dict) else {},
                "paused": challenge.get("paused") if isinstance(challenge.get("paused"), dict) else {},
            }
            cleaned_tasks = clean_tasks(task_data) if task_data is not None else None
            if len(cleaned_tasks["tasks"]) != len(task_data.get("tasks", [])):
                raise ValueError("Some tasks could not be read safely. Nothing was imported.")

            for item in schedules:
                db.session.add(Schedule(user_id=current_user.id, **item))
            if finance is not None:
                row = FinanceData.query.filter_by(user_id=current_user.id).first()
                if row:
                    row.data_json = json.dumps(finance)
                else:
                    db.session.add(FinanceData(user_id=current_user.id, data_json=json.dumps(finance)))
            if cleaned_tasks is not None:
                save_user_store(current_user.id, "tasks", cleaned_tasks)
            save_user_store(current_user.id, "goals", cleaned_goals)
            save_user_store(current_user.id, "dreams", cleaned_dreams)
            save_user_store(current_user.id, "challenge", cleaned_challenge)
            counts = {
                "calendar": len(schedules),
                "finance": "imported" if finance is not None else "not found in browser backup",
                "tasks": len(cleaned_tasks["tasks"]) if cleaned_tasks is not None else "not found in browser backup",
                "goals": len(cleaned_goals["goals"]),
                "dreams": len(cleaned_dreams["dreams"]),
                "challenge_logs": sum(len(days) for days in cleaned_challenge["logs"].values() if isinstance(days, dict)),
                "dream_media_skipped": skipped_media_count,
            }
            save_user_store(current_user.id, RESTORE_MARKER_KEY, {"imported_at": datetime.utcnow().isoformat() + "Z", "counts": counts})
            db.session.commit()
            result = counts
        except ValueError as exc:
            db.session.rollback()
            error = str(exc)
        except Exception:
            db.session.rollback()
            app.logger.exception("Backup restore failed")
            error = "The restore could not be completed. No changes were saved. Check the files and try again, or share this message with support: restore failed."
    return render_template("restore.html", active_page="restore", result=result, error=error)

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(host='0.0.0.0', port=5000, debug=True)
