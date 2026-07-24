"""
SERVIDOR — Centro de Operaciones
Dashboard de tareas multi-usuario con roles (admin / editor).
Backend: FastAPI + SQLite. Diseñado para desplegarse en EasyPanel (Docker).
"""

import io
import os
import re
import json
import shutil
import tarfile
import tempfile
import threading
import smtplib
import ssl
from email.message import EmailMessage
import time
import sqlite3
import secrets
import hashlib
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, HTTPException, Header, Depends, UploadFile, File, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional

# ---------------------------------------------------------------- configuración

DATA_DIR = os.environ.get("DATA_DIR", "/data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "servidor.db")
UPLOADS_DIR = os.path.join(DATA_DIR, "uploads")
os.makedirs(UPLOADS_DIR, exist_ok=True)

STATUSES = ["pendiente", "revision", "terminado"]
PRIORITIES = ["alta", "media", "baja"]
ROLES = ["admin", "editor"]

app = FastAPI(title="SERVIDOR — Centro de Operaciones")


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            display_name TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'editor',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            channel TEXT DEFAULT '',
            assigned_to INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            due_date TEXT DEFAULT '',
            priority TEXT NOT NULL DEFAULT 'media',
            status TEXT NOT NULL DEFAULT 'pendiente',
            delivery_note TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS task_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            label TEXT NOT NULL,
            url TEXT NOT NULL,
            kind TEXT DEFAULT 'archivo',
            added_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            drive_folder_id TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );
        -- pares clave/valor: credenciales OAuth, refresh token, id de carpeta raíz
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        -- sesiones de subida directa navegador → Drive (reanudables)
        CREATE TABLE IF NOT EXISTS uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            filename TEXT NOT NULL,
            size INTEGER NOT NULL,
            mime TEXT DEFAULT '',
            session_uri TEXT NOT NULL,
            drive_file_id TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pendiente',
            added_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL
        );
        -- cuentas de Google Drive ("servidores" de almacenamiento): se agregan más al llenarse
        CREATE TABLE IF NOT EXISTS drive_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT DEFAULT '',
            refresh_token TEXT NOT NULL,
            root_folder_id TEXT DEFAULT '',
            active INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        -- carpeta de cada canal POR cuenta (la misma estructura se replica en cada servidor)
        CREATE TABLE IF NOT EXISTS drive_channel_folders (
            account_id INTEGER NOT NULL,
            channel_name TEXT NOT NULL,
            folder_id TEXT NOT NULL,
            PRIMARY KEY (account_id, channel_name)
        );
        -- calendario: anotaciones por día (con link opcional)
        CREATE TABLE IF NOT EXISTS calendar_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,          -- YYYY-MM-DD
            title TEXT NOT NULL,
            url TEXT DEFAULT '',
            done INTEGER DEFAULT 0,
            created_by INTEGER,
            created_at TEXT NOT NULL
        );
        """
    )
    # migración: estados antiguos (4) → nuevos (3)
    ts = datetime.now(timezone.utc).isoformat()
    conn.execute("UPDATE tasks SET status='pendiente' WHERE status='en_progreso'")
    conn.execute("UPDATE tasks SET status='revision' WHERE status='entregada'")
    conn.execute("UPDATE tasks SET status='terminado' WHERE status='aprobada'")
    # migración: canales que solo existían como texto en tareas → tabla channels
    conn.execute(
        "INSERT OR IGNORE INTO channels (name, created_at) "
        "SELECT DISTINCT channel, ? FROM tasks WHERE channel != ''",
        (ts,),
    )
    # migración: columnas nuevas en tablas que ya existían de versiones previas
    for table, col, ddl in [
        ("channels", "drive_folder_id", "ALTER TABLE channels ADD COLUMN drive_folder_id TEXT DEFAULT ''"),
        ("tasks", "drive_folder_id", "ALTER TABLE tasks ADD COLUMN drive_folder_id TEXT DEFAULT ''"),
        ("task_files", "storage", "ALTER TABLE task_files ADD COLUMN storage TEXT DEFAULT 'server'"),
        ("task_files", "drive_file_id", "ALTER TABLE task_files ADD COLUMN drive_file_id TEXT DEFAULT ''"),
        ("task_files", "size", "ALTER TABLE task_files ADD COLUMN size INTEGER DEFAULT 0"),
        ("users", "telegram_chat_id", "ALTER TABLE users ADD COLUMN telegram_chat_id TEXT DEFAULT ''"),
        ("users", "telegram_code", "ALTER TABLE users ADD COLUMN telegram_code TEXT DEFAULT ''"),
        ("users", "email", "ALTER TABLE users ADD COLUMN email TEXT DEFAULT ''"),
        ("calendar_events", "done", "ALTER TABLE calendar_events ADD COLUMN done INTEGER DEFAULT 0"),
    ]:
        cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if col not in cols:
            conn.execute(ddl)
            # si acabamos de crear users.email, rellenar con el usuario cuando sea un correo
            if table == "users" and col == "email":
                conn.execute("UPDATE users SET email = username WHERE email = '' AND username LIKE '%@%'")
    # migración: cuenta única de Drive (en config) → tabla drive_accounts como "Servidor 1"
    n_acc = conn.execute("SELECT COUNT(*) AS c FROM drive_accounts").fetchone()["c"]
    if n_acc == 0:
        old_rt = conn.execute("SELECT value FROM config WHERE key='google_refresh_token'").fetchone()
        if old_rt and old_rt["value"]:
            old_email = conn.execute("SELECT value FROM config WHERE key='google_account_email'").fetchone()
            old_root = conn.execute("SELECT value FROM config WHERE key='drive_root_id'").fetchone()
            conn.execute(
                "INSERT INTO drive_accounts (name,email,refresh_token,root_folder_id,active,created_at) "
                "VALUES ('Servidor 1',?,?,?,1,?)",
                (old_email["value"] if old_email else "", old_rt["value"],
                 old_root["value"] if old_root else "", ts),
            )
            acc_id = conn.execute("SELECT id FROM drive_accounts WHERE name='Servidor 1'").fetchone()["id"]
            # las carpetas de canal ya creadas pertenecen a esa cuenta
            for ch in conn.execute("SELECT name, drive_folder_id FROM channels WHERE drive_folder_id != ''").fetchall():
                conn.execute(
                    "INSERT OR IGNORE INTO drive_channel_folders (account_id, channel_name, folder_id) VALUES (?,?,?)",
                    (acc_id, ch["name"], ch["drive_folder_id"]),
                )
    conn.commit()
    conn.close()


init_db()

# ---------------------------------------------------------------- utilidades


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return f"{salt}${h.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, expected = stored.split("$", 1)
    except ValueError:
        return False
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return secrets.compare_digest(h.hex(), expected)


def user_public(row) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "role": row["role"],
        "active": bool(row["active"]),
    }


def get_current_user(authorization: Optional[str] = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Sesión no válida. Inicia sesión de nuevo.")
    token = authorization.removeprefix("Bearer ").strip()
    conn = db()
    row = conn.execute(
        """SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id
           WHERE s.token = ? AND u.active = 1""",
        (token,),
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(401, "Sesión expirada o usuario inactivo.")
    return dict(row)


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "Solo un administrador puede hacer esto.")
    return user


# ---------------------------------------------------------------- modelos


class SetupBody(BaseModel):
    username: str
    password: str
    display_name: str


class LoginBody(BaseModel):
    username: str
    password: str


class UserCreate(BaseModel):
    username: str
    password: str
    display_name: str
    role: str = "editor"


class UserPatch(BaseModel):
    display_name: Optional[str] = None
    role: Optional[str] = None
    active: Optional[bool] = None
    password: Optional[str] = None


class TaskCreate(BaseModel):
    title: str
    description: str = ""
    channel: str = ""
    assigned_to: Optional[int] = None
    due_date: str = ""
    priority: str = "media"


class TaskPatch(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    channel: Optional[str] = None
    assigned_to: Optional[int] = None
    due_date: Optional[str] = None
    priority: Optional[str] = None
    status: Optional[str] = None
    delivery_note: Optional[str] = None


class FileCreate(BaseModel):
    label: str
    url: str
    kind: str = "archivo"


# ---------------------------------------------------------------- auth


@app.get("/api/bootstrap")
def bootstrap():
    conn = db()
    count = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    conn.close()
    return {"needs_setup": count == 0}


@app.post("/api/setup")
def setup(body: SetupBody):
    conn = db()
    count = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    if count > 0:
        conn.close()
        raise HTTPException(400, "El sistema ya fue configurado.")
    if len(body.password) < 6:
        conn.close()
        raise HTTPException(400, "La contraseña debe tener al menos 6 caracteres.")
    conn.execute(
        "INSERT INTO users (username, password_hash, display_name, role, created_at) VALUES (?,?,?,?,?)",
        (body.username.strip().lower(), hash_password(body.password), body.display_name.strip(), "admin", now()),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/login")
def login(body: LoginBody):
    conn = db()
    row = conn.execute(
        "SELECT * FROM users WHERE username = ?", (body.username.strip().lower(),)
    ).fetchone()
    if not row or not verify_password(body.password, row["password_hash"]) or not row["active"]:
        conn.close()
        raise HTTPException(401, "Usuario o contraseña incorrectos.")
    token = secrets.token_hex(32)
    conn.execute(
        "INSERT INTO sessions (token, user_id, created_at) VALUES (?,?,?)",
        (token, row["id"], now()),
    )
    conn.commit()
    conn.close()
    return {"token": token, "user": user_public(row)}


@app.post("/api/logout")
def logout(authorization: Optional[str] = Header(None)):
    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
        conn = db()
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
        conn.close()
    return {"ok": True}


@app.get("/api/me")
def me(user: dict = Depends(get_current_user)):
    return user_public(user)


# ---------------------------------------------------------------- usuarios


@app.get("/api/users")
def list_users(user: dict = Depends(get_current_user)):
    conn = db()
    rows = conn.execute("SELECT * FROM users ORDER BY display_name").fetchall()
    conn.close()
    return [user_public(r) for r in rows]


@app.post("/api/users")
def create_user(body: UserCreate, admin: dict = Depends(require_admin)):
    if body.role not in ROLES:
        raise HTTPException(400, "Rol no válido. Usa 'admin' o 'editor'.")
    if len(body.password) < 6:
        raise HTTPException(400, "La contraseña debe tener al menos 6 caracteres.")
    conn = db()
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, display_name, role, created_at) VALUES (?,?,?,?,?)",
            (body.username.strip().lower(), hash_password(body.password), body.display_name.strip(), body.role, now()),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(400, "Ese nombre de usuario ya existe.")
    row = conn.execute("SELECT * FROM users WHERE username = ?", (body.username.strip().lower(),)).fetchone()
    conn.close()
    return user_public(row)


@app.patch("/api/users/{user_id}")
def patch_user(user_id: int, body: UserPatch, admin: dict = Depends(require_admin)):
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Usuario no encontrado.")
    if body.role is not None and body.role not in ROLES:
        conn.close()
        raise HTTPException(400, "Rol no válido.")
    if body.active is False and row["role"] == "admin":
        admins = conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE role='admin' AND active=1"
        ).fetchone()["c"]
        if admins <= 1:
            conn.close()
            raise HTTPException(400, "No puedes desactivar al único administrador activo.")
    updates, params = [], []
    if body.display_name is not None:
        updates.append("display_name = ?"); params.append(body.display_name.strip())
    if body.role is not None:
        updates.append("role = ?"); params.append(body.role)
    if body.active is not None:
        updates.append("active = ?"); params.append(1 if body.active else 0)
    if body.password is not None:
        if len(body.password) < 6:
            conn.close()
            raise HTTPException(400, "La contraseña debe tener al menos 6 caracteres.")
        updates.append("password_hash = ?"); params.append(hash_password(body.password))
    if updates:
        params.append(user_id)
        conn.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", params)
        if body.active is False:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        conn.commit()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return user_public(row)


# ---------------------------------------------------------------- tareas


def task_full(conn, task_id: int) -> dict:
    t = conn.execute(
        """SELECT t.*, ua.display_name AS assigned_name, uc.display_name AS creator_name
           FROM tasks t
           LEFT JOIN users ua ON ua.id = t.assigned_to
           LEFT JOIN users uc ON uc.id = t.created_by
           WHERE t.id = ?""",
        (task_id,),
    ).fetchone()
    if not t:
        return None
    files = conn.execute(
        """SELECT f.*, u.display_name AS added_by_name
           FROM task_files f LEFT JOIN users u ON u.id = f.added_by
           WHERE f.task_id = ? ORDER BY f.created_at""",
        (task_id,),
    ).fetchall()
    d = dict(t)
    d["files"] = [dict(f) for f in files]
    return d


@app.get("/api/tasks")
def list_tasks(
    status: Optional[str] = None,
    assigned_to: Optional[int] = None,
    channel: Optional[str] = None,
    with_files: int = 0,
    user: dict = Depends(get_current_user),
):
    conn = db()
    q = """SELECT t.*, ua.display_name AS assigned_name, uc.display_name AS creator_name,
                  (SELECT COUNT(*) FROM task_files f WHERE f.task_id = t.id) AS file_count
           FROM tasks t
           LEFT JOIN users ua ON ua.id = t.assigned_to
           LEFT JOIN users uc ON uc.id = t.created_by
           WHERE 1=1"""
    params = []
    if status:
        q += " AND t.status = ?"; params.append(status)
    if assigned_to:
        q += " AND t.assigned_to = ?"; params.append(assigned_to)
    if channel:
        q += " AND t.channel = ?"; params.append(channel)
    # Las terminadas solo se muestran el día que se terminaron: al empezar el
    # siguiente día local desaparecen del tablero (los datos y la búsqueda quedan intactos).
    local_midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = local_midnight.astimezone(timezone.utc).isoformat()
    q += " AND NOT (t.status = 'terminado' AND t.completed_at < ?)"; params.append(cutoff)
    q += """ ORDER BY CASE t.priority WHEN 'alta' THEN 0 WHEN 'media' THEN 1 ELSE 2 END,
             CASE WHEN t.due_date = '' THEN 1 ELSE 0 END, t.due_date, t.created_at DESC"""
    rows = conn.execute(q, params).fetchall()
    out = [dict(r) for r in rows]
    # con with_files=1 cada tarea trae sus adjuntos (para mostrarlos en Pendientes)
    if with_files and out:
        ids = [t["id"] for t in out]
        marks = ",".join("?" * len(ids))
        fmap: dict = {}
        for f in conn.execute(
            f"SELECT id, task_id, label, url, kind, storage, drive_file_id, size "
            f"FROM task_files WHERE task_id IN ({marks}) ORDER BY created_at", ids
        ).fetchall():
            fmap.setdefault(f["task_id"], []).append(dict(f))
        for t in out:
            t["files"] = fmap.get(t["id"], [])
    conn.close()
    return out


@app.get("/api/tasks/{task_id}")
def get_task(task_id: int, user: dict = Depends(get_current_user)):
    conn = db()
    t = task_full(conn, task_id)
    if t:
        # espejo con Drive: archivos borrados allá desaparecen también de la tarea
        try:
            t["files"], alive = prune_dead_drive_files(
                conn, t["files"], {t.get("drive_folder_id") or ""})
            if t.get("drive_folder_id") and not alive.get(t["drive_folder_id"], True):
                t["drive_folder_id"] = ""
        except Exception:
            pass
    conn.close()
    if not t:
        raise HTTPException(404, "Tarea no encontrada.")
    return t


@app.post("/api/tasks")
def create_task(body: TaskCreate, admin: dict = Depends(require_admin)):
    if body.priority not in PRIORITIES:
        raise HTTPException(400, "Prioridad no válida.")
    conn = db()
    if body.channel.strip():
        conn.execute(
            "INSERT OR IGNORE INTO channels (name, created_at) VALUES (?,?)",
            (body.channel.strip(), now()),
        )
    cur = conn.execute(
        """INSERT INTO tasks (title, description, channel, assigned_to, created_by,
                              due_date, priority, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,'pendiente',?,?)""",
        (body.title.strip(), body.description.strip(), body.channel.strip(),
         body.assigned_to, admin["id"], body.due_date, body.priority, now(), now()),
    )
    conn.commit()
    t = task_full(conn, cur.lastrowid)
    # 🔔 nueva tarea: avisar a TODO el equipo
    notify_users(conn, all_user_ids(conn),
                 f"📥 <b>Nueva tarea:</b> {t['title']}\n"
                 f"Canal: {t['channel'] or '—'}"
                 + (f"\nAsignada a: {t['assigned_name']}" if t.get('assigned_name') else ""),
                 exclude=admin["id"], subject=f"📥 Nueva tarea: {t['title']}")
    conn.close()
    return t


# Un editor puede mover pendiente ↔ revisión (así todos ven qué está en revisión).
# El editor puede terminar la tarea (con archivado a Drive); reabrir es solo del admin.
EDITOR_TRANSITIONS = {
    "pendiente": {"revision"},
    "revision": {"pendiente", "terminado"},
}


@app.patch("/api/tasks/{task_id}")
def patch_task(task_id: int, body: TaskPatch, user: dict = Depends(get_current_user)):
    conn = db()
    t = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not t:
        conn.close()
        raise HTTPException(404, "Tarea no encontrada.")

    is_admin = user["role"] == "admin"
    auto_claim = False

    if not is_admin:
        # Un editor solo cambia estado y nota de entrega, no los detalles.
        forbidden = [body.title, body.description, body.channel, body.assigned_to,
                     body.due_date, body.priority]
        if any(v is not None for v in forbidden):
            conn.close()
            raise HTTPException(403, "Solo un administrador puede editar los detalles de la tarea.")
        if body.status is not None:
            allowed = EDITOR_TRANSITIONS.get(t["status"], set())
            if body.status not in allowed:
                conn.close()
                raise HTTPException(403, f"No puedes pasar la tarea de '{t['status']}' a '{body.status}'.")
            # si la tarea no era de nadie y la toma para revisión, queda a su nombre
            if body.status == "revision" and t["assigned_to"] is None:
                auto_claim = True

    if body.status is not None and body.status not in STATUSES:
        conn.close()
        raise HTTPException(400, "Estado no válido.")
    if body.priority is not None and body.priority not in PRIORITIES:
        conn.close()
        raise HTTPException(400, "Prioridad no válida.")

    updates, params = [], []
    fields = {
        "title": body.title, "description": body.description, "channel": body.channel,
        "assigned_to": body.assigned_to, "due_date": body.due_date,
        "priority": body.priority, "status": body.status, "delivery_note": body.delivery_note,
    }
    for col, val in fields.items():
        if val is not None:
            updates.append(f"{col} = ?")
            params.append(val.strip() if isinstance(val, str) and col != "status" else val)
    if body.status == "terminado":
        updates.append("completed_at = ?"); params.append(now())
    elif body.status is not None:
        updates.append("completed_at = ''")
    if auto_claim:
        updates.append("assigned_to = ?"); params.append(user["id"])
    if updates:
        updates.append("updated_at = ?"); params.append(now())
        params.append(task_id)
        conn.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?", params)
        conn.commit()
    tt = task_full(conn, task_id)
    # 🔔 avisos por Telegram (sin notificar a quien hizo la acción)
    quien = user["display_name"]
    if body.assigned_to is not None and body.assigned_to != t["assigned_to"] and body.assigned_to:
        notify_users(conn, [body.assigned_to],
                     f"📥 <b>Te asignaron una tarea:</b> {tt['title']}\nCanal: {tt['channel'] or '—'}",
                     exclude=user["id"], subject=f"📥 Te asignaron: {tt['title']}")
    if body.status is not None and body.status != t["status"]:
        if body.status == "revision":
            # revisión: solo a los admins
            notify_users(conn, admin_ids(conn),
                         f"🟠 <b>En revisión:</b> {tt['title']}\nLa pasó: {quien}",
                         exclude=user["id"], subject=f"🟠 En revisión: {tt['title']}")
        elif body.status == "terminado":
            # terminada: a TODO el equipo
            notify_users(conn, all_user_ids(conn),
                         f"✅ <b>Terminada:</b> {tt['title']}\nLa terminó: {quien}",
                         exclude=user["id"], subject=f"✅ Terminada: {tt['title']}")
        elif body.status == "pendiente" and t["status"] in ("revision", "terminado"):
            notify_users(conn, admin_ids(conn) + [tt["assigned_to"]],
                         f"↩️ <b>Volvió a pendiente:</b> {tt['title']}\nLa movió: {quien}",
                         exclude=user["id"], subject=f"↩️ Volvió a pendiente: {tt['title']}")
    conn.close()
    return tt


@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: int, admin: dict = Depends(require_admin)):
    conn = db()
    cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        raise HTTPException(404, "Tarea no encontrada.")
    return {"ok": True}


# ---------------------------------------------------------------- archivos


@app.post("/api/tasks/{task_id}/files")
def add_file(task_id: int, body: FileCreate, user: dict = Depends(get_current_user)):
    conn = db()
    t = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not t:
        conn.close()
        raise HTTPException(404, "Tarea no encontrada.")
    if user["role"] != "admin" and t["assigned_to"] != user["id"]:
        conn.close()
        raise HTTPException(403, "Solo puedes adjuntar archivos a tus propias tareas.")
    url = body.url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        conn.close()
        raise HTTPException(400, "El link debe empezar con http:// o https://")
    conn.execute(
        "INSERT INTO task_files (task_id, label, url, kind, added_by, created_at) VALUES (?,?,?,?,?,?)",
        (task_id, body.label.strip(), url, body.kind.strip() or "archivo", user["id"], now()),
    )
    conn.commit()
    t = task_full(conn, task_id)
    conn.close()
    return t


def guess_kind(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in {"jpg", "jpeg", "png", "webp", "gif", "psd"}:
        return "miniatura"
    if ext in {"mp3", "wav", "flac", "m4a", "aac", "ogg"}:
        return "música"
    if ext in {"mp4", "mov", "avi", "mkv", "webm"}:
        return "video"
    if ext in {"txt", "doc", "docx", "pdf", "md", "rtf"}:
        return "guion"
    return "archivo"


MAX_UPLOAD = 500 * 1024 * 1024  # 500 MB


@app.post("/api/tasks/{task_id}/upload")
async def upload_task_file(task_id: int, file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    conn = db()
    t = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not t:
        conn.close()
        raise HTTPException(404, "Tarea no encontrada.")
    original = os.path.basename(file.filename or "archivo")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", original)[:120] or "archivo"
    stored = f"t{task_id}_{secrets.token_hex(4)}_{safe}"
    dest = os.path.join(UPLOADS_DIR, stored)
    size = 0
    try:
        with open(dest, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD:
                    raise HTTPException(400, "Archivo demasiado grande (máximo 500 MB).")
                out.write(chunk)
    except HTTPException:
        os.remove(dest)
        conn.close()
        raise
    conn.execute(
        "INSERT INTO task_files (task_id, label, url, kind, added_by, created_at) VALUES (?,?,?,?,?,?)",
        (task_id, original, "/uploads/" + stored, guess_kind(original), user["id"], now()),
    )
    conn.commit()
    t = task_full(conn, task_id)
    conn.close()
    return t


@app.delete("/api/files/{file_id}")
def delete_file(file_id: int, user: dict = Depends(get_current_user)):
    conn = db()
    f = conn.execute("SELECT * FROM task_files WHERE id = ?", (file_id,)).fetchone()
    if not f:
        conn.close()
        raise HTTPException(404, "Archivo no encontrado.")
    if user["role"] != "admin" and f["added_by"] != user["id"]:
        conn.close()
        raise HTTPException(403, "Solo puedes borrar los archivos que tú agregaste.")
    conn.execute("DELETE FROM task_files WHERE id = ?", (file_id,))
    conn.commit()
    conn.close()
    # si era un archivo subido (no un link externo), borrar también el archivo físico
    if f["url"].startswith("/uploads/"):
        path = os.path.join(UPLOADS_DIR, os.path.basename(f["url"]))
        if os.path.isfile(path):
            os.remove(path)
    return {"ok": True}


# ---------------------------------------------------------------- extras


class ChannelCreate(BaseModel):
    name: str


@app.get("/api/channels")
def channels(user: dict = Depends(get_current_user)):
    conn = db()
    rows = conn.execute("SELECT * FROM channels ORDER BY name").fetchall()
    # Igual que en el tablero: las terminadas de días anteriores no cuentan.
    local_midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = local_midnight.astimezone(timezone.utc).isoformat()
    out = []
    for r in rows:
        c = dict(r)
        for s in STATUSES:
            c[s] = conn.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE channel = ? AND status = ? "
                "AND NOT (status = 'terminado' AND completed_at < ?)",
                (r["name"], s, cutoff),
            ).fetchone()["n"]
        out.append(c)
    conn.close()
    return out


@app.post("/api/channels")
def create_channel(body: ChannelCreate, admin: dict = Depends(require_admin)):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "El nombre del canal es obligatorio.")
    conn = db()
    try:
        conn.execute("INSERT INTO channels (name, created_at) VALUES (?,?)", (name, now()))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(400, "Ese canal ya existe.")
    row = conn.execute("SELECT * FROM channels WHERE name = ?", (name,)).fetchone()
    # si Drive está conectado, crear ya mismo la carpeta del canal en SERVIDOR-VIDEOS/
    drive_folder = ""
    try:
        acc = active_account(conn)
        if acc:
            token = drive_access_token(conn, acc)
            drive_folder = drive_channel_folder(conn, token, acc, name)
    except HTTPException:
        pass  # Drive no conectado: la carpeta se creará al archivar la primera tarea
    conn.close()
    out = dict(row)
    out["drive_folder_id"] = drive_folder or out.get("drive_folder_id", "")
    return out


@app.delete("/api/channels/{channel_id}")
def delete_channel(channel_id: int, force: bool = False, admin: dict = Depends(require_admin)):
    conn = db()
    row = conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Ese canal no existe.")
    name = row["name"]
    n_tasks = conn.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE channel = ?", (name,)
    ).fetchone()["n"]
    if n_tasks > 0 and not force:
        conn.close()
        raise HTTPException(
            409,
            f"El canal «{name}» tiene {n_tasks} tarea(s). Elimínalas o muévelas antes, "
            f"o confirma para borrar el canal y todas sus tareas.",
        )
    if force and n_tasks > 0:
        conn.execute("DELETE FROM tasks WHERE channel = ?", (name,))
    conn.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
    conn.commit()
    conn.close()
    return {"deleted": name, "tasks_deleted": n_tasks if force else 0}


# ---------------------------------------------------------------- índice / búsqueda

def _like_and(tokens, cols):
    """Construye un WHERE donde CADA palabra debe aparecer en alguna de las columnas."""
    clauses, params = [], []
    for tok in tokens:
        clauses.append("(" + " OR ".join(f"{c} LIKE ?" for c in cols) + ")")
        params += [f"%{tok}%"] * len(cols)
    return " AND ".join(clauses), params


@app.get("/api/search")
def search(q: str = "", channel: str = "", user: dict = Depends(get_current_user)):
    tokens = [t for t in q.strip().split() if t][:6]
    if not tokens:
        return {"files": [], "tasks": []}

    conn = db()
    # límite por rol: el admin ve todo; el editor solo lo de sus tareas
    scope, scope_params = "", []
    if user["role"] != "admin":
        scope = " AND t.assigned_to = ?"
        scope_params = [user["id"]]
    # filtro opcional por canal ("" = búsqueda general en todo el servidor)
    if channel:
        scope += " AND t.channel = ?"
        scope_params.append(channel)

    fwhere, fparams = _like_and(tokens, ["f.label", "f.kind", "t.title", "t.channel"])
    files = conn.execute(
        f"""SELECT f.id, f.label, f.kind, f.url, f.storage, f.drive_file_id, f.size, f.created_at,
                   t.id AS task_id, t.title AS task_title, t.channel, t.status, t.drive_folder_id,
                   u.display_name AS added_by_name
            FROM task_files f
            JOIN tasks t ON t.id = f.task_id
            LEFT JOIN users u ON u.id = f.added_by
            WHERE ({fwhere}){scope}
            ORDER BY f.created_at DESC LIMIT 100""",
        fparams + scope_params,
    ).fetchall()

    twhere, tparams = _like_and(tokens, ["t.title", "t.channel", "t.description"])
    tasks = conn.execute(
        f"""SELECT t.id, t.title, t.channel, t.status, t.drive_folder_id,
                   (SELECT COUNT(*) FROM task_files f WHERE f.task_id = t.id) AS file_count
            FROM tasks t
            WHERE ({twhere}){scope}
            ORDER BY t.created_at DESC LIMIT 50""",
        tparams + scope_params,
    ).fetchall()

    # espejo con Drive: lo borrado allá desaparece de aquí (registros incluidos)
    folder_ids = ({r["drive_folder_id"] for r in files} | {r["drive_folder_id"] for r in tasks})
    try:
        files, alive = prune_dead_drive_files(conn, files, folder_ids)
    except Exception:
        files, alive = [dict(r) for r in files], {}
    conn.close()

    def with_folder(r):
        d = dict(r)
        fid = d.get("drive_folder_id") or ""
        if fid and not alive.get(fid, True):
            fid = ""
        d["drive_folder_id"] = fid
        d["folder_link"] = drive_folder_link(fid)
        return d

    return {"files": [with_folder(r) for r in files], "tasks": [with_folder(r) for r in tasks]}


@app.get("/api/stats")
def stats(user: dict = Depends(get_current_user)):
    conn = db()
    out = {}
    for s in STATUSES:
        out[s] = conn.execute("SELECT COUNT(*) AS c FROM tasks WHERE status = ?", (s,)).fetchone()["c"]
    out["mias_pendientes"] = conn.execute(
        "SELECT COUNT(*) AS c FROM tasks WHERE assigned_to = ? AND status = 'pendiente'",
        (user["id"],),
    ).fetchone()["c"]
    conn.close()
    return out


# ================================================================ GOOGLE DRIVE
#
# Modelo de almacenamiento (decisión del dueño):
#   - Adjuntos de trabajo (guion, miniatura, música)  -> se quedan en ESTE servidor
#     (endpoint /api/tasks/{id}/upload, carpeta $DATA_DIR/uploads).
#   - Video final (el entregable)                      -> se sube DIRECTO del navegador
#     a Google Drive (los bytes nunca pasan por el servidor). Aquí solo creamos la
#     sesión reanudable y verificamos que el archivo quedó bien.
#
# Requiere que el dueño conecte su cuenta UNA sola vez (OAuth). Las credenciales de
# la app (client_id / client_secret) se sacan de Google Cloud Console y se guardan
# en la tabla config. Ver README para el paso a paso.

DRIVE_ROOT_NAME = "SERVIDOR-VIDEOS"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"
GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable"

_token_cache: dict = {}      # account_id -> {access_token, expires_at}
_storage_cache: dict = {}    # account_id -> {data, at} (cuota de Drive, cache 60 s)


def cfg_get(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def cfg_set(conn, key: str, value: str):
    conn.execute(
        "INSERT INTO config (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _http_json(method: str, url: str, headers: dict, data: bytes = None, timeout: float = None):
    """Petición HTTP simple; devuelve (status, headers_dict, body_json_o_texto)."""
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read().decode()
            body = json.loads(raw) if raw else {}
            return res.status, dict(res.headers), body
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            body = json.loads(raw)
        except Exception:
            body = {"raw": raw}
        return e.code, dict(e.headers), body


def active_account(conn):
    """El 'servidor' de Drive activo (donde se sube ahora). Fallback: el primero conectado."""
    return (conn.execute(
        "SELECT * FROM drive_accounts WHERE active=1 AND refresh_token != '' LIMIT 1").fetchone()
        or conn.execute(
        "SELECT * FROM drive_accounts WHERE refresh_token != '' ORDER BY id LIMIT 1").fetchone())


def drive_access_token(conn, acc=None) -> str:
    """Access token válido para una cuenta (por defecto la activa), renovándolo si hace falta."""
    if acc is None:
        acc = active_account(conn)
    if not acc:
        raise HTTPException(400, "Google Drive no está conectado. Ve a Ajustes → Drive.")
    cached = _token_cache.get(acc["id"])
    if cached and cached["expires_at"] > time.time() + 60:
        return cached["access_token"]
    client_id = cfg_get(conn, "google_client_id")
    client_secret = cfg_get(conn, "google_client_secret")
    if not (client_id and client_secret):
        raise HTTPException(400, "Google Drive no está conectado. Ve a Ajustes → Drive.")
    form = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": acc["refresh_token"],
        "grant_type": "refresh_token",
    }).encode()
    status, _, body = _http_json(
        "POST", GOOGLE_TOKEN,
        {"Content-Type": "application/x-www-form-urlencoded"}, form,
    )
    if status != 200 or "access_token" not in body:
        # refresh token revocado/expirado -> forzar reconexión de ESA cuenta
        conn.execute("UPDATE drive_accounts SET refresh_token='' WHERE id=?", (acc["id"],))
        conn.commit()
        raise HTTPException(400, f"La conexión de «{acc['name']}» expiró. Reconéctala en Ajustes → Drive.")
    _token_cache[acc["id"]] = {
        "access_token": body["access_token"],
        "expires_at": time.time() + int(body.get("expires_in", 3600)),
    }
    return body["access_token"]


def drive_find_folder(token: str, name: str, parent: str) -> Optional[str]:
    q = (f"name = '{name.replace(chr(39), chr(92) + chr(39))}' and "
         "mimeType = 'application/vnd.google-apps.folder' and "
         f"'{parent}' in parents and trashed = false")
    url = f"{DRIVE_API}/files?q={urllib.parse.quote(q)}&fields=files(id,name)&spaces=drive"
    status, _, body = _http_json("GET", url, {"Authorization": "Bearer " + token})
    if status == 200 and body.get("files"):
        return body["files"][0]["id"]
    return None


def drive_create_folder(token: str, name: str, parent: Optional[str]) -> str:
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent:
        meta["parents"] = [parent]
    status, _, body = _http_json(
        "POST", f"{DRIVE_API}/files?fields=id",
        {"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        json.dumps(meta).encode(),
    )
    if status not in (200, 201) or "id" not in body:
        raise HTTPException(502, f"No se pudo crear la carpeta en Drive: {body}")
    return body["id"]


def drive_ensure_folder(conn, token: str, name: str, parent: Optional[str]) -> str:
    fid = drive_find_folder(token, name, parent or "root")
    return fid or drive_create_folder(token, name, parent)


def drive_root_folder(conn, token: str, acc) -> str:
    if acc["root_folder_id"]:
        return acc["root_folder_id"]
    root = drive_ensure_folder(conn, token, DRIVE_ROOT_NAME, None)
    conn.execute("UPDATE drive_accounts SET root_folder_id=? WHERE id=?", (root, acc["id"]))
    conn.commit()
    return root


def drive_channel_folder(conn, token: str, acc, channel_name: str) -> str:
    """Carpeta destino de un video EN esa cuenta: raíz si no tiene canal, o subcarpeta del canal."""
    root = drive_root_folder(conn, token, acc)
    if not channel_name:
        return root
    row = conn.execute(
        "SELECT folder_id FROM drive_channel_folders WHERE account_id=? AND channel_name=?",
        (acc["id"], channel_name),
    ).fetchone()
    if row and row["folder_id"]:
        return row["folder_id"]
    fid = drive_ensure_folder(conn, token, channel_name, root)
    conn.execute(
        "INSERT OR REPLACE INTO drive_channel_folders (account_id, channel_name, folder_id) VALUES (?,?,?)",
        (acc["id"], channel_name, fid),
    )
    conn.commit()
    return fid


# cache en memoria de carpetas ya resueltas (se repuebla solo si se reinicia el server)
_folder_cache: dict = {}


def drive_task_folder(conn, token: str, acc, t, folder_name: str = "") -> str:
    """Carpeta del proyecto en Drive: SERVIDOR-VIDEOS/<canal>/<nombre>/
    El nombre lo elige el usuario al archivar (ej. "Guion 21 de julio");
    si no lo da, se usa "T<id> - <título>"."""
    name = re.sub(r'[\\/:*?"<>|]+', " ", folder_name or "").strip()[:90]
    if not name:
        safe_title = re.sub(r'[\\/:*?"<>|]+', " ", t["title"]).strip()[:80] or "video"
        name = f"T{t['id']} - {safe_title}"
    key = ("task", acc["id"], t["id"], name)
    if key in _folder_cache:
        return _folder_cache[key]
    parent = drive_channel_folder(conn, token, acc, t["channel"] or "")
    fid = drive_ensure_folder(conn, token, name, parent)
    _folder_cache[key] = fid
    # guardar el id de la carpeta en la tarea para poder abrirla desde la búsqueda
    conn.execute("UPDATE tasks SET drive_folder_id = ? WHERE id = ?", (fid, t["id"]))
    conn.commit()
    return fid


def drive_folder_link(folder_id: str) -> str:
    """URL que abre la carpeta directamente en la interfaz de Google Drive."""
    return f"https://drive.google.com/drive/folders/{folder_id}" if folder_id else ""


def drive_upload_small(conn, name: str, content: str, folder_id: str) -> str:
    """Sube un archivo pequeño de texto (p. ej. la lista de links) a una carpeta de Drive."""
    token = drive_access_token(conn)
    boundary = "srvboundary" + secrets.token_hex(8)
    meta = json.dumps({"name": name, "parents": [folder_id]})
    payload = (
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
        f"--{boundary}\r\nContent-Type: text/plain; charset=UTF-8\r\n\r\n{content}\r\n"
        f"--{boundary}--"
    ).encode()
    status, _, body = _http_json(
        "POST", "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id",
        {"Authorization": f"Bearer {token}",
         "Content-Type": f"multipart/related; boundary={boundary}"},
        payload, timeout=20)
    if status not in (200, 201) or "id" not in body:
        raise HTTPException(502, "No se pudo guardar el archivo de links en Drive.")
    return body["id"]


_alive_cache: dict = {}  # drive_id -> hasta cuándo vale el veredicto "sigue vivo"


def _drive_ids_alive(conn, ids) -> dict:
    """Comprueba en Google qué ids de Drive (archivo o carpeta) siguen existiendo.
    Devuelve {id: True/False}. Un id está muerto si TODAS las cuentas conectadas
    responden 404 o lo reportan en la papelera. Ante la duda (sin conexión, error
    de red, rate limit) responde True para no borrar registros por un fallo temporal."""
    ids = {i for i in ids if i}
    out, pend, now_t = {}, [], time.time()
    for i in ids:
        if _alive_cache.get(i, 0) > now_t:
            out[i] = True
        else:
            pend.append(i)
    if not pend:
        return out
    # tokens de TODAS las cuentas conectadas (un archivo puede vivir en cualquier "servidor")
    tokens = []
    for acc in conn.execute(
        "SELECT * FROM drive_accounts WHERE refresh_token != '' ORDER BY active DESC, id"
    ).fetchall():
        try:
            tokens.append(drive_access_token(conn, acc))
        except Exception:
            pass
    if not tokens:  # Drive desconectado -> no podemos verificar, no tocar nada
        return {**out, **{i: True for i in pend}}

    def check(fid):
        for tk in tokens:
            status, _, body = _http_json(
                "GET", f"{DRIVE_API}/files/{fid}?fields=id,trashed",
                {"Authorization": f"Bearer {tk}"}, timeout=10)
            if status == 200:
                return fid, not body.get("trashed", False)
            if status != 404:  # error raro (rate limit, red) -> no arriesgarse a borrar
                return fid, True
        return fid, False  # 404 en todas las cuentas -> ya no existe en Drive

    with ThreadPoolExecutor(max_workers=8) as ex:
        for fid, alive in ex.map(check, pend):
            out[fid] = alive
            if alive:
                _alive_cache[fid] = now_t + 120
    return out


def prune_dead_drive_files(conn, file_rows, folder_ids=()):
    """Espejo con Drive: si un archivo archivado ya no existe allá (borrado o en la
    papelera), se elimina su registro para que no vuelva a aparecer en la plataforma.
    También olvida carpetas de tarea borradas. Devuelve (filas_vivas, mapa_alive)."""
    rows = [dict(r) for r in file_rows]
    drive_ids = {r["drive_file_id"] for r in rows
                 if r.get("storage") == "drive" and r.get("drive_file_id")}
    folder_ids = {f for f in folder_ids if f}
    if not (drive_ids or folder_ids):
        return rows, {}
    alive = _drive_ids_alive(conn, drive_ids | folder_ids)
    keep = []
    for r in rows:
        if (r.get("storage") == "drive" and r.get("drive_file_id")
                and not alive.get(r["drive_file_id"], True)):
            conn.execute("DELETE FROM task_files WHERE id = ?", (r["id"],))
        else:
            keep.append(r)
    for fid in folder_ids:
        if not alive.get(fid, True):
            conn.execute("UPDATE tasks SET drive_folder_id='' WHERE drive_folder_id=?", (fid,))
    conn.commit()
    return keep, alive


def drive_subpath_folder(conn, token: str, base: str, subpath: str) -> str:
    """Replica subcarpetas (máx. 5 niveles) dentro de la carpeta del proyecto."""
    fid = base
    for part in [p for p in subpath.split("/") if p and p not in (".", "..")][:5]:
        key = ("sub", fid, part)
        if key not in _folder_cache:
            _folder_cache[key] = drive_ensure_folder(conn, token, part, fid)
        fid = _folder_cache[key]
    return fid


# ---------------------------------------------------------------- Drive: estado y OAuth


class DriveCreds(BaseModel):
    client_id: str
    client_secret: str


def _redirect_uri(request: Request) -> str:
    return str(request.base_url).rstrip("/") + "/api/drive/callback"


@app.get("/api/drive/status")
def drive_status(request: Request, user: dict = Depends(get_current_user)):
    conn = db()
    has_creds = bool(cfg_get(conn, "google_client_id") and cfg_get(conn, "google_client_secret"))
    acc = active_account(conn)
    accounts = [
        {"id": a["id"], "name": a["name"], "email": a["email"],
         "connected": bool(a["refresh_token"]), "active": bool(a["active"])}
        for a in conn.execute("SELECT * FROM drive_accounts ORDER BY id").fetchall()
    ]
    conn.close()
    return {
        "has_credentials": has_creds,
        "connected": acc is not None,
        "email": acc["email"] if acc else "",
        "active_id": acc["id"] if acc else 0,
        "accounts": accounts,
        "redirect_uri": _redirect_uri(request),
    }


@app.post("/api/drive/accounts/{account_id}/activate")
def drive_account_activate(account_id: int, admin: dict = Depends(require_admin)):
    conn = db()
    a = conn.execute("SELECT * FROM drive_accounts WHERE id=?", (account_id,)).fetchone()
    if not a:
        conn.close()
        raise HTTPException(404, "Ese servidor no existe.")
    if not a["refresh_token"]:
        conn.close()
        raise HTTPException(400, f"«{a['name']}» está desconectado. Reconéctalo primero.")
    conn.execute("UPDATE drive_accounts SET active=0")
    conn.execute("UPDATE drive_accounts SET active=1 WHERE id=?", (account_id,))
    conn.commit()
    conn.close()
    return {"ok": True, "active": a["name"]}


@app.delete("/api/drive/accounts/{account_id}")
def drive_account_remove(account_id: int, admin: dict = Depends(require_admin)):
    conn = db()
    a = conn.execute("SELECT * FROM drive_accounts WHERE id=?", (account_id,)).fetchone()
    if not a:
        conn.close()
        raise HTTPException(404, "Ese servidor no existe.")
    conn.execute("DELETE FROM drive_accounts WHERE id=?", (account_id,))
    conn.execute("DELETE FROM drive_channel_folders WHERE account_id=?", (account_id,))
    # si era el activo, activar otro que quede conectado
    if a["active"]:
        nxt = conn.execute(
            "SELECT id FROM drive_accounts WHERE refresh_token != '' ORDER BY id LIMIT 1").fetchone()
        if nxt:
            conn.execute("UPDATE drive_accounts SET active=1 WHERE id=?", (nxt["id"],))
    conn.commit()
    conn.close()
    _token_cache.pop(account_id, None)
    _storage_cache.pop(account_id, None)
    return {"ok": True, "removed": a["name"]}


@app.get("/api/drive/storage")
def drive_storage(account_id: int = 0, user: dict = Depends(get_current_user)):
    """Cuota de almacenamiento de un 'servidor' (cuenta Drive): total, usado y libre."""
    conn = db()
    if account_id:
        acc = conn.execute("SELECT * FROM drive_accounts WHERE id=?", (account_id,)).fetchone()
        if not acc:
            conn.close()
            raise HTTPException(404, "Ese servidor no existe.")
    else:
        acc = active_account(conn)
        if not acc:
            conn.close()
            raise HTTPException(400, "No hay ningún servidor de Drive conectado.")
    cached = _storage_cache.get(acc["id"])
    if cached and time.time() - cached["at"] < 60:
        conn.close()
        return cached["data"]
    if not acc["refresh_token"]:
        conn.close()
        raise HTTPException(400, f"«{acc['name']}» está desconectado.")
    token = drive_access_token(conn, acc)
    status, _, body = _http_json(
        "GET", f"{DRIVE_API}/about?fields=storageQuota,user",
        {"Authorization": "Bearer " + token},
    )
    conn.close()
    if status != 200 or "storageQuota" not in body:
        raise HTTPException(502, f"Drive no devolvió la cuota: {body}")
    q = body["storageQuota"]
    limit = int(q.get("limit") or 0)          # 0 = sin límite (poco común)
    usage = int(q.get("usage") or 0)
    data = {
        "id": acc["id"], "name": acc["name"],
        "email": (body.get("user") or {}).get("emailAddress", acc["email"]),
        "limit": limit, "usage": usage,
        "usage_in_drive": int(q.get("usageInDrive") or 0),
        "in_trash": int(q.get("usageInDriveTrash") or 0),
        "free": max(limit - usage, 0) if limit else 0,
    }
    _storage_cache[acc["id"]] = {"data": data, "at": time.time()}
    return data


@app.post("/api/drive/credentials")
def drive_credentials(body: DriveCreds, admin: dict = Depends(require_admin)):
    conn = db()
    cfg_set(conn, "google_client_id", body.client_id.strip())
    cfg_set(conn, "google_client_secret", body.client_secret.strip())
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/drive/auth-url")
def drive_auth_url(request: Request, admin: dict = Depends(require_admin)):
    conn = db()
    client_id = cfg_get(conn, "google_client_id")
    if not client_id:
        conn.close()
        raise HTTPException(400, "Primero guarda las credenciales de Google (client_id y secret).")
    state = secrets.token_hex(16)
    cfg_set(conn, "oauth_state", state)
    conn.commit()
    conn.close()
    params = urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": _redirect_uri(request),
        "response_type": "code",
        "scope": DRIVE_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    })
    return {"auth_url": f"{GOOGLE_AUTH}?{params}"}


@app.get("/api/drive/callback")
def drive_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    conn = db()
    if error:
        conn.close()
        return HTMLResponse(_drive_close_page(f"Autorización cancelada: {error}"))
    if not code or state != cfg_get(conn, "oauth_state"):
        conn.close()
        return HTMLResponse(_drive_close_page("Enlace de autorización inválido. Intenta de nuevo."))
    form = urllib.parse.urlencode({
        "code": code,
        "client_id": cfg_get(conn, "google_client_id"),
        "client_secret": cfg_get(conn, "google_client_secret"),
        "redirect_uri": _redirect_uri(request),
        "grant_type": "authorization_code",
    }).encode()
    status, _, body = _http_json(
        "POST", GOOGLE_TOKEN,
        {"Content-Type": "application/x-www-form-urlencoded"}, form,
    )
    if status != 200 or "refresh_token" not in body:
        conn.close()
        return HTMLResponse(_drive_close_page(
            "Google no devolvió un refresh token. Quita el acceso de la app en tu cuenta "
            "Google y vuelve a conectar (debe pedir permiso de nuevo)."))
    cfg_set(conn, "oauth_state", "")
    access_token = body.get("access_token", "")
    # email de la cuenta que autorizó (para saber si es un servidor nuevo o una reconexión)
    email = ""
    try:
        s2, _, info = _http_json(
            "GET", "https://www.googleapis.com/oauth2/v2/userinfo",
            {"Authorization": "Bearer " + access_token},
        )
        if s2 == 200 and info.get("email"):
            email = info["email"]
    except Exception:
        pass
    existing = conn.execute(
        "SELECT * FROM drive_accounts WHERE email=? AND email != ''", (email,)
    ).fetchone() if email else None
    if existing:
        # la misma cuenta se reconecta: renovar su token, conservar nombre y carpetas
        conn.execute("UPDATE drive_accounts SET refresh_token=? WHERE id=?",
                     (body["refresh_token"], existing["id"]))
        acc_id, acc_name = existing["id"], existing["name"]
    else:
        n = conn.execute("SELECT COUNT(*) AS c FROM drive_accounts").fetchone()["c"] + 1
        first = n == 1
        conn.execute(
            "INSERT INTO drive_accounts (name,email,refresh_token,active,created_at) VALUES (?,?,?,?,?)",
            (f"Servidor {n}", email, body["refresh_token"], 1 if first else 0, now()),
        )
        acc_id = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
        acc_name = f"Servidor {n}"
    if access_token:
        _token_cache[acc_id] = {
            "access_token": access_token,
            "expires_at": time.time() + int(body.get("expires_in", 3600)),
        }
    conn.commit()
    conn.close()
    return HTMLResponse(_drive_close_page(f"¡{acc_name} conectado! Ya puedes cerrar esta ventana.", ok=True))


@app.post("/api/drive/disconnect")
def drive_disconnect(admin: dict = Depends(require_admin)):
    """Desconecta TODOS los servidores (se conserva el registro para reconectar)."""
    conn = db()
    conn.execute("UPDATE drive_accounts SET refresh_token=''")
    conn.commit()
    conn.close()
    _token_cache.clear()
    _storage_cache.clear()
    return {"ok": True}


def _drive_close_page(msg: str, ok: bool = False) -> str:
    color = "#E9B44C" if ok else "#E86A6A"
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Drive</title><style>
body{{background:#0A0D1C;color:#EAEDFB;font-family:system-ui,sans-serif;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0;text-align:center}}
.box{{max-width:380px;padding:30px}}.dot{{width:14px;height:14px;border-radius:50%;
background:{color};margin:0 auto 18px;box-shadow:0 0 18px {color}}}
button{{margin-top:22px;background:#8C7BFF;color:#0A0D1C;border:none;border-radius:10px;
padding:11px 20px;font-weight:800;cursor:pointer;font-size:14px}}</style></head>
<body><div class="box"><div class="dot"></div><p>{msg}</p>
<button onclick="window.close()">Cerrar ventana</button>
<script>try{{if(window.opener)window.opener.postMessage('drive-updated','*')}}catch(e){{}}
setTimeout(()=>{{try{{window.close()}}catch(e){{}}}},2500)</script></div></body></html>"""


