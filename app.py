import os, sqlite3, secrets, hashlib, hmac, time, json, subprocess, threading, re, uuid
from pathlib import Path
from datetime import datetime
from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
import numpy as np

BASE=Path(__file__).parent
for d in ('uploads','outputs'): (BASE/d).mkdir(exist_ok=True)
DB=BASE/'clips.db'
SECRET=os.environ.get('SESSION_SECRET', '')
if not SECRET: SECRET=secrets.token_urlsafe(32); print('WARNING: SESSION_SECRET not set. Sessions reset on restart.')
app=FastAPI(title='aadilclips.ai')
app.add_middleware(SessionMiddleware,secret_key=SECRET,https_only=os.getenv('HTTPS_ONLY')=='1',same_site='lax')
tpl=Jinja2Templates(directory=str(BASE/'templates'))
tpl.env.filters['from_json']=json.loads
app.mount('/static',StaticFiles(directory=str(BASE/'static')),name='static')
MAX_UPLOAD=500*1024*1024
CAPTION_STYLES = ['None','Hormozi Bold','Karaoke Yellow','Neon Red','Neon Blue','Minimal White','Classic Yellow','Gaming Green','Comic Pop','Cinematic','TikTok Bold','Instagram Clean','Shadow White','Outline Black','Gold Luxe','Pink Glow','Purple Glow','Orange Punch','Retro Pixel','Subtitle Bar','High Contrast']
QUALITY_PRESETS = {'720p': (720,1280,24), '1080p': (1080,1920,20), '1440p': (1440,2560,18)}


def db():
 c=sqlite3.connect(DB,timeout=30); c.row_factory=sqlite3.Row; return c

def hashpw(password,salt=None):
 salt=salt or secrets.token_hex(16)
 return salt+':'+hashlib.scrypt(password.encode(),salt=bytes.fromhex(salt),n=16384,r=8,p=1).hex()
def verify(password,stored):
 try:
  salt,_=stored.split(':',1)
  return hmac.compare_digest(hashpw(password,salt),stored)
 except Exception: return False

def init():
 with db() as c:
  c.execute('CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, password TEXT NOT NULL, role TEXT NOT NULL DEFAULT "user", blocked INTEGER DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP)')
  c.execute('CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, source TEXT, mode TEXT, status TEXT, progress TEXT, clips TEXT DEFAULT "[]", error TEXT DEFAULT "", created_at TEXT DEFAULT CURRENT_TIMESTAMP)')
  c.execute('CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT)')
  c.execute('INSERT OR IGNORE INTO settings VALUES (?,?)',('about','Clipping My Way to Success | Whop | Content Rewards | Real Results, Real Journey'))
  c.execute('INSERT OR IGNORE INTO settings VALUES (?,?)',('daily_limit','3'))
  admin=os.getenv('ADMIN_EMAIL','').strip().lower(); pw=os.getenv('ADMIN_PASSWORD','')
  if admin and pw:
   if len(pw)<14: raise RuntimeError('ADMIN_PASSWORD must have at least 14 characters')
   row=c.execute('SELECT id FROM users WHERE email=?',(admin,)).fetchone()
   if row: c.execute('UPDATE users SET role="admin" WHERE id=?',(row['id'],))
   else: c.execute('INSERT INTO users(email,password,role) VALUES (?,?,"admin")',(admin,hashpw(pw)))
init()

def user(request):
 uid=request.session.get('uid')
 if not uid:return None
 with db() as c: row=c.execute('SELECT id,email,role,blocked FROM users WHERE id=?',(uid,)).fetchone()
 return row if row and not row['blocked'] else None

def page(request,name,**kwargs): return tpl.TemplateResponse(request,name,{'user':user(request),**kwargs})
def require(request,admin=False):
 u=user(request)
 if not u: raise HTTPException(401,'Please sign in')
 if admin and u['role']!='admin': raise HTTPException(403,'Admin only')
 return u

@app.get('/',response_class=HTMLResponse)
def home(request:Request):
 with db() as c: about=c.execute('SELECT value FROM settings WHERE key="about"').fetchone()['value']
 return page(request,'index.html',about=about)

