from __future__ import annotations

import csv
import hmac
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Optional
from urllib.parse import quote

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.utils import secure_filename

# ----------------------------
# Paths (Render Disk ready)
# ----------------------------
BASE_DIR = Path(__file__).resolve().parent

DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data"))).resolve()
UPLOADS_DIR = Path(os.environ.get("UPLOADS_DIR", str(BASE_DIR / "uploads"))).resolve()

DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

SUBMISSIONS_CSV = DATA_DIR / "submissions.csv"

# ----------------------------
# Upload policy
# ----------------------------
ALLOWED_EXT = {"jpg", "jpeg", "png", "webp"}
MAX_FILES = int(os.environ.get("MAX_FILES", "5"))
MAX_TOTAL_MB = int(os.environ.get("MAX_TOTAL_MB", "25"))  # whole request cap
MAX_FILE_MB = int(os.environ.get("MAX_FILE_MB", "10"))    # per photo cap

app = Flask(__name__, static_folder="static", static_url_path="/static")

@app.template_filter('is_numeric')
def is_numeric_filter(value) -> bool:
    """Return True if the string looks like a plain number (after removing separators)."""
    if value is None:
        return False
    s = str(value).strip()
    if not s:
        return False
    # remove common thousands separators and spaces
    for ch in (' ', '\u00a0', ',', '.', '_'):
        s = s.replace(ch, '')
    return s.isdigit()

app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["MAX_CONTENT_LENGTH"] = MAX_TOTAL_MB * 1024 * 1024


# ----------------------------
# Helpers
# ----------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id() -> str:
    return uuid.uuid4().hex[:10].upper()


def _allowed_file(filename: str) -> bool:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext in ALLOWED_EXT


def _csv_columns() -> list[str]:
    # Единый CSV для карточек (NR KITAP)
    return [
        "id",
        "created_utc",
        "kind",
        "title",
        "price_tenge",
        "phone",
        "description",
        "photos",
        "password",
    ]


def _ensure_csv_header() -> None:
    """Ensure CSV exists with the expected header.
    If file exists but header is missing 'password', do a simple migration.
    """
    if not SUBMISSIONS_CSV.exists():
        with SUBMISSIONS_CSV.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(_csv_columns())
        return

    # Migration: add missing columns to existing CSV (most importantly 'password')
    with SUBMISSIONS_CSV.open("r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        rows = list(reader)

    if not rows:
        with SUBMISSIONS_CSV.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(_csv_columns())
        return

    header = rows[0]
    expected = _csv_columns()

    if header == expected:
        return

    if "password" not in header:
        new_header = header + ["password"]
        new_rows = [new_header]
        for r in rows[1:]:
            new_rows.append(r + [""])
        with SUBMISSIONS_CSV.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerows(new_rows)
        return

    # If header differs in other ways, keep it as-is (avoid data loss).
    return


def _save_submission_row(
    sid: str,
    created_utc: str,
    kind: str,
    title: str,
    price_tenge: str,
    phone: str,
    description: str,
    photos: List[str],
    password: str = "",
):
    _ensure_csv_header()
    with SUBMISSIONS_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            sid,
            created_utc,
            kind,
            title,
            price_tenge,
            phone,
            description,
            ";".join(photos),
            password,
        ])


def _read_all_rows() -> list[dict]:
    if not SUBMISSIONS_CSV.exists():
        return []
    with SUBMISSIONS_CSV.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows: list[dict] = []
        for r in reader:
            if not (r.get("id") or "").strip():
                continue
            rows.append(r)
        return rows


def _write_all_rows(rows: list[dict]) -> None:
    # атомарная запись
    tmp = SUBMISSIONS_CSV.with_suffix(".tmp")
    cols = _csv_columns()
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            out = {c: (r.get(c, "") or "") for c in cols}
            w.writerow(out)
    tmp.replace(SUBMISSIONS_CSV)


