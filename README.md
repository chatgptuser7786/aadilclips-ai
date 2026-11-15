# aadilclips.ai | Independent clipping MVP

Working **FastAPI + SQLite + FFmpeg** prototype with mobile-responsive frontend, private role-checked admin dashboard, user signup/login, authorized file upload, highlight scoring, vertical MP4 export, job history, daily limits, and the supplied original brand logo.

## Run locally

Install Python 3.11+ and FFmpeg (`ffmpeg -version` must work). From this directory:

```bash
python -m venv .venv
# Windows PowerShell: .venv\\Scripts\\Activate.ps1
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Set environment variables **before first startup**. Example PowerShell:

```powershell
$env:SESSION_SECRET = (python -c "import secrets;print(secrets.token_urlsafe(48))")
$env:ADMIN_EMAIL = "YOUR_REAL_EMAIL@example.com"
$env:ADMIN_PASSWORD = "YOUR_OWN_STRONG_UNIQUE_PASSWORD"
uvicorn app:app --host 127.0.0.1 --port 8000
```

Visit http://127.0.0.1:8000 . Log in using the admin email/password you set, then open `/admin`. Admin credentials are never embedded in the frontend. The admin email is a login identifier, not an automatically created email inbox. Do not use a password previously posted in chat.

## What works / what doesn't

- **Works**: responsive site, original logo, About section and Instagram link, registration/login, admin permissions, admin About editing, daily quota, blocking regular users, uploaded video to 9:16 MP4, job history and downloads.
- **Experimental**: "funny" and "best" modes use audio activity and peaks as a simple highlight heuristic. They **do not** understand jokes, facial expressions, speech, visual scenes, or predict virality. Both modes are currently very similar.
- **YouTube URL**: disabled by default. `ENABLE_YOUTUBE=1` enables an optional yt-dlp import attempt for authorized content, but may fail due to platform restrictions. You must comply with YouTube terms and copyright permissions. Prefer uploading owned videos.
- **Not included**: semantic multimodal AI moment ranking, auto subtitles, face tracking, editor timeline, payments, 2FA, email verification, password reset, distributed processing, secure cloud hosting, and a registered custom domain. These require additional implementation.
- **Scaling**: jobs currently run in daemon threads. Restarting the server interrupts processing. For production use, migrate to a persistent job queue and separate worker; store videos in object storage; use Postgres, backups, email verification, rate limits, CSRF protection, HTTPS, and 2FA for admin. Do **not** expose this MVP directly to the public internet.

## Deployment

A real public release needs a server with FFmpeg and enough CPU/disk, persistent storage, HTTPS, DNS control of `aadilclips.ai`, secure environment secrets, and a job queue. Static-only hosting such as GitHub Pages cannot run this video backend. The domain has not been purchased or connected here.

## Clip editor update
- Select duration 5–60 seconds (default 30), 720p/1080p/1440p exports, and 20 caption style names plus None.
- Caption text is **manual static overlay**, not automatic subtitles, karaoke timing, or actual distinct animated style engines. Several presets differ only by color or background in this MVP.
- FFmpeg must be compiled with `drawtext` and `libfreetype`; configure `CAPTION_FONT_FILE` to an installed TrueType font.
- Higher output resolution does not restore detail absent in the input. Longer jobs require CPU/disk.