@app.get('/login',response_class=HTMLResponse)
def login_page(request:Request):return page(request,'auth.html',register=False)
@app.get('/signup',response_class=HTMLResponse)
def signup_page(request:Request):return page(request,'auth.html',register=True)
@app.post('/signup')
def signup(request:Request,email:str=Form(...),password:str=Form(...)):
 email=email.strip().lower()
 if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+',email) or len(password)<12: return page(request,'auth.html',register=True,error='Valid email and 12+ character password required')
 try:
  with db() as c:
   cur=c.execute('INSERT INTO users(email,password) VALUES (?,?)',(email,hashpw(password))); uid=cur.lastrowid
 except sqlite3.IntegrityError:return page(request,'auth.html',register=True,error='Email already registered')
 request.session['uid']=uid;return RedirectResponse('/dashboard',303)
@app.post('/login')
def login(request:Request,email:str=Form(...),password:str=Form(...)):
 with db() as c: u=c.execute('SELECT * FROM users WHERE email=?',(email.strip().lower(),)).fetchone()
 if not u or u['blocked'] or not verify(password,u['password']):return page(request,'auth.html',register=False,error='Invalid credentials')
 request.session['uid']=u['id'];return RedirectResponse('/admin' if u['role']=='admin' else '/dashboard',303)
@app.post('/logout')
def logout(request:Request):request.session.clear();return RedirectResponse('/',303)
@app.get('/dashboard',response_class=HTMLResponse)
def dashboard(request:Request):
 u=require(request)
 with db() as c: jobs=c.execute('SELECT * FROM jobs WHERE user_id=? ORDER BY created_at DESC LIMIT 30',(u['id'],)).fetchall()
 return page(request,'dashboard.html',jobs=jobs,caption_styles=CAPTION_STYLES)
@app.get('/admin',response_class=HTMLResponse)
def admin(request:Request):
 u=require(request,True)
 with db() as c:
  users=c.execute('SELECT id,email,role,blocked,created_at FROM users ORDER BY id DESC').fetchall()
  jobs=c.execute('SELECT jobs.*,users.email FROM jobs JOIN users ON users.id=jobs.user_id ORDER BY created_at DESC LIMIT 40').fetchall()
  settings={r['key']:r['value'] for r in c.execute('SELECT * FROM settings')}
 return page(request,'admin.html',users=users,jobs=jobs,settings=settings)
@app.post('/admin/settings')
def settings(request:Request,about:str=Form(...),daily_limit:int=Form(...)):
 require(request,True)
 if not 1<=daily_limit<=100:raise HTTPException(400,'Invalid limit')
 with db() as c:
  c.execute('UPDATE settings SET value=? WHERE key="about"',(about[:1000],))
  c.execute('UPDATE settings SET value=? WHERE key="daily_limit"',(str(daily_limit),))
 return RedirectResponse('/admin',303)
@app.post('/admin/users/{uid}/toggle')
def toggle(request:Request,uid:int):
 u=require(request,True)
 if uid==u['id']:raise HTTPException(400,'Cannot block yourself')
 with db() as c:c.execute('UPDATE users SET blocked=1-blocked WHERE id=? AND role!="admin"',(uid,))
 return RedirectResponse('/admin',303)

def run(cmd,timeout=1800):return subprocess.run(cmd,capture_output=True,text=True,timeout=timeout,check=True)
def update(jid,**fields):
 with db() as c:
  for k,v in fields.items():c.execute(f'UPDATE jobs SET {k}=? WHERE id=?',(v,jid))

