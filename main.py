from fastapi import FastAPI, Request, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
import sqlite3, hashlib, io, requests, jwt
from PIL import Image

# ---------------- Config ----------------
DATABASE = "users.db"
JWT_SECRET = "mi_secreto_superseguro"
JWT_ALGORITHM = "HS256"
ACCOUNT_TOKEN = "K5hNBTvc7hgOa1fCoIEabxTbEWA8GeuD"
WT = "4fd6sg89d7s6"
HEADERS_BASE = {"Authorization": f"Bearer {ACCOUNT_TOKEN}"}

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)

# ---------------- DB ----------------
def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    db = get_db()
    db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS folders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            content_id TEXT NOT NULL UNIQUE
        )
    """)
    # Ensure 'source' column exists to mark external sources (e.g. 'pixeldrain')
    try:
        db.execute("ALTER TABLE folders ADD COLUMN source TEXT DEFAULT 'gofile'")
    except Exception:
        # if column already exists or SQLite doesn't allow, ignore
        pass
    db.commit()
    db.close()

init_db()

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()

def verify_password(password: str, password_hash: str) -> bool:
    return hash_password(password) == password_hash

# ---------------- Schemas ----------------
class UserSchema(BaseModel):
    username: str
    password: str

class FolderSchema(BaseModel):
    name: str
    content_id: str
    source: str = 'gofile'

# ---------------- JWT ----------------
def create_jwt(user_id: int):
    payload = {"user_id": user_id}
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return token

def decode_jwt(token: str):
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except:
        raise HTTPException(status_code=401, detail="Token inválido")

def auth_required(authorization: str = Header(None)):
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header requerido")
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Formato Bearer inválido")
    token = authorization.split(" ", 1)[1]
    return decode_jwt(token)

# ---------------- Endpoints Auth ----------------
@app.post("/register")
def register(user: UserSchema):
    db = get_db()
    try:
        db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                   (user.username, hash_password(user.password)))
        db.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="Usuario ya existe")
    finally:
        db.close()
    return {"message": "Usuario registrado"}

@app.post("/login")
def login(user: UserSchema):
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE username=?", (user.username,)).fetchone()
    db.close()
    if not row or not verify_password(user.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Usuario o contraseña incorrectos")
    token = create_jwt(row["id"])
    return {"message": "Inicio sesión exitoso", "token": token}

# ---------------- Folders (protegido) ----------------
@app.get("/folders")
def list_folders(auth=Depends(auth_required)):
    db = get_db()
    folders = db.execute("SELECT * FROM folders").fetchall()
    db.close()
    return [{"id": f["id"], "name": f["name"], "content_id": f["content_id"], "source": f["source"] if "source" in f.keys() else "gofile"} for f in folders]

@app.post("/folders")
def add_folder(folder: FolderSchema, auth=Depends(auth_required)):
    db = get_db()
    try:
        # include source (e.g. 'gofile' or 'pixeldrain')
        db.execute("INSERT INTO folders (name, content_id, source) VALUES (?, ?, ?)", (folder.name, folder.content_id, folder.source))
        db.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="El content_id ya existe")
    finally:
        db.close()
    return {"message": "Folder registrado correctamente"}

# ---------------- Pixeldrain support ----------------
PIXELDRAIN_ROOT = "https://pixeldrain.com"


def pixeldrain_get_json(path, params=None):
    # try direct and /api/ prefixed paths if needed
    url = PIXELDRAIN_ROOT + path
    resp = requests.get(url, params=params)
    if resp.status_code == 404 and path.startswith('/api/'):
        # try without /api/
        url2 = PIXELDRAIN_ROOT + path.replace('/api/', '/')
        resp = requests.get(url2, params=params)
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=f"Pixeldrain error {resp.status_code}")
    try:
        return resp.json()
    except Exception:
        raise HTTPException(status_code=500, detail="Invalid JSON from pixeldrain")


@app.get("/pixeldrain/resolve")
def pixeldrain_resolve(content_id: str):
    """Try to resolve a pixeldrain id: list or file. Returns {type: 'list'|'file', data: ...} """
    # try list

    # try list endpoints
    try:
        for p in (f"/api/list/{content_id}", f"/list/{content_id}"):
            try:
                data = pixeldrain_get_json(p)
                if data and data.get('files'):
                    return {"type": "list", "data": data}
            except HTTPException:
                continue
    except Exception:
        pass

    # try file info endpoints
    try:
        for p in (f"/api/file/{content_id}/info", f"/file/{content_id}/info"):
            try:
                data = pixeldrain_get_json(p)
                return {"type": "file", "data": data}
            except HTTPException:
                continue
    except Exception:
        pass

    raise HTTPException(status_code=404, detail="Pixeldrain id not found")


@app.get("/pixeldrain/info")
def pixeldrain_info(file_id: str):
    return pixeldrain_get_json(f"/api/file/{file_id}/info")


@app.get("/pixeldrain/thumbnail")
def pixeldrain_thumbnail(file_id: str, width: int = 128, height: int = 128):
    # Stream the pixeldrain thumbnail
    url = f"{PIXELDRAIN_ROOT}/file/{file_id}/thumbnail"
    params = {"width": width, "height": height}
    resp = requests.get(url, params=params, stream=True)
    if resp.status_code in (301, 302) and resp.headers.get('location'):
        # follow redirect
        return StreamingResponse(requests.get(resp.headers['location'], stream=True).iter_content(8192), media_type="image/png")
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail="Thumbnail not available")
    return StreamingResponse(resp.iter_content(8192), media_type=resp.headers.get('Content-Type', 'image/png'))


@app.get("/pixeldrain/file")
def pixeldrain_file(file_id: str):
    # Stream file content from pixeldrain directly; pixeldrain serves ranges itself if needed
    url = f"{PIXELDRAIN_ROOT}/file/{file_id}"
    resp = requests.get(url, stream=True)
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail="File not available")

    def generate():
        for chunk in resp.iter_content(8192):
            yield chunk

    return StreamingResponse(generate(), media_type=resp.headers.get('Content-Type', 'application/octet-stream'))

# ---------------- Contenido abierto ----------------
def get_content_sync(content_id, content_filter="", page=1, page_size=1000, sort_field="createTime", sort_direction=-1, password=""):
    url = f"https://api.gofile.io/contents/{content_id}"
    headers = {"Authorization": f"Bearer {ACCOUNT_TOKEN}"}
    params = {"wt": WT, "contentFilter": content_filter, "page": page, "pageSize": page_size, "sortField": sort_field, "sortDirection": sort_direction}
    if password:
        params["password"] = password
    resp = requests.get(url, params=params, headers=headers)
    if resp.status_code != 200:
        raise Exception(f"HTTP {resp.status_code}")
    data = resp.json()
    if data["status"] not in ("ok", "error-notFound"):
        raise Exception(f"API status {data['status']}")
    return data

@app.get("/get_content")
def get_content(content_id: str, page_size: int=1000):
    data = get_content_sync(content_id, page_size=page_size)
    # Redirige links a proxy
    for _, item in (data.get("data", {}).get("children") or {}).items():
        item["link_original"] = item.get("link")
        if not (item.get("type") == "folder"):
            item["link"] = f"http://40.233.25.130:8000/proxy?content_id={item['id']}"
    return data

import os

CACHE_DIR = "cache"
os.makedirs(CACHE_DIR, exist_ok=True)

def get_cache_path(content_id: str, max_width: int = None, max_height: int = None, ext="jpg"):
    """Genera la ruta en cache para una imagen redimensionada"""
    size_tag = f"{max_width or 'orig'}x{max_height or 'orig'}"
    return os.path.join(CACHE_DIR, f"{content_id}_{size_tag}.{ext}")

@app.get("/proxy")
def proxy_media(content_id: str, max_width: int = None, max_height: int = None, mode: str = None, request: Request = None):
    file_info = get_content_sync(content_id)["data"]
    media_url = file_info["link"]
    mimetype = file_info.get("mimetype", "application/octet-stream")
    is_image = mimetype.startswith("image")
    headers = HEADERS_BASE.copy()

    # ---------- Si es imagen y hay dimensiones o modo ----------
    # Preset resolutions (width, height)
    PRESETS = {
        'thumbnail': (480, 480),     # max 480 per side
        'small': (1280, 720),        # HD
        'medium': (1920, 1080),      # Full HD
        'large': (2560, 1440),       # 2K
        '4k': (3840, 2160),          # 4K (also considered for nearest-match)
    }

    def choose_target_size(max_w, max_h, mode):
        # If explicit mode requested
        if mode:
            m = mode.lower()
            if m in ('original', 'orig'):
                return None  # indicates original (no resize)
            if m in PRESETS:
                return PRESETS[m]
        # If max requested, choose nearest preset by max dimension
        if max_w or max_h:
            requested = max(max_w or 0, max_h or 0)
            # consider all presets including 4k
            candidates = []
            for k, (w,h) in PRESETS.items():
                candidates.append((k, max(w,h)))
            # find closest
            candidates.sort(key=lambda c: abs(c[1] - requested))
            best = candidates[0][0]
            return PRESETS[best]
        return None

    if is_image:
        # Determine target size (None means original
        target = choose_target_size(max_width, max_height, mode)
        # If target is None => serve original
        if target is None:
            # Fall through to streaming original below
            pass
        else:
            target_w, target_h = target
            # Enforce thumbnail bounds
            if mode and mode.lower() == 'thumbnail':
                target_w = min(target_w, 480)
                target_h = min(target_h, 480)

            ext = (file_info.get("name", "").split(".")[-1] or "jpg").lower()
            cache_path = get_cache_path(content_id, target_w, target_h, ext)

            # Si ya existe en cache → devolver directo
            if os.path.exists(cache_path):
                return StreamingResponse(open(cache_path, "rb"), media_type=mimetype)

            # Si no existe → descargar, redimensionar y guardar
            resp = requests.get(media_url, headers=headers, stream=True)
            image = Image.open(io.BytesIO(resp.content))
            orig_w, orig_h = image.size

            # Redimensionar manteniendo proporción usando target box
            image.thumbnail((target_w or orig_w, target_h or orig_h), Image.LANCZOS)

            # Guardar en caché
            image.save(cache_path, format=image.format or "JPEG")

            return StreamingResponse(open(cache_path, "rb"), media_type=mimetype)

    # ---------- Para video o imágenes sin resize ----------
    # If this is a video, forward Range and propagate partial responses
    if mimetype.startswith('video'):
        # Forward Range header (or request from start) to upstream
        range_header = None
        if request is not None:
            range_header = request.headers.get('range')
        upstream_headers = headers.copy()
        if range_header:
            upstream_headers['Range'] = range_header
        else:
            upstream_headers['Range'] = 'bytes=0-'

        resp = requests.get(media_url, headers=upstream_headers, stream=True)
        if resp.status_code not in (200, 206):
            raise HTTPException(status_code=resp.status_code, detail="File not available")

        out_headers = {}
        if 'content-range' in resp.headers:
            out_headers['Content-Range'] = resp.headers['content-range']
        if 'content-length' in resp.headers:
            out_headers['Content-Length'] = resp.headers['content-length']
        out_headers['Accept-Ranges'] = resp.headers.get('accept-ranges', 'bytes')
        content_type = resp.headers.get('Content-Type', resp.headers.get('content-type', 'application/octet-stream'))

        def generate():
            try:
                for chunk in resp.iter_content(8192):
                    if not chunk:
                        continue
                    yield chunk
            finally:
                try:
                    resp.close()
                except Exception:
                    pass

        return StreamingResponse(generate(), status_code=resp.status_code, headers=out_headers, media_type=content_type)

    # fallback: stream upstream content for non-image, non-video
    resp = requests.get(media_url, headers=headers, stream=True)

    def generate():
        for chunk in resp.iter_content(8192):
            yield chunk

    return StreamingResponse(generate(), media_type=mimetype)