# ---------------------------------------------------------------- Drive: subida del video final


class UploadInit(BaseModel):
    filename: str
    size: int
    mime: str = "application/octet-stream"
    subpath: str = ""       # subcarpeta relativa dentro de la carpeta del proyecto
    folder_name: str = ""   # nombre de la carpeta del proyecto en Drive (lo elige el usuario)


class UploadComplete(BaseModel):
    drive_file_id: str


@app.post("/api/tasks/{task_id}/drive/init")
def drive_upload_init(task_id: int, body: UploadInit, request: Request, user: dict = Depends(get_current_user)):
    conn = db()
    t = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not t:
        conn.close()
        raise HTTPException(404, "Tarea no encontrada.")
    if user["role"] != "admin" and t["assigned_to"] != user["id"]:
        conn.close()
        raise HTTPException(403, "Solo puedes subir archivos de tus propias tareas.")
    acc = active_account(conn)
    token = drive_access_token(conn, acc)
    folder = drive_subpath_folder(conn, token, drive_task_folder(conn, token, acc, t, body.folder_name), body.subpath)
    drive_name = body.filename
    meta = json.dumps({"name": drive_name, "parents": [folder]}).encode()
    # Origin del navegador: Google liga la sesión a este origen y así permite
    # que el navegador suba los bytes directo (CORS). Sin esto: "Failed to fetch".
    origin = request.headers.get("origin") or str(request.base_url).rstrip("/")
    status, headers, respbody = _http_json(
        "POST", DRIVE_UPLOAD,
        {
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": body.mime or "application/octet-stream",
            "X-Upload-Content-Length": str(body.size),
            "Origin": origin,
        },
        meta,
    )
    session_uri = headers.get("Location") or headers.get("location")
    if status not in (200, 201) or not session_uri:
        conn.close()
        raise HTTPException(502, f"Drive no abrió la sesión de subida: {respbody}")
    cur = conn.execute(
        """INSERT INTO uploads (task_id, filename, size, mime, session_uri, status, added_by, created_at)
           VALUES (?,?,?,?,?,'subiendo',?,?)""",
        (task_id, body.filename, body.size, body.mime, session_uri, user["id"], now()),
    )
    conn.commit()
    up_id = cur.lastrowid
    conn.close()
    return {"upload_id": up_id, "session_uri": session_uri}