def select_windows(path,mode,clip_length=30,max_clips=3):
 # Baseline signal-based highlight scoring. Not a semantic funny-moment detector.
 import wave
 pcm=BASE/'uploads'/(uuid.uuid4().hex+'.wav')
 try:
  run(['ffmpeg','-y','-i',str(path),'-vn','-ac','1','-ar','8000','-acodec','pcm_s16le',str(pcm)],timeout=600)
  with wave.open(str(pcm),'rb') as f: samples=np.frombuffer(f.readframes(f.getnframes()),dtype=np.int16).astype(np.float32)/32768
  duration=len(samples)/8000
  if duration<5:raise ValueError('Video must be at least 5 seconds')
  length=min(clip_length,max(5,int(duration)))
  if duration<=length:return [(0,length)]
  windows=[]
  for start in range(0,max(1,int(duration-length)+1),5):
   chunk=samples[start*8000:int((start+length)*8000)]
   if len(chunk)<8000:continue
   rms=float(np.sqrt(np.mean(chunk**2)))
   peaks=float(np.percentile(np.abs(chunk[::40]),95))
   score=(0.65*rms+0.35*peaks) if mode=='funny' else (0.5*rms+0.5*peaks)
   windows.append((score,start))
  chosen=[]
  for _,start in sorted(windows,reverse=True):
   if all(abs(start-s)>=length*0.8 for s,_ in chosen):chosen.append((start,length))
   if len(chosen)>=max_clips:break
  return sorted(chosen) or [(0,length)]
 finally:pcm.unlink(missing_ok=True)

def process(jid,source,mode,upload_path=None,clip_length=30,quality="1080p",caption_style="None",caption_text="",caption_size=48,caption_position="bottom"):
 try:
  update(jid,status='processing',progress='Preparing source')
  folder=BASE/'uploads'/jid;folder.mkdir(exist_ok=True)
  if upload_path: src=Path(upload_path)
  else:
   if os.getenv('ENABLE_YOUTUBE')!='1':raise ValueError('YouTube URL importing is disabled. Upload a video you own or have permission to use.')
   if not re.fullmatch(r'https://(www\.)?(youtube\.com/watch\?[^\s]+|youtu\.be/[\w-]+)',source):raise ValueError('Only YouTube URLs are supported')
   run(['yt-dlp','--no-playlist','--max-filesize','500M','--match-filter','duration <= 3600','-f','bv*+ba/b','--merge-output-format','mp4','-o',str(folder/'source.%(ext)s'),source],timeout=900)
   matches=list(folder.glob('source.*'))
   if not matches:raise ValueError('Video could not be imported')
   src=matches[0]
  update(jid,progress='Analyzing audio highlights')
  windows=select_windows(src,mode,clip_length=clip_length)
  out=[]
  for idx,(start,length) in enumerate(windows,1):
   update(jid,progress=f'Rendering clip {idx}/{len(windows)}')
   filename=f'{jid}_{idx}.mp4'; target=BASE/'outputs'/filename
   # Center crop to vertical 9:16; no face tracking in this MVP.
   width,height,crf=QUALITY_PRESETS[quality]
   vf=f'scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1'
   if caption_style!='None' and caption_text.strip():
    # User-authored overlay text. Automatic transcription is not provided by this MVP.
    colors={'Karaoke Yellow':'yellow','Classic Yellow':'yellow','Neon Red':'red','Neon Blue':'cyan','Gaming Green':'lime','Gold Luxe':'gold','Pink Glow':'hotpink','Purple Glow':'violet','Orange Punch':'orange'}
    color=colors.get(caption_style,'white')
    y={'top':'h*0.15','middle':'(h-text_h)/2','bottom':'h*0.78'}[caption_position]
    # drawtext textfile avoids FFmpeg filter injection and quote escaping issues.
    caption_file=folder/f'caption_{idx}.txt'
    caption_file.write_text(caption_text,encoding='utf-8')
    fontfile=os.getenv('CAPTION_FONT_FILE','/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf')
    if not Path(fontfile).is_file(): raise ValueError('Caption font unavailable. Set CAPTION_FONT_FILE to a valid .ttf path')
    # Quote filter paths safely (reject special characters in configured font path).
    if any(x in str(fontfile) for x in "':,;[]\\") or any(x in str(caption_file) for x in "':,;[]\\"):
     raise ValueError('Caption path contains unsupported characters')
    fontsize=max(20,int(caption_size*width/1080))
    box=':box=1:boxcolor=black@0.65:boxborderw=14' if caption_style=='Subtitle Bar' else ''
    vf+=f",drawtext=fontfile='{fontfile}':textfile='{caption_file}':fontcolor={color}:fontsize={fontsize}:borderw=3:bordercolor=black:x=(w-text_w)/2:y={y}{box}"
   run(['ffmpeg','-y','-ss',str(start),'-i',str(src),'-t',str(length),'-vf',vf,'-c:v','libx264','-preset','veryfast','-crf',str(crf),'-c:a','aac','-movflags','+faststart',str(target)],timeout=600)
   out.append({'filename':filename,'start':start,'duration':length})
  update(jid,status='done',progress='Complete',clips=json.dumps(out))
 except Exception as e:update(jid,status='failed',progress='Failed',error=str(e)[:500])

