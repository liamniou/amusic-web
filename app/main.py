import functools
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import apple
import db
import navidrome
import settings
import wm
from jobs import TERMINAL, manager, user_creds

STATIC = Path(__file__).with_name("static")
COOKIE = "amw_session"
CODECS = {"alac", "aac-legacy", "aac", "ec3", "ac3", "aac-binaural", "aac-downmix"}


@asynccontextmanager
async def lifespan(_app):
    db.conn()
    db.purge_sessions()
    manager.start()
    yield


app = FastAPI(title="Apple Music → Navidrome", lifespan=lifespan, docs_url=None, redoc_url=None)


def current_user(request: Request) -> dict:
    sid = request.cookies.get(COOKIE)
    s = db.q1("SELECT * FROM sessions WHERE id = ? AND expires > ?", sid or "", time.time()) if sid else None
    if not s:
        raise HTTPException(401, "Not logged in")
    return s


def creds_for(user: dict) -> navidrome.Creds:
    c = user_creds(user["username"])
    if not c:
        raise HTTPException(401, "Session expired, please log in again")
    return c


def _apple_errors(fn):
    @functools.wraps(fn)
    async def wrapper(*a, **kw):
        try:
            return await fn(*a, **kw)
        except apple.AppleError as e:
            raise HTTPException(404, str(e))
        except httpx.HTTPError as e:
            raise HTTPException(502, f"Apple Music request failed: {e}")
    return wrapper


class LoginBody(BaseModel):
    username: str
    password: str


@app.post("/api/login")
async def login(body: LoginBody, response: Response):
    try:
        info = await navidrome.login(body.username, body.password)
    except navidrome.NavidromeError as e:
        raise HTTPException(401, str(e))
    except httpx.HTTPError:
        raise HTTPException(502, "Navidrome is unreachable")
    if not info.get("isAdmin") and not settings.ALLOW_NON_ADMIN:
        raise HTTPException(403, "Only Navidrome admins can use this app")
    username = info["username"]
    db.ex("INSERT OR REPLACE INTO users (username, subsonic_token, subsonic_salt, is_admin, updated) VALUES (?,?,?,?,?)",
          username, info["subsonicToken"], info["subsonicSalt"], int(bool(info.get("isAdmin"))), time.time())
    sid = secrets.token_urlsafe(32)
    ttl = settings.SESSION_DAYS * 86400
    db.ex("INSERT INTO sessions (id, username, is_admin, expires) VALUES (?,?,?,?)",
          sid, username, int(bool(info.get("isAdmin"))), time.time() + ttl)
    response.set_cookie(COOKIE, sid, max_age=ttl, httponly=True, samesite="lax", secure=settings.COOKIE_SECURE)
    return {"username": username, "isAdmin": bool(info.get("isAdmin"))}


@app.post("/api/logout")
async def logout(request: Request, response: Response):
    sid = request.cookies.get(COOKIE)
    if sid:
        db.ex("DELETE FROM sessions WHERE id = ?", sid)
    response.delete_cookie(COOKIE)
    return {"ok": True}


async def default_storefront() -> str:
    """Region of the Apple account in wrapper-manager; only its catalog is downloadable."""
    st = await wm.status()
    return st["regions"][0].lower() if st.get("regions") else settings.APPLE_STOREFRONT


@app.get("/api/me")
async def me(user=Depends(current_user)):
    return {"username": user["username"], "isAdmin": bool(user["is_admin"]),
            "navidromeUrl": settings.NAVIDROME_PUBLIC_URL, "storefront": await default_storefront()}


@app.get("/api/status")
async def status(user=Depends(current_user)):
    return {"wrapper": await wm.status()}


def require_admin(user=Depends(current_user)) -> dict:
    if not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    return user


class WrapperLoginBody(BaseModel):
    username: str
    password: str


class TwoFABody(BaseModel):
    code: str = Field(min_length=4, max_length=12)


@app.post("/api/wrapper/login")
async def wrapper_login(body: WrapperLoginBody, user=Depends(require_admin)):
    flow = wm.start_login(body.username.strip(), body.password)
    return await flow.wait(45)


@app.get("/api/wrapper/login/{flow_id}")
async def wrapper_login_state(flow_id: str, user=Depends(require_admin)):
    flow = wm.get_flow(flow_id)
    if not flow:
        raise HTTPException(404, "Login session expired, start again")
    return await flow.wait(25)


@app.post("/api/wrapper/login/{flow_id}/2fa")
async def wrapper_login_2fa(flow_id: str, body: TwoFABody, user=Depends(require_admin)):
    flow = wm.get_flow(flow_id)
    if not flow:
        raise HTTPException(404, "Login session expired, start again")
    try:
        await flow.submit_2fa(body.code.strip())
    except ValueError as e:
        raise HTTPException(409, str(e))
    return await flow.wait(45)


@app.get("/api/search")
@_apple_errors
async def search(q: str, user=Depends(current_user)):
    if not q.strip():
        raise HTTPException(400, "Empty query")
    return await apple.search(q.strip(), await default_storefront())