@app.get("/api/uploads/{upload_id}")
def drive_upload_get(upload_id: int, user: dict = Depends(get_current_user)):
    conn = db()
    u = conn.execute("SELECT * FROM uploads WHERE id = ?", (upload_id,)).fetchone()
    conn.close()
    if not u:
        raise HTTPException(404, "Subida no encontrada.")
    return dict(u)


@app.post("/api/tasks/{task_id}/drive/complete")
def drive_upload_complete(task_id: int, body: UploadComplete, user: dict = Depends(get_current_user)):
    conn = db()
    u = conn.execute(
        "SELECT * FROM uploads WHERE task_id = ? AND drive_file_id = '' ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    token = drive_access_token(conn)
    # verificar en Drive que el archivo existe y tomar su nombre/tamaño/link reales
    url = f"{DRIVE_API}/files/{body.drive_file_id}?fields=id,name,size,webViewLink"
    status, _, info = _http_json("GET", url, {"Authorization": "Bearer " + token})
    if status != 200 or "id" not in info:
        conn.close()
        raise HTTPException(502, "No se pudo verificar el video en Drive. Revisa la subida.")
    link = info.get("webViewLink") or f"https://drive.google.com/file/d/{info['id']}/view"
    if u:
        conn.execute(
            "UPDATE uploads SET drive_file_id = ?, status = 'completo' WHERE id = ?",
            (info["id"], u["id"]),
        )
    fname = info.get("name", "archivo")
    conn.execute(
        """INSERT INTO task_files (task_id, label, url, kind, storage, drive_file_id, size, added_by, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (task_id, fname, link, guess_kind(fname), "drive",
         info["id"], int(info.get("size") or 0), user["id"], now()),
    )
    conn.commit()
    t = task_full(conn, task_id)
    conn.close()
    return t


class FinishBody(BaseModel):
    clean_server_files: bool = True


@app.post("/api/tasks/{task_id}/finish")
def finish_task(task_id: int, body: FinishBody, user: dict = Depends(get_current_user)):
    """Marca la tarea como terminada. Si el material quedó archivado en Drive,
    limpia del servidor los adjuntos subidos (ya no tiene sentido guardarlos).
    Puede terminarla el admin, o el editor si la tarea es suya (o no está asignada)."""
    conn = db()
    t = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not t:
        conn.close()
        raise HTTPException(404, "Tarea no encontrada.")
    if user["role"] != "admin" and t["assigned_to"] not in (None, user["id"]):
        conn.close()
        raise HTTPException(403, "Esta tarea está asignada a otra persona.")
    conn.execute(
        "UPDATE tasks SET status = 'terminado', completed_at = ?, updated_at = ? WHERE id = ?",
        (now(), now(), task_id),
    )
    # los links adjuntos también quedan en la carpeta final de Drive, en un txt
    links_saved = False
    links = [dict(r) for r in conn.execute(
        "SELECT label, url FROM task_files WHERE task_id = ? AND kind = 'link'", (task_id,)
    ).fetchall()]
    # también los links que hayan pegado en la descripción o en la nota de entrega
    seen = {l["url"] for l in links}
    for field in (t["description"] or "", t["delivery_note"] or ""):
        for m in re.findall(r"https?://[^\s<>\"']+", field):
            u = m.rstrip(".,;)»”\"'")
            if u not in seen:
                links.append({"label": "Link pegado en la tarea", "url": u})
                seen.add(u)
    if links and t["drive_folder_id"]:
        try:
            body_txt = (
                f"LINKS DEL PROYECTO — {t['title']}\n" + "=" * 50 + "\n\n"
                + "\n".join(f"• {l['label']}\n  {l['url']}\n" for l in links)
            )
            drive_upload_small(conn, "🔗 LINKS DEL PROYECTO.txt", body_txt, t["drive_folder_id"])
            links_saved = True
        except Exception:
            pass  # no bloquear el cierre de la tarea por esto
    cleaned = 0
    if body.clean_server_files:
        rows = conn.execute(
            "SELECT * FROM task_files WHERE task_id = ? AND url LIKE '/uploads/%'", (task_id,)
        ).fetchall()
        for f in rows:
            path = os.path.join(UPLOADS_DIR, os.path.basename(f["url"]))
            if os.path.isfile(path):
                os.remove(path)
            conn.execute("DELETE FROM task_files WHERE id = ?", (f["id"],))
            cleaned += 1
    conn.commit()
    result = task_full(conn, task_id)
    # 🔔 avisar a TODO el equipo: tarea terminada y archivada
    notify_users(conn, all_user_ids(conn),
                 f"✅ <b>Terminada y archivada:</b> {result['title']}\n"
                 f"Canal: {result['channel'] or '—'}\nLa terminó: {user['display_name']}"
                 + ("\n📁 Material guardado en Drive" if result.get("drive_folder_id") else ""),
                 exclude=user["id"], subject=f"✅ Terminada: {result['title']}")
    conn.close()
    result["cleaned_files"] = cleaned
    result["links_saved"] = links_saved
    return result


# ---------------------------------------------------------------- calendario


class CalEvent(BaseModel):
    date: str
    title: str
    url: str = ""


@app.get("/api/calendar")
def calendar_list(start: str = "", end: str = "", user: dict = Depends(get_current_user)):
    """Anotaciones del calendario. Si se dan start/end (YYYY-MM-DD) filtra por rango."""
    conn = db()
    if start and end:
        rows = conn.execute(
            "SELECT * FROM calendar_events WHERE date >= ? AND date <= ? ORDER BY date, id",
            (start, end)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM calendar_events ORDER BY date, id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/calendar")
def calendar_create(body: CalEvent, admin: dict = Depends(require_admin)):
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", body.date.strip()):
        raise HTTPException(400, "Fecha no válida.")
    if not body.title.strip():
        raise HTTPException(400, "Escribe una anotación.")
    url = body.url.strip()
    if url and not (url.startswith("http://") or url.startswith("https://")):
        url = "https://" + url
    conn = db()
    cur = conn.execute(
        "INSERT INTO calendar_events (date, title, url, created_by, created_at) VALUES (?,?,?,?,?)",
        (body.date.strip(), body.title.strip(), url, admin["id"], now()))
    conn.commit()
    row = conn.execute("SELECT * FROM calendar_events WHERE id = ?", (cur.lastrowid,)).fetchone()
    conn.close()
    return dict(row)


@app.patch("/api/calendar/{event_id}")
def calendar_update(event_id: int, body: CalEvent, admin: dict = Depends(require_admin)):
    url = body.url.strip()
    if url and not (url.startswith("http://") or url.startswith("https://")):
        url = "https://" + url
    conn = db()
    cur = conn.execute(
        "UPDATE calendar_events SET date = ?, title = ?, url = ? WHERE id = ?",
        (body.date.strip(), body.title.strip(), url, event_id))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        raise HTTPException(404, "Anotación no encontrada.")
    return {"ok": True}


@app.post("/api/calendar/{event_id}/toggle")
def calendar_toggle(event_id: int, admin: dict = Depends(require_admin)):
    """Marca/desmarca la anotación como lista (verde)."""
    conn = db()
    row = conn.execute("SELECT done FROM calendar_events WHERE id = ?", (event_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Anotación no encontrada.")
    new = 0 if row["done"] else 1
    conn.execute("UPDATE calendar_events SET done = ? WHERE id = ?", (new, event_id))
    conn.commit()
    conn.close()
    return {"ok": True, "done": bool(new)}


@app.delete("/api/calendar/{event_id}")
def calendar_delete(event_id: int, admin: dict = Depends(require_admin)):
    conn = db()
    conn.execute("DELETE FROM calendar_events WHERE id = ?", (event_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


# ---------------------------------------------------------------- notificaciones Telegram


def _tg_api(token: str, method: str, payload: dict = None, timeout: float = 12):
    data = json.dumps(payload).encode() if payload is not None else None
    return _http_json(
        "POST", f"https://api.telegram.org/bot{token}/{method}",
        {"Content-Type": "application/json"}, data, timeout=timeout)


def tg_send_async(token: str, chat_ids: list, text: str):
    """Envía el mensaje en segundo plano para no frenar la respuesta al usuario."""
    def run():
        for cid in chat_ids:
            try:
                _tg_api(token, "sendMessage", {"chat_id": cid, "text": text, "parse_mode": "HTML"})
            except Exception:
                pass
    threading.Thread(target=run, daemon=True).start()


def email_send_async(host_cfg: dict, to_list: list, subject: str, body: str):
    """Envía correos por SMTP (Gmail) en segundo plano."""
    def run():
        try:
            ctx = ssl.create_default_context()
            with smtplib.SMTP(host_cfg["host"], host_cfg["port"], timeout=20) as s:
                s.starttls(context=ctx)
                s.login(host_cfg["user"], host_cfg["pass"])
                for to in to_list:
                    msg = EmailMessage()
                    msg["Subject"] = subject
                    msg["From"] = f'{host_cfg.get("from_name","SERVIDOR YOUTUBE")} <{host_cfg["user"]}>'
                    msg["To"] = to
                    msg.set_content(body)
                    s.send_message(msg)
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()


def smtp_cfg(conn):
    user = cfg_get(conn, "smtp_user")
    pw = cfg_get(conn, "smtp_pass")
    if not (user and pw):
        return None
    return {"host": "smtp.gmail.com", "port": 587, "user": user, "pass": pw,
            "from_name": "SERVIDOR YOUTUBE"}


def notify_users(conn, user_ids, text: str, exclude: int = None, subject: str = "SERVIDOR YOUTUBE"):
    """Notifica por Telegram Y por correo a los usuarios (menos a quien hizo la acción)."""
    ids = {u for u in user_ids if u and u != exclude}
    if not ids:
        return
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT telegram_chat_id, email FROM users WHERE id IN ({marks}) AND active=1",
        list(ids)).fetchall()
    token = cfg_get(conn, "telegram_bot_token")
    if token:
        chats = [r["telegram_chat_id"] for r in rows if r["telegram_chat_id"]]
        if chats:
            tg_send_async(token, chats, text)
    scfg = smtp_cfg(conn)
    if scfg:
        emails = [r["email"] for r in rows if r["email"]]
        if emails:
            # el email va en texto plano (sin las etiquetas HTML de Telegram)
            plain = text.replace("<b>", "").replace("</b>", "")
            email_send_async(scfg, emails, subject, plain)


def admin_ids(conn):
    return [r["id"] for r in conn.execute(
        "SELECT id FROM users WHERE role='admin' AND active=1").fetchall()]


def all_user_ids(conn):
    return [r["id"] for r in conn.execute(
        "SELECT id FROM users WHERE active=1").fetchall()]


class TgToken(BaseModel):
    token: str


@app.get("/api/telegram/status")
def telegram_status(user: dict = Depends(get_current_user)):
    conn = db()
    token = cfg_get(conn, "telegram_bot_token")
    bot = cfg_get(conn, "telegram_bot_username")
    me = conn.execute("SELECT telegram_chat_id FROM users WHERE id = ?", (user["id"],)).fetchone()
    conn.close()
    return {"configured": bool(token), "bot_username": bot or "",
            "linked": bool(me and me["telegram_chat_id"])}


@app.post("/api/telegram/token")
def telegram_set_token(body: TgToken, admin: dict = Depends(require_admin)):
    token = body.token.strip()
    status, _, info = _tg_api(token, "getMe")
    if status != 200 or not info.get("ok"):
        raise HTTPException(400, "Token inválido. Copia el token completo que te dio @BotFather.")
    conn = db()
    cfg_set(conn, "telegram_bot_token", token)
    cfg_set(conn, "telegram_bot_username", info["result"].get("username", ""))
    conn.commit()
    conn.close()
    return {"ok": True, "bot_username": info["result"].get("username", "")}


@app.post("/api/telegram/link")
def telegram_link(user: dict = Depends(get_current_user)):
    """Genera el código de vinculación y el link t.me para este usuario."""
    conn = db()
    token = cfg_get(conn, "telegram_bot_token")
    bot = cfg_get(conn, "telegram_bot_username")
    if not (token and bot):
        conn.close()
        raise HTTPException(400, "El administrador aún no configura el bot de Telegram.")
    code = secrets.token_hex(4)
    conn.execute("UPDATE users SET telegram_code = ? WHERE id = ?", (code, user["id"]))
    conn.commit()
    conn.close()
    return {"code": code, "url": f"https://t.me/{bot}?start={code}"}


@app.post("/api/telegram/verify")
def telegram_verify(user: dict = Depends(get_current_user)):
    """Lee los mensajes nuevos del bot y vincula los códigos /start recibidos."""
    conn = db()
    token = cfg_get(conn, "telegram_bot_token")
    if not token:
        conn.close()
        raise HTTPException(400, "El bot no está configurado.")
    offset = cfg_get(conn, "telegram_offset")
    params = {"timeout": 0}
    if offset:
        params["offset"] = int(offset)
    status, _, info = _tg_api(token, "getUpdates", params)
    if status == 200 and info.get("ok"):
        last = None
        for up in info.get("result", []):
            last = up["update_id"]
            msg = up.get("message") or {}
            text = (msg.get("text") or "").strip()
            chat = msg.get("chat", {}).get("id")
            if text.startswith("/start") and chat:
                parts = text.split()
                code = parts[1] if len(parts) > 1 else ""
                row = conn.execute(
                    "SELECT id, display_name FROM users WHERE telegram_code = ? AND telegram_code != ''",
                    (code,)).fetchone()
                if row:
                    conn.execute(
                        "UPDATE users SET telegram_chat_id = ?, telegram_code = '' WHERE id = ?",
                        (str(chat), row["id"]))
                    tg_send_async(token, [str(chat)],
                                  f"🔔 ¡Hola {row['display_name']}! Notificaciones de SERVIDOR YOUTUBE activadas ✓")
        if last is not None:
            cfg_set(conn, "telegram_offset", str(last + 1))
        conn.commit()
    me = conn.execute("SELECT telegram_chat_id FROM users WHERE id = ?", (user["id"],)).fetchone()
    conn.close()
    return {"linked": bool(me and me["telegram_chat_id"])}


@app.post("/api/telegram/test")
def telegram_test(user: dict = Depends(get_current_user)):
    conn = db()
    token = cfg_get(conn, "telegram_bot_token")
    me = conn.execute("SELECT telegram_chat_id FROM users WHERE id = ?", (user["id"],)).fetchone()
    conn.close()
    if not (token and me and me["telegram_chat_id"]):
        raise HTTPException(400, "Tu Telegram no está vinculado.")
    tg_send_async(token, [me["telegram_chat_id"]], "✅ Prueba de notificación — todo funciona.")
    return {"ok": True}


@app.post("/api/telegram/unlink")
def telegram_unlink(user: dict = Depends(get_current_user)):
    conn = db()
    conn.execute("UPDATE users SET telegram_chat_id = '', telegram_code = '' WHERE id = ?", (user["id"],))
    conn.commit()
    conn.close()
    return {"ok": True}


# ---- correo (SMTP Gmail) ----
class SmtpConfig(BaseModel):
    user: str
    password: str


class EmailBody(BaseModel):
    email: str


@app.get("/api/notify/status")
def notify_status(user: dict = Depends(get_current_user)):
    conn = db()
    token = cfg_get(conn, "telegram_bot_token")
    bot = cfg_get(conn, "telegram_bot_username")
    me = conn.execute("SELECT telegram_chat_id, email FROM users WHERE id = ?", (user["id"],)).fetchone()
    smtp_user = cfg_get(conn, "smtp_user")
    conn.close()
    return {
        "tg_configured": bool(token), "bot_username": bot or "",
        "tg_linked": bool(me and me["telegram_chat_id"]),
        "email_configured": bool(smtp_user), "smtp_from": smtp_user or "",
        "my_email": (me["email"] if me else "") or "",
    }


@app.post("/api/smtp/config")
def smtp_config(body: SmtpConfig, admin: dict = Depends(require_admin)):
    user_ = body.user.strip()
    pw = body.password.strip().replace(" ", "")  # las app-password de Gmail traen espacios
    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as s:
            s.starttls(context=ctx)
            s.login(user_, pw)
    except Exception:
        raise HTTPException(400, "No pude iniciar sesión. Verifica el correo y la contraseña de aplicación de Gmail.")
    conn = db()
    cfg_set(conn, "smtp_user", user_)
    cfg_set(conn, "smtp_pass", pw)
    conn.commit()
    conn.close()
    return {"ok": True, "smtp_from": user_}


@app.post("/api/notify/email")
def set_my_email(body: EmailBody, user: dict = Depends(get_current_user)):
    email = body.email.strip()
    if email and "@" not in email:
        raise HTTPException(400, "Correo no válido.")
    conn = db()
    conn.execute("UPDATE users SET email = ? WHERE id = ?", (email, user["id"]))
    conn.commit()
    conn.close()
    return {"ok": True, "email": email}


@app.post("/api/notify/email-test")
def email_test(user: dict = Depends(get_current_user)):
    conn = db()
    scfg = smtp_cfg(conn)
    me = conn.execute("SELECT email FROM users WHERE id = ?", (user["id"],)).fetchone()
    conn.close()
    if not scfg:
        raise HTTPException(400, "El administrador aún no configura el correo.")
    if not (me and me["email"]):
        raise HTTPException(400, "No has puesto tu correo.")
    email_send_async(scfg, [me["email"]], "Prueba — SERVIDOR YOUTUBE",
                     "✅ Esto es una prueba. Si lo recibes, los avisos por correo funcionan.")
    return {"ok": True}


# ---------------------------------------------------------------- copia de seguridad


@app.get("/api/admin/backup")
def download_backup(admin: dict = Depends(require_admin)):
    """Descarga TODO (base de datos + adjuntos del servidor) en un solo archivo."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(DB_PATH, arcname="servidor.db")
        if os.path.isdir(UPLOADS_DIR):
            tar.add(UPLOADS_DIR, arcname="uploads")
    buf.seek(0)
    fecha = datetime.now().strftime("%Y-%m-%d")
    return Response(
        content=buf.read(), media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="servidor-backup-{fecha}.tar.gz"'},
    )


@app.post("/api/admin/restore")
async def restore_backup(file: UploadFile = File(...), admin: dict = Depends(require_admin)):
    """Restaura una copia de seguridad (reemplaza base de datos y adjuntos).
    Después de restaurar hay que iniciar sesión de nuevo."""
    data = await file.read()
    with tempfile.TemporaryDirectory() as td:
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
                tar.extractall(td, filter="data")
        except Exception:
            raise HTTPException(400, "El archivo no es una copia de seguridad válida (.tar.gz).")
        newdb = os.path.join(td, "servidor.db")
        if not os.path.isfile(newdb):
            raise HTTPException(400, "La copia no contiene servidor.db.")
        # validar que de verdad es la base de datos de esta app
        try:
            check = sqlite3.connect(newdb)
            ok = check.execute("SELECT name FROM sqlite_master WHERE name IN ('tasks','users')").fetchall()
            check.close()
            if len(ok) < 2:
                raise ValueError()
        except Exception:
            raise HTTPException(400, "El archivo no parece una base de datos del SERVIDOR.")
        # copiar primero al mismo disco que la DB y reemplazar de forma atómica
        # (os.replace directo falla si /tmp y /data son discos distintos, como en Docker)
        staging = DB_PATH + ".restore"
        shutil.copy2(newdb, staging)
        os.replace(staging, DB_PATH)
        up = os.path.join(td, "uploads")
        if os.path.isdir(up):
            os.makedirs(UPLOADS_DIR, exist_ok=True)
            for name in os.listdir(up):
                shutil.copy2(os.path.join(up, name), os.path.join(UPLOADS_DIR, name))
    return {"ok": True, "note": "Restaurado. Inicia sesión de nuevo."}


# ---------------------------------------------------------------- frontend

app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/uploads", StaticFiles(directory=UPLOADS_DIR), name="uploads")


@app.get("/")
def index():
    return FileResponse("static/index.html")
