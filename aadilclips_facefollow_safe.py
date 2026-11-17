import os
import re
import json
import uuid
import hmac
import hashlib
import secrets
import sqlite3
import subprocess
import threading
import wave
import urllib.request
import urllib.error
import sys
from pathlib import Path

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
import numpy as np
from fastapi import FastAPI, Request, Form, File, UploadFile, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

BASE = Path(__file__).resolve().parent
for folder in ("uploads", "outputs", "static"):
    (BASE / folder).mkdir(exist_ok=True)
DB = BASE / "clips.db"
app = FastAPI(title="aadilclips.ai")
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET") or secrets.token_urlsafe(32),
    same_site="lax",
)
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
tpl = Jinja2Templates(directory=str(BASE / "templates"))
tpl.env.filters["from_json"] = lambda value: json.loads(value or "[]")

MAX_UPLOAD = 500 * 1024 * 1024
CAPTION_STYLES = [
    "None", "Hormozi Bold", "Karaoke Yellow", "Neon Red", "Neon Blue",
    "Minimal White", "Classic Yellow", "Gaming Green", "Comic Pop",
    "Cinematic", "TikTok Bold", "Instagram Clean", "Shadow White",
    "Outline Black", "Gold Luxe", "Pink Glow", "Purple Glow",
    "Orange Punch", "Retro Pixel", "Subtitle Bar", "High Contrast",
]
QUALITY_PRESETS = {"720p": (720, 24), "1080p": (1080, 20), "1440p": (1440, 18)}
VIDEO_RATIOS = ("9:16", "1:1", "16:9")
CROP_MODES = ("fill", "fit", "blur", "manual", "follow")
PROCESS_LOCK = threading.Lock()
GROQ_API = "https://api.groq.com/openai/v1"


def db():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def hashpw(password, salt=None):
    salt = salt or secrets.token_hex(16)
    result = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
    return salt + ":" + result.hex()


def verify(password, stored):
    try:
        return hmac.compare_digest(hashpw(password, stored.split(":")[0]), stored)
    except Exception:
        return False


def init():
    with db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,email TEXT UNIQUE NOT NULL,password TEXT NOT NULL,role TEXT NOT NULL DEFAULT 'user',blocked INTEGER DEFAULT 0,created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        c.execute("CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,source TEXT,mode TEXT,status TEXT,progress TEXT,clips TEXT DEFAULT '[]',error TEXT DEFAULT '',created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        c.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT)")
        c.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", ("about", "Clipping My Way to Success | Whop | Content Rewards | Real Results, Real Journey"))
        c.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", ("daily_limit", "3"))
        email = os.getenv("ADMIN_EMAIL", "").strip().lower()
        password = os.getenv("ADMIN_PASSWORD", "")
        if email and password:
            if len(password) < 14:
                raise RuntimeError("ADMIN_PASSWORD must have at least 14 characters")
            existing = c.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
            if existing:
                c.execute("UPDATE users SET role='admin' WHERE id=?", (existing["id"],))
            else:
                c.execute("INSERT INTO users(email,password,role) VALUES (?,?,'admin')", (email, hashpw(password)))


init()


def user(request):
    uid = request.session.get("uid")
    if not uid:
        return None
    with db() as c:
        found = c.execute("SELECT id,email,role,blocked FROM users WHERE id=?", (uid,)).fetchone()
    return found if found and not found["blocked"] else None


def require(request, admin=False):
    found = user(request)
    if not found:
        raise HTTPException(401, "Please sign in")
    if admin and found["role"] != "admin":
        raise HTTPException(403, "Admin only")
    return found


def page(request, name, **kwargs):
    return tpl.TemplateResponse(request, name, {"user": user(request), **kwargs})


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    with db() as c:
        about = c.execute("SELECT value FROM settings WHERE key='about'").fetchone()["value"]
    return page(request, "index.html", about=about)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return page(request, "auth.html", register=False)


