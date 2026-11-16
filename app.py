
import os
import sqlite3
import secrets
import hashlib
import hmac
import json
import subprocess
import threading
import re
import uuid
from pathlib import Path

import numpy as np
from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

BASE = Path(__file__).parent

for directory in ("uploads", "outputs"):
    (BASE / directory).mkdir(exist_ok=True)

DB = BASE / "clips.db"

SECRET = os.environ.get("SESSION_SECRET", "")
if not SECRET:
    SECRET = secrets.token_urlsafe(32)
    print("WARNING: SESSION_SECRET not set. Sessions reset on restart.")

app = FastAPI(title="aadilclips.ai")
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET,
    https_only=os.getenv("HTTPS_ONLY") == "1",
    same_site="lax",
)

tpl = Jinja2Templates(directory=str(BASE / "templates"))
tpl.env.filters["from_json"] = json.loads

app.mount(
    "/static",
    StaticFiles(directory=str(BASE / "static")),
    name="static",
)

MAX_UPLOAD = 500 * 1024 * 1024

CAPTION_STYLES = [
    "None",
    "Hormozi Bold",
    "Karaoke Yellow",
    "Neon Red",
    "Neon Blue",
    "Minimal White",
    "Classic Yellow",
    "Gaming Green",
    "Comic Pop",
    "Cinematic",
    "TikTok Bold",
    "Instagram Clean",
    "Shadow White",
    "Outline Black",
    "Gold Luxe",
    "Pink Glow",
    "Purple Glow",
    "Orange Punch",
    "Retro Pixel",
    "Subtitle Bar",
    "High Contrast",
]

QUALITY_PRESETS = {
    "720p": (720, 24),
    "1080p": (1080, 20),
    "1440p": (1440, 18),
}

VIDEO_RATIOS = ("9:16", "1:1", "16:9")
CROP_MODES = ("fill", "fit", "blur", "manual")