def _find_row(rows: list[dict], sid: str) -> Optional[dict]:
    for r in rows:
        if (r.get("id") or "").strip() == sid:
            return r
    return None


def _list_photos(sid: str) -> list[str]:
    d = UPLOADS_DIR / sid
    if not d.exists() or not d.is_dir():
        return []
    return sorted([p.name for p in d.iterdir() if p.is_file()])


def _thumb_url(sid: str, kind: str, photos: list[str]) -> str:
    # превью: первое изображение, иначе лого
    if photos:
        first = photos[0]
        ext = first.rsplit(".", 1)[-1].lower() if "." in first else ""
        if ext in {"jpg","jpeg","png","webp","gif"}:
            return f"/uploads/{sid}/{quote(first)}"
    return "/static/logo.jpeg"

    # продавцы: первое фото, иначе лого
    if photos:
        return f"/uploads/{sid}/{quote(photos[0])}"
    return "/static/logo.jpeg"


def _load_submissions(limit: int = 200) -> list[dict]:
    """Newer-first list for public page."""
    if not SUBMISSIONS_CSV.exists():
        return []

    items: list[dict] = []
    with SUBMISSIONS_CSV.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            sid = (row.get("id") or "").strip()
            if not sid:
                continue

            kind = (row.get("kind") or "material").strip().lower()

            photos_raw = (row.get("photos") or "").strip()
            photos = [p for p in photos_raw.split(";") if p] if photos_raw else []

            items.append({
                "id": sid,
                "created_utc": (row.get("created_utc") or "").strip(),
                "kind": kind,
                "title": (row.get("title") or "").strip(),
                "price_tenge": (row.get("price_tenge") or "").strip(),
                "phone": (row.get("phone") or "").strip(),
                "description": (row.get("description") or "").strip(),
                "photos": photos,
                "thumb_url": _thumb_url(sid, kind, photos),
                "password": (row.get("password") or "").strip(),
            })

    # newest first: created_utc is ISO, so lexicographic sort works
    items.sort(key=lambda x: x.get("created_utc", ""), reverse=True)
    return items[:limit]


# ----------------------------
# Public routes
# ----------------------------

@app.get("/")
def index():
    submissions = _load_submissions(limit=int(os.environ.get("MAX_LISTINGS", "200")))

    unlocked = set(session.get("unlocked_cards", []) or [])
    for s in submissions:
        sid = s.get("id")
        s["unlocked"] = bool(sid and sid in unlocked)

    return render_template("index.html", submissions=submissions)


@app.post("/submit")
def submit():
    # Публичная отправка отключена (карточки создаёт только админ через /admin/new)
    abort(404)


@app.get("/thanks/<sid>")
def thanks(sid: str):
    rows = _read_all_rows()
    r = _find_row(rows, sid)
    kind = ((r.get("kind") or "sell") if r else "sell").strip().lower()
    if kind not in {"buy", "sell"}:
        kind = "sell"

    photos: list[str] = []
    sub_dir = UPLOADS_DIR / sid
    if sub_dir.exists() and sub_dir.is_dir():
        photos = sorted([p.name for p in sub_dir.iterdir() if p.is_file()])

    return render_template("thanks.html", sid=sid, photos=photos, kind=kind)




@app.post("/unlock/<sid>")
def unlock(sid: str):
    password = (request.form.get("password") or "").strip()
    rows = _read_all_rows()
    r = _find_row(rows, sid)
    if not r:
        abort(404)

    expected = (r.get("password") or "").strip()
    if not expected:
        # карточка без пароля
        return redirect(url_for("index"))

    if password and hmac.compare_digest(password, expected):
        unlocked = list(session.get("unlocked_cards", []) or [])
        if sid not in unlocked:
            unlocked.append(sid)
        session["unlocked_cards"] = unlocked
        return redirect(url_for("index"))

    flash("Неверный пароль.")
    return redirect(url_for("index"))