@app.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request):
    return page(request, "auth.html", register=True)


@app.post("/signup")
def signup(request: Request, email: str = Form(...), password: str = Form(...)):
    email = email.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or len(password) < 12:
        return page(request, "auth.html", register=True, error="Valid email and 12+ character password required")
    try:
        with db() as c:
            uid = c.execute("INSERT INTO users(email,password) VALUES (?,?)", (email, hashpw(password))).lastrowid
    except sqlite3.IntegrityError:
        return page(request, "auth.html", register=True, error="Email already registered")
    request.session["uid"] = uid
    return RedirectResponse("/dashboard", 303)


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    with db() as c:
        found = c.execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
    if not found or found["blocked"] or not verify(password, found["password"]):
        return page(request, "auth.html", register=False, error="Invalid credentials")
    request.session["uid"] = found["id"]
    return RedirectResponse("/admin" if found["role"] == "admin" else "/dashboard", 303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", 303)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    found = require(request)
    with db() as c:
        jobs = c.execute("SELECT * FROM jobs WHERE user_id=? ORDER BY created_at DESC LIMIT 30", (found["id"],)).fetchall()
    return page(request, "dashboard_facefollow.html", jobs=jobs, caption_styles=CAPTION_STYLES)


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request):
    require(request, True)
    with db() as c:
        users = c.execute("SELECT id,email,role,blocked,created_at FROM users ORDER BY id DESC").fetchall()
        jobs = c.execute("SELECT jobs.*,users.email FROM jobs JOIN users ON users.id=jobs.user_id ORDER BY jobs.created_at DESC LIMIT 40").fetchall()
        settings = {row["key"]: row["value"] for row in c.execute("SELECT * FROM settings")}
    return page(request, "admin.html", users=users, jobs=jobs, settings=settings)


@app.post("/admin/settings")
def settings(request: Request, about: str = Form(...), daily_limit: int = Form(...)):
    require(request, True)
    if not 1 <= daily_limit <= 100:
        raise HTTPException(400, "Invalid limit")
    with db() as c:
        c.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", ("about", about[:1000]))
        c.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", ("daily_limit", str(daily_limit)))
    return RedirectResponse("/admin", 303)


@app.post("/admin/users/{uid}/toggle")
def toggle(request: Request, uid: int):
    found = require(request, True)
    if uid == found["id"]:
        raise HTTPException(400, "Cannot block yourself")
    with db() as c:
        c.execute("UPDATE users SET blocked=1-blocked WHERE id=? AND role!='admin'", (uid,))
    return RedirectResponse("/admin", 303)


def run(command, timeout=900):
    if command and command[0] == "ffmpeg":
        command = [command[0], "-hide_banner", "-loglevel", "error", "-nostdin", *command[1:]]
    return subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, check=True, timeout=timeout)


def update(jid, **fields):
    allowed = {"status", "progress", "clips", "error"}
    with db() as c:
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError("Invalid job field")
            c.execute(f"UPDATE jobs SET {key}=? WHERE id=?", (value, jid))