def db():
    connection = sqlite3.connect(DB, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def hashpw(password, salt=None):
    salt = salt or secrets.token_hex(16)
    hashed = hashlib.scrypt(
        password.encode(),
        salt=bytes.fromhex(salt),
        n=16384,
        r=8,
        p=1,
    ).hex()
    return salt + ":" + hashed


def verify(password, stored):
    try:
        salt, _ = stored.split(":", 1)
        return hmac.compare_digest(hashpw(password, salt), stored)
    except Exception:
        return False


def init():
    with db() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                blocked INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        c.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                source TEXT,
                mode TEXT,
                status TEXT,
                progress TEXT,
                clips TEXT DEFAULT '[]',
                error TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        c.execute(
            """
            CREATE TABLE IF NOT EXISTS settings(
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )

        c.execute(
            "INSERT OR IGNORE INTO settings VALUES (?,?)",
            (
                "about",
                "Clipping My Way to Success | Whop | Content Rewards | Real Results, Real Journey",
            ),
        )

        c.execute(
            "INSERT OR IGNORE INTO settings VALUES (?,?)",
            ("daily_limit", "3"),
        )

        admin_email = os.getenv("ADMIN_EMAIL", "").strip().lower()
        admin_password = os.getenv("ADMIN_PASSWORD", "")

        if admin_email and admin_password:
            if len(admin_password) < 14:
                raise RuntimeError(
                    "ADMIN_PASSWORD must have at least 14 characters"
                )

            existing = c.execute(
                "SELECT id FROM users WHERE email=?",
                (admin_email,),
            ).fetchone()

            if existing:
                c.execute(
                    "UPDATE users SET role='admin' WHERE id=?",
                    (existing["id"],),
                )
            else:
                c.execute(
                    """
                    INSERT INTO users(email,password,role)
                    VALUES (?,?,'admin')
                    """,
                    (admin_email, hashpw(admin_password)),
                )


init()


def user(request):
    uid = request.session.get("uid")
    if not uid:
        return None

    with db() as c:
        row = c.execute(
            "SELECT id,email,role,blocked FROM users WHERE id=?",
            (uid,),
        ).fetchone()

    return row if row and not row["blocked"] else None


def page(request, name, **kwargs):
    return tpl.TemplateResponse(
        request,
        name,
        {"user": user(request), **kwargs},
    )


def require(request, admin=False):
    current_user = user(request)

    if not current_user:
        raise HTTPException(401, "Please sign in")

    if admin and current_user["role"] != "admin":
        raise HTTPException(403, "Admin only")

    return current_user


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    with db() as c:
        about = c.execute(
            "SELECT value FROM settings WHERE key='about'"
        ).fetchone()["value"]

    return page(request, "index.html", about=about)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return page(request, "auth.html", register=False)


@app.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request):
    return page(request, "auth.html", register=True)


@app.post("/signup")
def signup(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
):
    email = email.strip().lower()

    if (
        not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)
        or len(password) < 12
    ):
        return page(
            request,
            "auth.html",
            register=True,
            error="Valid email and 12+ character password required",
        )

    try:
        with db() as c:
            cur = c.execute(
                "INSERT INTO users(email,password) VALUES (?,?)",
                (email, hashpw(password)),
            )
            uid = cur.lastrowid
    except sqlite3.IntegrityError:
        return page(
            request,
            "auth.html",
            register=True,
            error="Email already registered",
        )

    request.session["uid"] = uid
    return RedirectResponse("/dashboard", 303)


@app.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
):
    with db() as c:
        found = c.execute(
            "SELECT * FROM users WHERE email=?",
            (email.strip().lower(),),
        ).fetchone()

    if (
        not found
        or found["blocked"]
        or not verify(password, found["password"])
    ):
        return page(
            request,
            "auth.html",
            register=False,
            error="Invalid credentials",
        )

    request.session["uid"] = found["id"]

    destination = (
        "/admin" if found["role"] == "admin" else "/dashboard"
    )
    return RedirectResponse(destination, 303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", 303)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    current_user = require(request)

    with db() as c:
        jobs = c.execute(
            """
            SELECT * FROM jobs
            WHERE user_id=?
            ORDER BY created_at DESC
            LIMIT 30
            """,
            (current_user["id"],),
        ).fetchall()

    return page(
        request,
        "dashboard.html",
        jobs=jobs,
        caption_styles=CAPTION_STYLES,
    )


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request):
    require(request, True)

    with db() as c:
        users = c.execute(
            """
            SELECT id,email,role,blocked,created_at
            FROM users ORDER BY id DESC
            """
        ).fetchall()

        jobs = c.execute(
            """
            SELECT jobs.*,users.email
            FROM jobs
            JOIN users ON users.id=jobs.user_id
            ORDER BY created_at DESC
            LIMIT 40
            """
        ).fetchall()

        settings = {
            row["key"]: row["value"]
            for row in c.execute("SELECT * FROM settings")
        }

    return page(
        request,
        "admin.html",
        users=users,
        jobs=jobs,
        settings=settings,
    )


@app.post("/admin/settings")
def settings(
    request: Request,
    about: str = Form(...),
    daily_limit: int = Form(...),
):
    require(request, True)

    if not 1 <= daily_limit <= 100:
        raise HTTPException(400, "Invalid limit")

    with db() as c:
        c.execute(
            "UPDATE settings SET value=? WHERE key='about'",
            (about[:1000],),
        )
        c.execute(
            "UPDATE settings SET value=? WHERE key='daily_limit'",
            (str(daily_limit),),
        )

    return RedirectResponse("/admin", 303)


@app.post("/admin/users/{uid}/toggle")
def toggle(request: Request, uid: int):
    current_user = require(request, True)

    if uid == current_user["id"]:
        raise HTTPException(400, "Cannot block yourself")

    with db() as c:
        c.execute(
            """
            UPDATE users
            SET blocked=1-blocked
            WHERE id=? AND role!='admin'
            """,
            (uid,),
        )

    return RedirectResponse("/admin", 303)


def run(command, timeout=1800):
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )


def update(jid, **fields):
    allowed = {
        "status",
        "progress",
        "clips",
        "error",
    }

    with db() as c:
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError("Invalid job update field")

            c.execute(
                f"UPDATE jobs SET {key}=? WHERE id=?",
                (value, jid),
            )


def select_windows(
    path,
    mode,
    clip_length=30,
    max_clips=3,
):
    # Audio-energy based selection, not semantic AI analysis.
    import wave

    pcm = BASE / "uploads" / (uuid.uuid4().hex + ".wav")

    try:
        run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(path),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "8000",
                "-acodec",
                "pcm_s16le",
                str(pcm),
            ],
            timeout=600,
        )

        with wave.open(str(pcm), "rb") as audio:
            samples = (
                np.frombuffer(
                    audio.readframes(audio.getnframes()),
                    dtype=np.int16,
                ).astype(np.float32)
                / 32768
            )

        duration = len(samples) / 8000

        if duration < 5:
            raise ValueError("Video must be at least 5 seconds")

        length = min(
            clip_length,
            max(5, int(duration)),
        )

        if duration <= length:
            return [(0, length)]

        windows = []

        for start in range(
            0,
            max(1, int(duration - length) + 1),
            5,
        ):
            chunk = samples[
                start * 8000:
                int((start + length) * 8000)
            ]

            if len(chunk) < 8000:
                continue

            rms = float(np.sqrt(np.mean(chunk ** 2)))
            peaks = float(
                np.percentile(np.abs(chunk[::40]), 95)
            )

            if mode == "funny":
                score = 0.65 * rms + 0.35 * peaks
            else:
                score = 0.5 * rms + 0.5 * peaks

            windows.append((score, start))

        chosen = []

        for _, start in sorted(windows, reverse=True):
            if all(
                abs(start - previous_start) >= length * 0.8
                for previous_start, _ in chosen
            ):
                chosen.append((start, length))

            if len(chosen) >= max_clips:
                break

        return sorted(chosen) or [(0, length)]

    finally:
        pcm.unlink(missing_ok=True)


def get_dimensions(quality, video_ratio):
    base_size, crf = QUALITY_PRESETS[quality]

    if video_ratio == "9:16":
        width = base_size
        height = base_size * 16 // 9

    elif video_ratio == "1:1":
        width = base_size
        height = base_size

    elif video_ratio == "16:9":
        width = base_size * 16 // 9
        height = base_size

    else:
        raise ValueError("Unsupported video ratio")

    return width, height, crf


def build_video_filter(width, height, crop_mode):
    if crop_mode in ("fill", "manual"):
        return (
            f"scale={width}:{height}:"
            "force_original_aspect_ratio=increase,"
            f"crop={width}:{height},"
            "setsar=1"
        )

    if crop_mode == "fit":
        return (
            f"scale={width}:{height}:"
            "force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:"
            "color=black,"
            "setsar=1"
        )

    if crop_mode == "blur":
        return (
            "split=2[background][foreground];"
            f"[background]scale={width}:{height}:"
            "force_original_aspect_ratio=increase,"
            f"crop={width}:{height},"
            "boxblur=20:2[blurred];"
            f"[foreground]scale={width}:{height}:"
            "force_original_aspect_ratio=decrease"
            "[front];"
            "[blurred][front]overlay="
            "(W-w)/2:(H-h)/2,"
            "setsar=1"
        )

    raise ValueError("Unsupported crop mode")


def caption_filter(
    folder,
    index,
    width,
    caption_style,
    caption_text,
    caption_size,
    caption_position,
):
    if caption_style == "None" or not caption_text.strip():
        return ""

    colors = {
        "Karaoke Yellow": "yellow",
        "Classic Yellow": "yellow",
        "Neon Red": "red",
        "Neon Blue": "cyan",
        "Gaming Green": "lime",
        "Gold Luxe": "gold",
        "Pink Glow": "hotpink",
        "Purple Glow": "violet",
        "Orange Punch": "orange",
    }

    color = colors.get(caption_style, "white")

    positions = {
        "top": "h*0.15",
        "middle": "(h-text_h)/2",
        "bottom": "h*0.78",
    }

    caption_file = folder / f"caption_{index}.txt"
    caption_file.write_text(
        caption_text,
        encoding="utf-8",
    )

    fontfile = os.getenv(
        "CAPTION_FONT_FILE",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    )

    if not Path(fontfile).is_file():
        raise ValueError(
            "Caption font unavailable. "
            "Set CAPTION_FONT_FILE to a valid .ttf path"
        )

    if any(
        char in str(fontfile)
        for char in "':,;[]\\"
    ) or any(
        char in str(caption_file)
        for char in "':,;[]\\"
    ):
        raise ValueError(
            "Caption path contains unsupported characters"
        )

    fontsize = max(
        20,
        int(caption_size * width / 1080),
    )

    box = ""

    if caption_style == "Subtitle Bar":
        box = (
            ":box=1"
            ":boxcolor=black@0.65"
            ":boxborderw=14"
        )

    return (
        f"drawtext=fontfile='{fontfile}':"
        f"textfile='{caption_file}':"
        f"fontcolor={color}:"
        f"fontsize={fontsize}:"
        "borderw=3:"
        "bordercolor=black:"
        "x=(w-text_w)/2:"
        f"y={positions[caption_position]}"
        f"{box}"
    )


def process(
    jid,
    source,
    mode,
    upload_path=None,
    clip_length=30,
    quality="1080p",
    caption_style="None",
    caption_text="",
    caption_size=48,
    caption_position="bottom",
    video_ratio="9:16",
    crop_mode="fill",
):
    try:
        update(
            jid,
            status="processing",
            progress="Preparing source",
        )

        folder = BASE / "uploads" / jid
        folder.mkdir(exist_ok=True)

        if upload_path:
            src = Path(upload_path)
        else:
            if os.getenv("ENABLE_YOUTUBE") != "1":
                raise ValueError(
                    "YouTube URL importing is disabled. "
                    "Upload a video you own or have permission to use."
                )

            if not re.fullmatch(
                r"https://(www\.)?"
                r"(youtube\.com/watch\?[^\s]+|youtu\.be/[\w-]+)",
                source,
            ):
                raise ValueError(
                    "Only YouTube URLs are supported"
                )

            run(
                [
                    "yt-dlp",
                    "--no-playlist",
                    "--max-filesize",
                    "500M",
                    "--match-filter",
                    "duration <= 3600",
                    "-f",
                    "bv*+ba/b",
                    "--merge-output-format",
                    "mp4",
                    "-o",
                    str(folder / "source.%(ext)s"),
                    source,
                ],
                timeout=900,
            )

            matches = list(folder.glob("source.*"))

            if not matches:
                raise ValueError(
                    "Video could not be imported"
                )

            src = matches[0]

        update(
            jid,
            progress="Analyzing audio highlights",
        )

        windows = select_windows(
            src,
            mode,
            clip_length=clip_length,
        )

        width, height, crf = get_dimensions(
            quality,
            video_ratio,
        )

        output_clips = []

        for index, (start, length) in enumerate(
            windows,
            1,
        ):
            update(
                jid,
                progress=(
                    f"Rendering clip {index}/{len(windows)}"
                ),
            )

            filename = f"{jid}_{index}.mp4"
            target = BASE / "outputs" / filename

            video_filter = build_video_filter(
                width,
                height,
                crop_mode,
            )

            text_filter = caption_filter(
                folder,
                index,
                width,
                caption_style,
                caption_text,
                caption_size,
                caption_position,
            )

            if crop_mode == "blur":
                # The blur filter has multiple inputs,
                # so FFmpeg needs filter_complex.
                if text_filter:
                    filter_graph = (
                        video_filter
                        + ","
                        + text_filter
                        + "[outv]"
                    )
                else:
                    filter_graph = (
                        video_filter
                        + "[outv]"
                    )

                filter_arguments = [
                    "-filter_complex",
                    filter_graph,
                    "-map",
                    "[outv]",
                    "-map",
          