# Если не хочешь публичные ссылки на фото — удали этот роут
@app.get("/uploads/<sid>/<path:filename>")
def uploads(sid: str, filename: str):
    # Если на карточке стоит пароль — файлы доступны только после ввода пароля
    if not _is_admin():
        rows = _read_all_rows()
        r = _find_row(rows, sid)
        if r:
            expected = (r.get("password") or "").strip()
            if expected:
                unlocked = set(session.get("unlocked_cards", []) or [])
                if sid not in unlocked:
                    abort(403)
    return send_from_directory(UPLOADS_DIR / sid, filename)


@app.get("/health")
def health():
    return {"status": "ok"}


# ----------------------------
# Admin
# ----------------------------

ADMIN_KEY = os.environ.get("ADMIN_KEY", "").strip()


def _is_admin() -> bool:
    if not ADMIN_KEY:
        return False
    k = session.get("admin_key", "")
    return bool(k) and hmac.compare_digest(k, ADMIN_KEY)


def admin_required(fn):
    from functools import wraps

    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not _is_admin():
            return redirect(url_for("admin_login"))
        return fn(*args, **kwargs)

    return wrapper


def _admin_submissions(limit: int = 500) -> list[dict]:
    rows = _read_all_rows()
    items: list[dict] = []
    for r in rows:
        sid = (r.get("id") or "").strip()
        kind = (r.get("kind") or "sell").strip().lower()
        if kind not in {"buy", "sell"}:
            kind = "sell"

        photos_raw = (r.get("photos") or "").strip()
        photos = [p for p in photos_raw.split(";") if p] if photos_raw else _list_photos(sid)

        items.append({
            "id": sid,
            "created_utc": (r.get("created_utc") or "").strip(),
            "kind": kind,
            "title": (r.get("title") or "").strip(),
            "price_tenge": (r.get("price_tenge") or "").strip(),
            "phone": (r.get("phone") or "").strip(),
            "description": (r.get("description") or "").strip(),
            "photos": photos,
            "thumb_url": _thumb_url(sid, kind, photos),
            "password": (r.get("password") or "").strip(),
        })

    items.sort(key=lambda x: x.get("created_utc", ""), reverse=True)
    return items[:limit]


@app.get("/admin/login")
def admin_login():
    return render_template("admin/login.html")


@app.post("/admin/login")
def admin_login_post():
    key = (request.form.get("key") or "").strip()
    if not ADMIN_KEY:
        flash("ADMIN_KEY не задан в Render Environment.")
        return redirect(url_for("admin_login"))
    if hmac.compare_digest(key, ADMIN_KEY):
        session["admin_key"] = key
        return redirect("/admin")
    flash("Неверный ключ.")
    return redirect(url_for("admin_login"))


@app.get("/admin/logout")
def admin_logout():
    session.pop("admin_key", None)
    return redirect(url_for("index"))


@app.get("/admin")
@admin_required
def admin_index():
    subs = _admin_submissions()
    return render_template("admin/index.html", submissions=subs)




@app.get("/admin/new")
@admin_required
def admin_new():
    return render_template("admin/new.html")


@app.post("/admin/create")
@admin_required
def admin_create():
    # Создаём новую карточку (только админ)
    title = (request.form.get("title") or "").strip()
    price_tenge = (request.form.get("price") or "").strip()
    description = (request.form.get("description") or "").strip()
    password = (request.form.get("password") or "").strip()

    files = request.files.getlist("photos")
    files = [f for f in files if f and f.filename]

    sid = _new_id()
    created_utc = _now_iso()

    saved_names: List[str] = []
    if files:
        sub_dir = UPLOADS_DIR / sid
        sub_dir.mkdir(parents=True, exist_ok=True)
        for f in files:
            safe = secure_filename(f.filename) or "file"
            target = sub_dir / safe
            if target.exists():
                target = sub_dir / f"{target.stem}_{uuid.uuid4().hex[:6]}{target.suffix}"
            f.save(target)
            saved_names.append(target.name)

    _save_submission_row(
        sid=sid,
        created_utc=created_utc,
        kind="material",
        title=title,
        price_tenge=price_tenge,
        phone="",
        description=description,
        photos=saved_names,
        password=password,
    )

    flash("Карточка создана.")
    return redirect(f"/admin/edit/{sid}")


