import asyncio
import json
import os
import shutil
import signal
import sys
import time
from pathlib import Path

import apple
import db
import navidrome
from settings import AMD_DIR, SCAN_TIMEOUT

WORKER = str(Path(__file__).with_name("worker.py"))
STAGING_ROOT = Path(os.environ.get("MUSIC_DIR", "/music")) / ".amusic-staging"
OK_STATES = {"saved", "exists", "in_library", "warning"}
TERMINAL = {"completed", "partial", "failed", "cancelled"}


def _cleanup_staging(job_id: int):
    shutil.rmtree(STAGING_ROOT / f"job-{job_id}", ignore_errors=True)


def user_creds(username: str) -> navidrome.Creds | None:
    u = db.q1("SELECT * FROM users WHERE username = ?", username)
    return navidrome.Creds(username, u["subsonic_token"], u["subsonic_salt"]) if u else None


class JobManager:
    def __init__(self):
        self._wake = asyncio.Event()
        self._proc: asyncio.subprocess.Process | None = None
        self._current: int | None = None
        self._cancelled: set[int] = set()

    def start(self):
        db.ex("UPDATE jobs SET status = 'queued', phase = NULL WHERE status IN ('running', 'syncing')")
        for d in STAGING_ROOT.glob("job-*"):
            shutil.rmtree(d, ignore_errors=True)
        asyncio.create_task(self._loop())

    def enqueue(self, username: str, url: str, options: dict, meta: dict) -> int:
        job_id = db.ex(
            "INSERT INTO jobs (created, username, url, kind, title, artist, artwork, options) VALUES (?,?,?,?,?,?,?,?)",
            time.time(), username, url, meta.get("kind"), meta.get("title"), meta.get("artist"),
            meta.get("artwork"), json.dumps(options))
        self._wake.set()
        return job_id

    def cancel(self, job_id: int) -> bool:
        job = db.q1("SELECT status FROM jobs WHERE id = ?", job_id)
        if not job or job["status"] in TERMINAL:
            return False
        self._cancelled.add(job_id)
        if job["status"] == "queued":
            db.update("jobs", {"id": job_id}, status="cancelled", finished=time.time(), message="Cancelled")
        elif self._current == job_id and self._proc and self._proc.returncode is None:
            try:
                os.killpg(self._proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        return True

    async def _loop(self):
        while True:
            job = db.q1("SELECT * FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1")
            if not job:
                self._wake.clear()
                await self._wake.wait()
                continue
            self._current = job["id"]
            try:
                await self._run(job)
            except Exception as e:
                db.append_log(job["id"], f"\n[amusic-web] internal error: {e!r}\n")
                db.update("jobs", {"id": job["id"]}, status="failed", message=str(e), finished=time.time())
            finally:
                self._current = None
                self._proc = None

    async def _run(self, job: dict):
        jid = job["id"]
        opts = job["options"] or {}
        db.ex("DELETE FROM tracks WHERE job_id = ?", jid)
        db.update("jobs", {"id": jid}, status="running", phase="downloading", started=time.time(),
                  finished=None, message=None, log="", counts={}, nd_albums=[], nd_playlist=None)

        spec = {k: opts.get(k) for k in ("codec", "force", "fallback_aac", "language", "only_ids", "skip_ids",
                                         "include_participate")}
        spec.update(url=job["url"], convert=True, job_id=jid)

        lib = apple.parse_library_url(job["url"])
        lib_tracks = None
        if lib and lib["kind"] == "playlists":
            # Library playlists aren't in the catalog: hand the worker the resolved catalog tracks
            db.update("jobs", {"id": jid}, phase="reading library playlist")
            pl = await apple.library_playlist(lib["id"])
            lib_tracks = pl["tracks"]
            spec["storefront"] = pl["storefront"]
            spec["info"] = {"kind": "playlist", "title": pl["name"], "artist": pl["artist"], "artwork": pl["artwork"]}
            spec["tracks"] = [{"id": t["id"], "title": t["name"], "artist": t["artist"], "album": t["album"],
                               "track": t["track"], "disc": t["disc"], "isrc": t["isrc"]} for t in lib_tracks]
            if pl["unavailable"]:
                db.append_log(jid, f"[amusic-web] {pl['unavailable']} playlist track(s) have no catalog version and were skipped\n")

        if opts.get("skip_in_library") and not opts.get("force"):
            db.update("jobs", {"id": jid}, phase="checking library")
            spec["skip_ids"] = list(set(spec.get("skip_ids") or []) | set(await self._in_library_ids(job, lib_tracks)))

        self._proc = await asyncio.create_subprocess_exec(
            sys.executable, WORKER, json.dumps(spec), cwd=str(AMD_DIR),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True, limit=16 * 1024 * 1024)
        fatal = None

        async def read_events():
            nonlocal fatal
            async for line in self._proc.stdout:
                try:
                    ev = json.loads(line)
                except ValueError:
                    db.append_log(jid, line.decode(errors="replace"))
                    continue
                fatal = self._handle_event(jid, ev) or fatal

        async def read_logs():
            async for line in self._proc.stderr:
                db.append_log(jid, line.decode(errors="replace"))

        await asyncio.gather(read_events(), read_logs())
        rc = await self._proc.wait()

        if jid in self._cancelled:
            self._cancelled.discard(jid)
            _cleanup_staging(jid)
            db.ex("UPDATE tracks SET status = 'cancelled' WHERE job_id = ? AND status IN ('queued','downloading','decrypting')", jid)
            db.update("jobs", {"id": jid}, status="cancelled", phase=None, message="Cancelled", finished=time.time())
            self._recount(jid)
            return
        if fatal or rc != 0:
            _cleanup_staging(jid)
            db.update("jobs", {"id": jid}, status="failed", phase=None, finished=time.time(),
                      message=fatal or f"Worker exited with code {rc}")
            self._recount(jid)
            return

        await self._sync_navidrome(jid)

    async def _in_library_ids(self, job: dict, tracks: list[dict] | None = None) -> list[str]:
        """Apple track ids whose ISRC already exists anywhere in the Navidrome library."""
        creds = user_creds(job["username"])
        if not creds:
            return []
        try:
            if tracks is None:
                ref = apple.parse_url(job["url"])
                if not ref or ref["type"] not in ("playlist", "album"):
                    return []
                fetch = apple.playlist if ref["type"] == "playlist" else apple.album
                tracks = (await fetch(ref["id"], ref["storefront"]))["tracks"]
            songs = [{"id": t["id"], "isrc": t["isrc"], "title": t["name"], "artist": t["artist"],
                      "album": t["album"]} for t in tracks]
            res = await navidrome.check_library(creds, [], songs)
            ids = list(res["songs"].keys())
            db.append_log(job["id"], f"[amusic-web] {len(ids)} of {len(songs)} track(s) already in Navidrome, skipping\n")
            return ids
        except Exception as e:
            db.append_log(job["id"], f"[amusic-web] library pre-check failed: {e!r}\n")
            return []

    def _handle_event(self, jid: int, ev: dict) -> str | None:
        kind = ev.get("ev")
        if kind == "info":
            db.update("jobs", {"id": jid}, kind=ev.get("kind"), title=ev.get("title"), artist=ev.get("artist"),
                      artwork=ev.get("artwork"))
        elif kind == "tracks":
            for pos, t in enumerate(ev.get("tracks", [])):
                db.ex("INSERT OR REPLACE INTO tracks (job_id, id, pos, title, artist, album, track, disc, isrc) "
                      "VALUES (?,?,?,?,?,?,?,?,?)", jid, t["id"], pos, t.get("title"), t.get("artist"),
                      t.get("album"), t.get("track"), t.get("disc"), t.get("isrc"))
            self._recount(jid)
        elif kind == "track":
            fields = {k: ev[k] for k in ("status", "message", "path", "format", "title", "artist", "album", "isrc",
                                          "track", "disc") if ev.get(k) is not None}
            if "notes" in ev:
                fields["message"] = "; ".join(filter(None, [fields.get("message"), "tags fixed: " + ", ".join(ev["notes"])]))
            if not db.q1("SELECT 1 FROM tracks WHERE job_id = ? AND id = ?", jid, ev["id"]):
                pos = db.q1("SELECT COUNT(*) AS n FROM tracks WHERE job_id = ?", jid)["n"]
                db.ex("INSERT INTO tracks (job_id, id, pos) VALUES (?,?,?)", jid, ev["id"], pos)
            db.update("tracks", {"job_id": jid, "id": ev["id"]}, **fields)
            self._recount(jid)
        elif kind == "phase":
            db.update("jobs", {"id": jid}, phase=ev.get("phase"))
        elif kind == "fatal":
            return ev.get("message") or "Unknown error"
        return None

    def _recount(self, jid: int):
        rows = db.q("SELECT status, COUNT(*) AS n FROM tracks WHERE job_id = ? GROUP BY status", jid)
        db.update("jobs", {"id": jid}, counts={r["status"]: r["n"] for r in rows})

    async def _sync_navidrome(self, jid: int):
        job = db.q1("SELECT * FROM jobs WHERE id = ?", jid)
        opts = job["options"] or {}
        tracks = db.q("SELECT * FROM tracks WHERE job_id = ? ORDER BY pos", jid)
        good = [t for t in tracks if t["status"] in OK_STATES]
        bad = [t for t in tracks if t["status"] not in OK_STATES]
        creds = user_creds(job["username"])
        notes = []

        if good and creds:
            db.update("jobs", {"id": jid}, status="syncing", phase="scanning")
            if any(t["status"] in ("saved", "warning") for t in good):
                notes.append(await navidrome.start_scan_and_wait(creds, SCAN_TIMEOUT))
            db.update("jobs", {"id": jid}, phase="matching")
            pending = [t for t in good if t["isrc"]]
            for attempt in range(6):
                found = await asyncio.gather(*(navidrome.find_song(creds, t["isrc"], t["title"], t["artist"], t["album"])
                                               for t in pending))
                still = []
                for t, song in zip(pending, found):
                    if song:
                        db.update("tracks", {"job_id": jid, "id": t["id"]}, nd_song_id=song["id"],
                                  nd_album_id=song.get("albumId"))
                    else:
                        still.append(t)
                pending = still
                if not pending:
                    break
                await asyncio.sleep(10)
            if pending:
                notes.append(f"{len(pending)} track(s) not found in Navidrome yet")

            tracks = db.q("SELECT * FROM tracks WHERE job_id = ? ORDER BY pos", jid)
            albums, seen = [], set()
            for t in tracks:
                if t["nd_album_id"] and t["nd_album_id"] not in seen:
                    seen.add(t["nd_album_id"])
                    albums.append({"id": t["nd_album_id"], "name": t["album"], "link": navidrome.album_link(t["nd_album_id"])})
            db.update("jobs", {"id": jid}, nd_albums=albums)

            if job["kind"] == "playlist" and opts.get("create_playlist", True):
                db.update("jobs", {"id": jid}, phase="playlist")
                song_ids = [t["nd_song_id"] for t in tracks if t["nd_song_id"]]
                if song_ids:
                    name = opts.get("playlist_name") or job["title"] or "Apple Music playlist"
                    try:
                        pl = await navidrome.upsert_playlist(creds, name, song_ids, f"Imported from {job['url']}")
                        pl.update(count=len(song_ids), missing=len(tracks) - len(song_ids))
                        db.update("jobs", {"id": jid}, nd_playlist=pl)
                    except Exception as e:
                        notes.append(f"playlist creation failed: {e}")
        elif good and not creds:
            notes.append("no Navidrome credentials stored for job owner; skipped library sync")

        if not tracks:
            status = "failed"
            notes.insert(0, "nothing to download")
        elif bad or any(t["status"] == "warning" for t in tracks):
            status = "partial"
        else:
            status = "completed"
        db.update("jobs", {"id": jid}, status=status, phase=None, finished=time.time(),
                  message="; ".join(n for n in notes if n) or None)
        self._recount(jid)


manager = JobManager()