@app.get("/api/resolve")
@_apple_errors
async def resolve(url: str, user=Depends(current_user)):
    ref = apple.parse_url(url) or await apple.resolve_library_url(url)
    if not ref and url.startswith("https://"):
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as c:
            r = await c.get(url)
            ref = apple.parse_url(str(r.url))
    if not ref:
        raise HTTPException(400, "Not an Apple Music album, song, playlist or artist URL")
    return ref


@app.get("/api/{kind}/{storefront}/{item_id}")
@_apple_errors
async def item(kind: str, storefront: str, item_id: str, user=Depends(current_user)):
    if kind == "libplaylist":
        return await apple.library_playlist(item_id)
    fetch = {"album": apple.album, "playlist": apple.playlist, "artist": apple.artist, "song": apple.song}.get(kind)
    if not fetch:
        raise HTTPException(404, "Unknown item type")
    return await fetch(item_id, storefront)


class LibraryCheck(BaseModel):
    albums: list[dict] = Field(default_factory=list, max_length=200)
    songs: list[dict] = Field(default_factory=list, max_length=500)


@app.post("/api/library/check")
async def library_check(body: LibraryCheck, user=Depends(current_user)):
    return await navidrome.check_library(creds_for(user), body.albums, body.songs)


class JobBody(BaseModel):
    url: str
    codec: str = "alac"
    force: bool = False
    fallback_aac: bool = False
    create_playlist: bool = True
    playlist_name: str | None = None
    skip_in_library: bool = False
    include_participate: bool = False
    only_ids: list[str] | None = None
    kind: str | None = None
    title: str | None = None
    artist: str | None = None
    artwork: str | None = None


def _visible(user: dict) -> tuple[str, tuple]:
    return ("", ()) if user["is_admin"] else (" AND username = ?", (user["username"],))


def _get_job(job_id: int, user: dict) -> dict:
    cond, args = _visible(user)
    job = db.q1(f"SELECT * FROM jobs WHERE id = ?{cond}", job_id, *args)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@app.post("/api/jobs")
@_apple_errors
async def create_job(body: JobBody, user=Depends(current_user)):
    ref = apple.parse_url(body.url) or await apple.resolve_library_url(body.url)
    if not ref:
        raise HTTPException(400, "Not an Apple Music URL")
    body.url = ref["url"]
    if ref["type"] == "libplaylist":
        ref["type"] = "playlist"
    if body.codec not in CODECS:
        raise HTTPException(400, "Unsupported codec")
    creds_for(user)
    opts = body.model_dump(exclude={"url", "kind", "title", "artist", "artwork"})
    meta = {"kind": body.kind or ref["type"], "title": body.title, "artist": body.artist, "artwork": body.artwork}
    return {"id": manager.enqueue(user["username"], body.url, opts, meta)}


JOB_LIST_COLS = "id, created, started, finished, username, url, kind, title, artist, artwork, status, phase, message, counts, nd_albums, nd_playlist"


@app.get("/api/jobs")
async def list_jobs(limit: int = 50, user=Depends(current_user)):
    cond, args = _visible(user)
    return db.q(f"SELECT {JOB_LIST_COLS} FROM jobs WHERE 1=1{cond} ORDER BY id DESC LIMIT ?", *args, min(limit, 200))


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: int, user=Depends(current_user)):
    job = _get_job(job_id, user)
    job["log"] = job["log"][-30000:]
    job["tracks"] = db.q("SELECT * FROM tracks WHERE job_id = ? ORDER BY pos", job_id)
    for t in job["tracks"]:
        t["nd_link"] = navidrome.album_link(t["nd_album_id"]) if t["nd_album_id"] else None
    return job


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: int, user=Depends(current_user)):
    _get_job(job_id, user)
    if not manager.cancel(job_id):
        raise HTTPException(409, "Job is not running")
    return {"ok": True}


class RetryBody(BaseModel):
    mode: str = "failed"


@app.post("/api/jobs/{job_id}/retry")
async def retry_job(job_id: int, body: RetryBody, user=Depends(current_user)):
    job = _get_job(job_id, user)
    if job["status"] not in TERMINAL:
        raise HTTPException(409, "Job is still running")
    opts = dict(job["options"] or {})
    if body.mode == "failed":
        tracks = db.q("SELECT id, status FROM tracks WHERE job_id = ?", job_id)
        good = [t["id"] for t in tracks if t["status"] in ("saved", "exists", "in_library")]
        if good and len(good) < len(tracks):
            # Keep the full track list (playlists get rebuilt) but only re-download problem tracks;
            # force overwrites files that failed the integrity check
            opts.update(only_ids=None, skip_ids=good, force=True, skip_in_library=False)
    else:
        opts.update(skip_ids=None)
    meta = {k: job[k] for k in ("kind", "title", "artist", "artwork")}
    return {"id": manager.enqueue(user["username"], job["url"], opts, meta)}


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: int, user=Depends(current_user)):
    job = _get_job(job_id, user)
    if job["status"] not in TERMINAL:
        raise HTTPException(409, "Cancel the job first")
    db.ex("DELETE FROM tracks WHERE job_id = ?", job_id)
    db.ex("DELETE FROM jobs WHERE id = ?", job_id)
    return {"ok": True}


@app.get("/healthz")
async def healthz():
    return {"ok": True}


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/{path:path}")
async def spa(path: str):
    return FileResponse(STATIC / "index.html")