@app.get("/admin/edit/<sid>")
@admin_required
def admin_edit(sid: str):
    rows = _read_all_rows()
    r = _find_row(rows, sid)
    if not r:
        abort(404)

    photos = _list_photos(sid)
    first_photo = photos[0] if photos else ""
    row = {c: (r.get(c, "") or "") for c in _csv_columns()}
    return render_template("admin/edit.html", sid=sid, row=row, photos=photos, first_photo=first_photo)


@app.post("/admin/save/<sid>")
@admin_required
def admin_save(sid: str):
    rows = _read_all_rows()
    r = _find_row(rows, sid)
    if not r:
        abort(404)

    r["kind"] = (r.get("kind") or "material")
    r["title"] = (request.form.get("title") or "").strip()
    r["price_tenge"] = (request.form.get("price_tenge") or "").strip()
    r["description"] = (request.form.get("description") or "").strip()
    r["password"] = (request.form.get("password") or "").strip()

    # синхронизируем список фото с папкой
    photos = _list_photos(sid)
    r["photos"] = ";".join(photos)

    _write_all_rows(rows)
    flash("Сохранено.")
    return redirect(f"/admin/edit/{sid}")


@app.post("/admin/delete/<sid>")
@admin_required
def admin_delete(sid: str):
    rows = _read_all_rows()
    rows2 = [r for r in rows if (r.get("id") or "").strip() != sid]
    _write_all_rows(rows2)

    d = UPLOADS_DIR / sid
    if d.exists() and d.is_dir():
        shutil.rmtree(d)

    flash(f"Удалено: {sid}")
    return redirect("/admin")


@app.post("/admin/photo_delete/<sid>/<path:filename>")
@admin_required
def admin_photo_delete(sid: str, filename: str):
    p = (UPLOADS_DIR / sid / filename).resolve()
    base = (UPLOADS_DIR / sid).resolve()
    if not str(p).startswith(str(base)):
        abort(400)

    if p.exists() and p.is_file():
        p.unlink()

    rows = _read_all_rows()
    r = _find_row(rows, sid)
    if r:
        r["photos"] = ";".join(_list_photos(sid))
        _write_all_rows(rows)

    return redirect(f"/admin/edit/{sid}")


@app.post("/admin/upload/<sid>")
@admin_required
def admin_upload(sid: str):
    files = request.files.getlist("photos")
    files = [f for f in files if f and f.filename]

    if not files:
        flash("Не выбраны файлы.")
        return redirect(f"/admin/edit/{sid}")

    sub_dir = UPLOADS_DIR / sid
    sub_dir.mkdir(parents=True, exist_ok=True)

    saved = 0
    for f in files:
        name = secure_filename(f.filename) or "photo.jpg"
        target = sub_dir / name
        if target.exists():
            target = sub_dir / f"{target.stem}_{uuid.uuid4().hex[:6]}{target.suffix}"
        f.save(target)
        saved += 1

    rows = _read_all_rows()
    r = _find_row(rows, sid)
    if r:
        r["photos"] = ";".join(_list_photos(sid))
        _write_all_rows(rows)

    flash(f"Загружено файлов: {saved}")
    return redirect(f"/admin/edit/{sid}")


@app.get("/admin/csv")
@admin_required
def admin_csv_download():
    if not SUBMISSIONS_CSV.exists():
        abort(404)
    return send_file(SUBMISSIONS_CSV, as_attachment=True, download_name="submissions.csv")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)