def select_windows(path, mode, clip_length=30, max_clips=3):
    pcm = BASE / "uploads" / (uuid.uuid4().hex + ".wav")
    try:
        run(["ffmpeg", "-y", "-i", str(path), "-vn", "-ac", "1", "-ar", "8000", "-acodec", "pcm_s16le", str(pcm)], 600)
        segments = []
        with wave.open(str(pcm), "rb") as audio:
            duration = audio.getnframes() / float(audio.getframerate())
            if duration < 5:
                raise ValueError("Video must be at least 5 seconds and contain audio")
            length = min(clip_length, int(duration))
            if duration <= length:
                return [(0, length)]
            while True:
                raw = audio.readframes(5 * audio.getframerate())
                if not raw:
                    break
                chunk = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                if not len(chunk):
                    break
                rms = float(np.sqrt(np.mean(chunk * chunk)))
                peak = float(np.percentile(np.abs(chunk[::40]), 95))
                score = (0.65 * rms + 0.35 * peak) if mode == "funny" else (0.5 * rms + 0.5 * peak)
                segments.append(score)
        if not segments:
            raise ValueError("Unable to analyze video audio")
        windows = []
        segment_count = max(1, (length + 4) // 5)
        for start in range(0, max(1, int(duration - length) + 1), 5):
            i = start // 5
            batch = segments[i:i + segment_count]
            if batch:
                windows.append((sum(batch) / len(batch), start))
        chosen = []
        for _, start in sorted(windows, reverse=True):
            if all(abs(start - old) >= length * 0.8 for old, _ in chosen):
                chosen.append((start, length))
            if len(chosen) >= max_clips:
                break
        return sorted(chosen) or [(0, length)]
    finally:
        pcm.unlink(missing_ok=True)

def get_dimensions(quality, ratio):
    size, crf = QUALITY_PRESETS[quality]
    if ratio == "9:16":
        return size, size * 16 // 9, crf
    if ratio == "16:9":
        return size * 16 // 9, size, crf
    return size, size, crf


def video_filter(width, height, crop, crop_x=50, crop_y=50):
    if crop == "manual":
        x = crop_x / 100.0
        y = crop_y / 100.0
        return (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}:(iw-ow)*{x:.4f}:(ih-oh)*{y:.4f},setsar=1"
        )
    if crop == "fill":
        return f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1"
    if crop == "fit":
        return f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
    return (
        f"split=2[bg][fg];[bg]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},boxblur=20:2[blur];"
        f"[fg]scale={width}:{height}:force_original_aspect_ratio=decrease[front];"
        "[blur][front]overlay=(W-w)/2:(H-h)/2,setsar=1"
    )