@app.post('/jobs')
async def create_job(request:Request,mode:str=Form('best'),url:str=Form(''),video:UploadFile|None=File(None),clip_length:int=Form(30),quality:str=Form('1080p'),caption_style:str=Form('None'),caption_text:str=Form(''),caption_size:int=Form(48),caption_position:str=Form('bottom')):
 u=require(request)
 if mode not in ('funny','best'):raise HTTPException(400,'Invalid mode')
 if not 5<=clip_length<=60:raise HTTPException(400,'Clip duration must be 5–60 seconds')
 if quality not in QUALITY_PRESETS:raise HTTPException(400,'Invalid quality')
 if caption_style not in CAPTION_STYLES:raise HTTPException(400,'Invalid caption style')
 if caption_position not in ('top','middle','bottom') or not 24<=caption_size<=96 or len(caption_text)>160:raise HTTPException(400,'Invalid caption options')
 if not video and not url.strip():raise HTTPException(400,'Provide video or URL')
 with db() as c:
  limit=int(c.execute('SELECT value FROM settings WHERE key="daily_limit"').fetchone()['value'])
  count=c.execute('SELECT COUNT(*) FROM jobs WHERE user_id=? AND date(created_at)=date("now")',(u['id'],)).fetchone()[0]
  if count>=limit and u['role']!='admin':raise HTTPException(429,'Daily limit reached')
  jid=uuid.uuid4().hex
  c.execute('INSERT INTO jobs(id,user_id,source,mode,status,progress) VALUES (?,?,?,?,?,?)',(jid,u['id'],('upload' if video and video.filename else url[:300]),mode,'queued','Queued'))
 path=None
 if video and video.filename:
  ext=Path(video.filename).suffix.lower()
  if ext not in ('.mp4','.mov','.mkv','.webm'):raise HTTPException(400,'Unsupported video format')
  path=BASE/'uploads'/(jid+ext);total=0
  with open(path,'wb') as f:
   while chunk:=await video.read(1024*1024):
    total+=len(chunk)
    if total>MAX_UPLOAD:
     path.unlink(missing_ok=True)
     with db() as c:c.execute('DELETE FROM jobs WHERE id=?',(jid,))
     raise HTTPException(413,'File exceeds 500 MB')
    f.write(chunk)
 threading.Thread(target=process,args=(jid,url.strip(),mode,str(path) if path else None,clip_length,quality,caption_style,caption_text,caption_size,caption_position),daemon=True).start()
 return RedirectResponse('/dashboard',303)
@app.get('/api/jobs')
def api_jobs(request:Request):
 u=require(request)
 with db() as c: rows=c.execute('SELECT id,status,progress,clips,error FROM jobs WHERE user_id=? ORDER BY created_at DESC LIMIT 30',(u['id'],)).fetchall()
 return JSONResponse([dict(r) for r in rows])
@app.get('/clips/{filename}')
def clip(request:Request,filename:str):
 u=require(request)
 if not re.fullmatch(r'[0-9a-f]{32}_[1-3]\.mp4',filename):raise HTTPException(404)
 jid=filename.split('_')[0]
 with db() as c:job=c.execute('SELECT user_id FROM jobs WHERE id=?',(jid,)).fetchone()
 if not job or (job['user_id']!=u['id'] and u['role']!='admin'):raise HTTPException(403)
 p=BASE/'outputs'/filename
 if not p.exists():raise HTTPException(404)
 return FileResponse(p,media_type='video/mp4',filename=filename)
@app.get('/health')
def health():return {'ok':True,'service':'aadilclips.ai'}