def face_follow_filter(src, start, duration, width, height, jid, index):
    helper = BASE / "aadilclips_face_detector.py"
    if not helper.exists():
        raise ValueError("Face Follow detector file is missing")
    update(jid, progress=f"Analyzing faces in clip {index}")
    try:
        result = subprocess.run(
            [sys.executable, str(helper), str(src), str(start), str(duration)],
            capture_output=True, text=True, timeout=180, check=True,
        )
        points = json.loads(result.stdout)
    except subprocess.CalledProcessError as exc:
        message = (exc.stderr or "Face detection process failed").strip()
        raise ValueError(f"Face Follow unavailable: {message[:180]}") from None
    except subprocess.TimeoutExpired:
        raise ValueError("Face Follow analysis timed out. Try a shorter video") from None
    except (ValueError, json.JSONDecodeError):
        raise ValueError("Face Follow returned invalid tracking data") from None
    if not points:
        return video_filter(width, height, "fill")
    points = sorted(points, key=lambda p: p["t"])
    points.insert(0, {"t": 0.0, "x": points[0]["x"], "y": points[0]["y"]})
    if len(points) > 30:
        points = points[::max(1, len(points) // 30)]

    def make_expression(axis):
        expr = f"{max(0.05, min(0.95, float(points[-1][axis]))):.4f}"
        for i in range(len(points) - 2, -1, -1):
            t0 = max(0.0, float(points[i]["t"]))
            t1 = max(t0 + 0.01, float(points[i + 1]["t"]))
            v0 = max(0.05, min(0.95, float(points[i][axis])))
            v1 = max(0.05, min(0.95, float(points[i + 1][axis])))
            slope = (v1 - v0) / (t1 - t0)
            linear = f"({v0:.5f}+({slope:.6f})*(t-{t0:.3f}))"
            expr = f"if(lt(t\\,{t1:.3f})\\,{linear}\\,{expr})"
        dim = "iw" if axis == "x" else "ih"
        output = "ow" if axis == "x" else "oh"
        return f"clip({dim}*({expr})-{output}/2\\,0\\,{dim}-{output})"

    x = make_expression("x")
    y = make_expression("y")
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"setpts=PTS-STARTPTS,crop={width}:{height}:{x}:{y},setsar=1"
    )

def caption_filter(folder, index, width, style, text, size, position):
    if style == "None" or not text.strip():
        return ""
    font = os.getenv("CAPTION_FONT_FILE", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
    if not Path(font).is_file():
        raise ValueError("Caption font not installed on server")
    colors = {
        "Karaoke Yellow": "yellow", "Classic Yellow": "yellow",
        "Neon Red": "red", "Neon Blue": "cyan", "Gaming Green": "lime",
        "Gold Luxe": "gold", "Pink Glow": "pink", "Purple Glow": "violet",
        "Orange Punch": "orange",
    }
    y = {"top": "h*0.15", "middle": "(h-text_h)/2", "bottom": "h*0.78"}[position]
    txt = folder / f"caption_{index}.txt"
    txt.write_text(text, encoding="utf-8")
    if any(ch in str(txt) + str(font) for ch in "':,;[]\\"):
        raise ValueError("Caption path contains unsupported characters")
    return (
        f"drawtext=fontfile='{font}':textfile='{txt}':fontcolor={colors.get(style, 'white')}:"
        f"fontsize={max(20, int(size * width / 1080))}:borderw=3:bordercolor=black:"
        f"x=(w-text_w)/2:y={y}"
    )


def groq_post(endpoint, body, content_type, timeout=120):
    key = os.getenv("GROQ_API_KEY", "").strip()
    if not key:
        raise ValueError("Auto captions need GROQ_API_KEY in Render Environment")
    request = urllib.request.Request(
        GROQ_API + endpoint,
        data=body,
        headers={"Authorization": "Bearer " + key, "Content-Type": content_type, "Accept": "application/json", "User-Agent": "curl/8.7.1"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        details = exc.read(500).decode("utf-8", errors="replace")
        raise ValueError(f"AI captions API error {exc.code}: {details[:250]}") from None
    except urllib.error.URLError as exc:
        raise ValueError("AI captions service is unreachable; please retry") from exc


def groq_transcribe(audio_path):
    boundary = "aadilclips" + uuid.uuid4().hex
    parts = []

    def add(name, data, filename=None, mime=None):
        head = f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\""
        if filename:
            head += f"; filename=\"{filename}\""
        head += "\r\n"
        if mime:
            head += f"Content-Type: {mime}\r\n"
        parts.append(head.encode() + b"\r\n" + data + b"\r\n")

    add("model", b"whisper-large-v3-turbo")
    add("response_format", b"verbose_json")
    add("timestamp_granularities[]", b"word")
    add("timestamp_granularities[]", b"segment")
    add("temperature", b"0")
    add("file", audio_path.read_bytes(), audio_path.name, "audio/mpeg")
    payload = b"".join(parts) + f"--{boundary}--\r\n".encode()
    data = groq_post("/audio/transcriptions", payload, f"multipart/form-data; boundary={boundary}", 180)
    words = []
    for item in data.get("words", []):
        try:
            start = float(item["start"])
            end = float(item["end"])
            word = str(item["word"]).strip()
        except (ValueError, TypeError, KeyError):
            continue
        if word and end > start >= 0:
            words.append({"text": word, "start": start, "end": end})
    if not words:
        raise ValueError("Speech not detected or word timestamps unavailable")
    return words


def roman_hinglish(words):
    if not any(re.search(r"[\u0900-\u097f]", w["text"]) for w in words):
        return words
    source = [w["text"] for w in words]
    payload = json.dumps({
        "model": os.getenv("GROQ_TEXT_MODEL", "openai/gpt-oss-20b"),
        "messages": [
            {"role": "system", "content": (
                "Convert Hindi Devanagari words to natural, spoken Roman Hinglish. "
                "Do not translate or paraphrase. Keep English words unchanged. "
                "Return only a JSON object with key 'words' whose array has EXACTLY "
                "the same number of elements as input. Each input word becomes ONE "
                "Roman-script word at the same index. Preserve punctuation. "
                "Never output Devanagari. Example: [\"आज\",\"मैं\",\"तुम्हें\",\"बताऊंगा\"] "
                "becomes [\"Aaj\",\"main\",\"tumhe\",\"bataunga\"]."
            )},
            {"role": "user", "content": json.dumps({"words": source}, ensure_ascii=False)},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }, ensure_ascii=False).encode("utf-8")
    response = groq_post("/chat/completions", payload, "application/json", 120)
    try:
        roman = json.loads(response["choices"][0]["message"]["content"])["words"]
    except (KeyError, IndexError, TypeError, ValueError):
        raise ValueError("Roman Hinglish conversion returned invalid data") from None
    if (not isinstance(roman, list) or len(roman) != len(words) or
            any(not isinstance(w, str) or not w.strip() or
                re.search(r"[\u0900-\u097f]", w) or len(w.split()) != 1 for w in roman)):
        raise ValueError("Roman Hinglish word alignment failed; please retry")
    for original, converted in zip(words, roman):
        original["text"] = converted
    return words



def local_roman_hinglish_word(text):
    common = {
        "आज": "aaj", "मैं": "main", "मुझे": "mujhe", "मेरा": "mera",
        "मेरी": "meri", "मेरे": "mere", "हम": "hum", "हमारे": "hamare",
        "तुम": "tum", "तुम्हें": "tumhe", "तुम्हारा": "tumhara",
        "आप": "aap", "आपको": "aapko", "है": "hai", "हैं": "hain",
        "था": "tha", "थी": "thi", "थे": "the", "नहीं": "nahi",
        "हां": "haan", "हाँ": "haan", "और": "aur", "क्या": "kya",
        "क्यों": "kyon", "कैसे": "kaise", "कहाँ": "kahan",
        "कहां": "kahan", "यह": "yeh", "ये": "ye", "वह": "woh",
        "वो": "wo", "जो": "jo", "को": "ko", "से": "se", "की": "ki",
        "के": "ke", "का": "ka", "में": "mein", "पर": "par",
        "भी": "bhi", "तो": "to", "अब": "ab", "बस": "bas",
        "कर": "kar", "करो": "karo", "करना": "karna",
        "रहा": "raha", "रही": "rahi", "रहे": "rahe",
        "बता": "bata", "बताऊंगा": "bataunga", "बताऊँगा": "bataunga",
        "देखो": "dekho", "देखना": "dekhna", "बनाया": "banaya",
        "वीडियो": "video", "क्लिप": "clip", "फ्री": "free",
    }
    consonants = {
        "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "ng",
        "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "ny",
        "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n",
        "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n",
        "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m",
        "य": "y", "र": "r", "ल": "l", "व": "v", "श": "sh",
        "ष": "sh", "स": "s", "ह": "h", "ळ": "l",
        "क़": "q", "ख़": "kh", "ग़": "g", "ज़": "z", "ड़": "r",
        "ढ़": "rh", "फ़": "f", "य़": "y",
    }
    vowels = {
        "अ": "a", "आ": "aa", "इ": "i", "ई": "ee", "उ": "u",
        "ऊ": "oo", "ऋ": "ri", "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au",
    }
    marks = {
        "ा": "aa", "ि": "i", "ी": "ee", "ु": "u", "ू": "oo",
        "ृ": "ri", "े": "e", "ै": "ai", "ो": "o", "ौ": "au",
    }

    def convert(match):
        word = match.group(0)
        if word in common:
            return common[word]
        result = []
        i = 0
        while i < len(word):
            ch = word[i]
            if ch in consonants:
                next_ch = word[i + 1] if i + 1 < len(word) else ""
                if next_ch == "्":
                    result.append(consonants[ch])
                    i += 2
                    continue
                if next_ch in marks:
                    vowel = marks[next_ch]
                    if next_ch == "ा" and i + 2 >= len(word):
                        vowel = "a"
                    result.append(consonants[ch] + vowel)
                    i += 2
                    continue
                remaining = word[i + 1:]
                result.append(consonants[ch] + ("a" if any(c in consonants or c in vowels for c in remaining) else ""))
            elif ch in vowels:
                result.append(vowels[ch])
            elif ch in ("ं", "ँ"):
                result.append("n")
            elif ch == "ः":
                result.append("h")
            i += 1
        return "".join(result) or "word"

    return re.sub(r"[\u0900-\u097f]+", convert, text)


def safe_roman_hinglish(words):
    if not any(re.search(r"[\u0900-\u097f]", w["text"]) for w in words):
        return words
    try:
        return roman_hinglish(words)
    except Exception as exc:
        print("Roman Hinglish AI conversion unavailable; using local transliteration:", type(exc).__name__, flush=True)
        for item in words:
            item["text"] = local_roman_hinglish_word(item["text"])
        return words

def ass_color(style):
    rgb = {
        "Karaoke Yellow": "FFE14A", "Classic Yellow": "FFE14A",
        "Neon Red": "FF3648", "Neon Blue": "36DCFF",
        "Gaming Green": "5FFF65", "Gold Luxe": "FFD166",
        "Pink Glow": "FF74CC", "Purple Glow": "C58AFF",
        "Orange Punch": "FFAD49", "Subtitle Bar": "FFFFFF",
    }.get(style, "FFFFFF")
    return "&H00" + rgb[4:6] + rgb[2:4] + rgb[0:2]


def ass_time(value):
    cs = max(0, round(value * 100))
    h, remainder = divmod(cs, 360000)
    m, remainder = divmod(remainder, 6000)
    s, centiseconds = divmod(remainder, 100)
    return f"{h}:{m:02}:{s:02}.{centiseconds:02}"


def ass_text(value):
    return str(value).replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n", " ").replace("\r", " ")


def auto_caption_filter(folder, index, src, start, duration, width, height, style, size, position, jid):
    if not os.getenv("GROQ_API_KEY", "").strip():
        raise ValueError("Auto captions need GROQ_API_KEY in Render Environment")
    audio_path = folder / f"speech_{index}.mp3"
    try:
        update(jid, progress=f"Transcribing clip {index} with AI")
        run([
            "ffmpeg", "-y", "-ss", str(start), "-i", str(src), "-t", str(duration),
            "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", str(audio_path),
        ], 120)
        words = groq_transcribe(audio_path)
        update(jid, progress=f"Syncing captions for clip {index}")
        words = safe_roman_hinglish(words)
    finally:
        audio_path.unlink(missing_ok=True)

    color = ass_color(style)
    font_size = max(20, int(size * width / 1080))
    alignment = {"top": 8, "middle": 5, "bottom": 2}[position]
    margin = int(height * (0.12 if position == "top" else 0.17)) if position != "middle" else 0
    background = 3 if style == "Subtitle Bar" else 1
    outline = 4 if style in ("Hormozi Bold", "TikTok Bold", "High Contrast") else 3
    shadow = 2 if style in ("Shadow White", "Cinematic") else 0
    italic = -1 if style == "Cinematic" else 0
    font_name = os.getenv("CAPTION_ASS_FONT", "DejaVu Sans")
    lines = [
        "[Script Info]", "ScriptType: v4.00+", "WrapStyle: 2", "ScaledBorderAndShadow: yes",
        f"PlayResX: {width}", f"PlayResY: {height}", "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
        f"Style: Default,{font_name},{font_size},{color},&H00000000,&H00000000,&H99000000,-1,{italic},0,0,100,100,0,0,{background},{outline},{shadow},{alignment},20,20,{margin},1",
        "", "[Events]", "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]
    for item in words:
        begin = max(0, min(duration, item["start"]))
        end = max(begin + 0.08, min(duration, item["end"]))
        if begin >= duration:
            continue
        end = min(duration, end)
        if end <= begin:
            continue
        lines.append(f"Dialogue: 0,{ass_time(begin)},{ass_time(end)},Default,,0,0,0,,{ass_text(item['text'])}")
    path = folder / f"auto_{index}.ass"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if any(ch in str(path) for ch in "':,;[]\\"):
        raise ValueError("Caption file path contains unsupported characters")
    return f"ass=filename='{path}'"


def process(jid, source, mode, upload_path, clip_length, quality, style, text, size, position, ratio, crop, crop_x, crop_y):
    try:
        with PROCESS_LOCK:
            update(jid, status="processing", progress="Preparing source")
            folder = BASE / "uploads" / jid
            folder.mkdir(exist_ok=True)
            if upload_path:
                src = Path(upload_path)
            else:
                if os.getenv("ENABLE_YOUTUBE") != "1":
                    raise ValueError("YouTube importing is disabled. Please upload a video.")
                if not re.fullmatch(r"https://(www\.)?(youtube\.com/watch\?[^\s]+|youtu\.be/[\w-]+)", source):
                    raise ValueError("Unsupported YouTube URL")
                run([
                    "yt-dlp", "--no-playlist", "--max-filesize", "500M",
                    "--match-filter", "duration <= 3600", "-f", "bv*+ba/b",
                    "--merge-output-format", "mp4", "-o", str(folder / "source.%(ext)s"), source,
                ], 900)
                matches = list(folder.glob("source.*"))
                if not matches:
                    raise ValueError("Unable to import video")
                src = matches[0]
            update(jid, progress="Analyzing audio highlights")
            windows = select_windows(src, mode, clip_length)
            width, height, crf = get_dimensions(quality, ratio)
            clips = []
            for index, (start, duration) in enumerate(windows, 1):
                update(jid, progress=f"Preparing clip {index}/{len(windows)}")
                filename = f"{jid}_{index}.mp4"
                target = BASE / "outputs" / filename
                vf = (face_follow_filter(src, start, duration, width, height, jid, index)
                      if crop == "follow" else video_filter(width, height, crop, crop_x, crop_y))
                if style != "None" and not text.strip() and os.getenv("GROQ_API_KEY", "").strip():
                    cf = auto_caption_filter(folder, index, src, start, duration, width, height, style, size, position, jid)
                else:
                    cf = caption_filter(folder, index, width, style, text, size, position)
                update(jid, progress=f"Rendering clip {index}/{len(windows)}")
                if crop == "blur":
                    graph = vf + ("," + cf if cf else "") + "[outv]"
                    filter_args = ["-filter_complex", graph, "-map", "[outv]", "-map", "0:a?"]
                else:
                    filter_args = ["-vf", vf + ("," + cf if cf else "")]
                run([
                    "ffmpeg", "-y", "-ss", str(start), "-i", str(src), "-t", str(duration),
                    *filter_args, "-filter_threads", "1", "-filter_complex_threads", "1",
                    "-c:v", "libx264", "-threads", "1", "-preset", "veryfast", "-crf", str(crf),
                    "-c:a", "aac", "-movflags", "+faststart", str(target),
                ], 900)
                clips.append({"filename": filename, "start": start, "duration": duration, "ratio": ratio, "crop": crop})
            update(jid, status="done", progress="Complete", clips=json.dumps(clips))
    except Exception as exc:
        update(jid, status="failed", progress="Failed", error=str(exc)[:500])


@app.post("/jobs")
async def create_job(
    request: Request, mode: str = Form("best"), url: str = Form(""),
    video: UploadFile | None = File(None), clip_length: int = Form(30),
    quality: str = Form("1080p"), caption_style: str = Form("None"),
    caption_text: str = Form(""), caption_size: int = Form(48),
    caption_position: str = Form("bottom"), video_ratio: str = Form("9:16"),
    crop_mode: str = Form("fill"),
    crop_x: int = Form(50), crop_y: int = Form(50),
):
    found = require(request)
    if mode not in ("best", "funny") or not 5 <= clip_length <= 60:
        raise HTTPException(400, "Invalid clip settings")
    if quality not in QUALITY_PRESETS or video_ratio not in VIDEO_RATIOS or crop_mode not in CROP_MODES:
        raise HTTPException(400, "Invalid video settings")
    if not (0 <= crop_x <= 100 and 0 <= crop_y <= 100):
        raise HTTPException(400, "Crop position must be between 0 and 100")
    if caption_style not in CAPTION_STYLES or caption_position not in ("top", "middle", "bottom") or not 24 <= caption_size <= 96 or len(caption_text) > 160:
        raise HTTPException(400, "Invalid caption settings")
    has_video = bool(video and video.filename)
    if not has_video and not url.strip():
        raise HTTPException(400, "Upload a video or enter a URL")
    extension = Path(video.filename).suffix.lower() if has_video else ""
    if has_video and extension not in (".mp4", ".mov", ".mkv", ".webm"):
        raise HTTPException(400, "Unsupported file type")
    if not has_video and os.getenv("ENABLE_YOUTUBE") != "1":
        raise HTTPException(400, "YouTube importing is currently disabled")
    with db() as c:
        limit = int(c.execute("SELECT value FROM settings WHERE key='daily_limit'").fetchone()["value"])
        count = c.execute("SELECT COUNT(*) FROM jobs WHERE user_id=? AND date(created_at)=date('now')", (found["id"],)).fetchone()[0]
        if count >= limit and found["role"] != "admin":
            raise HTTPException(429, "Daily limit reached")
        jid = uuid.uuid4().hex
        c.execute(
            "INSERT INTO jobs(id,user_id,source,mode,status,progress) VALUES (?,?,?,?,?,?)",
            (jid, found["id"], "upload" if has_video else url[:300], mode, "queued", "Queued"),
        )
    path = None
    if has_video:
        path = BASE / "uploads" / (jid + extension)
        total = 0
        try:
            with open(path, "wb") as output:
                while True:
                    chunk = await video.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_UPLOAD:
                        raise HTTPException(413, "Maximum file size is 500 MB")
                    output.write(chunk)
        except Exception:
            path.unlink(missing_ok=True)
            with db() as c:
                c.execute("DELETE FROM jobs WHERE id=?", (jid,))
            raise
    threading.Thread(
        target=process,
        args=(jid, url.strip(), mode, str(path) if path else None, clip_length, quality,
              caption_style, caption_text, caption_size, caption_position, video_ratio, crop_mode, crop_x, crop_y),
        daemon=True,
    ).start()
    return RedirectResponse("/dashboard", 303)


@app.get("/api/jobs")
def api_jobs(request: Request):
    found = require(request)
    with db() as c:
        rows = c.execute("SELECT id,status,progress,clips,error FROM jobs WHERE user_id=? ORDER BY created_at DESC LIMIT 30", (found["id"],)).fetchall()
    return JSONResponse([dict(row) for row in rows])


@app.get("/clips/{filename}")
def clip(request: Request, filename: str):
    found = require(request)
    if not re.fullmatch(r"[0-9a-f]{32}_[1-3]\.mp4", filename):
        raise HTTPException(404)
    with db() as c:
        job = c.execute("SELECT user_id FROM jobs WHERE id=?", (filename.split("_")[0],)).fetchone()
    if not job or (job["user_id"] != found["id"] and found["role"] != "admin"):
        raise HTTPException(403)
    target = BASE / "outputs" / filename
    if not target.is_file():
        raise HTTPException(404)
    return FileResponse(target, media_type="video/mp4", filename=filename)


@app.get("/health")
def health():
    return {"ok": True, "service": "aadilclips.ai"}
