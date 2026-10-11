#!/usr/bin/env python3
"""Runs a single download job with AppleMusicDecrypt and reports progress as JSON lines.

Usage: worker.py '<json spec>'

stdout carries only JSON events ({"ev": ...}); AppleMusicDecrypt logs go to stderr.
Playlist tracks are saved into their regular album folders so they become part of the library;
the web app then builds the Navidrome playlist from the reported ISRCs.
"""
import asyncio
import json
import os
import re
import shutil
import sys
import traceback
from pathlib import Path

AMD_DIR = os.environ.get("AMD_DIR", "/amd")
APP_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(AMD_DIR)
sys.path.insert(0, AMD_DIR)
sys.path.insert(1, APP_DIR)

_events = sys.stdout
sys.stdout = sys.stderr

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def emit(ev: str, **data):
    _events.write(json.dumps({"ev": ev, **data}, ensure_ascii=False, default=str) + "\n")
    _events.flush()


def _log_sink(msg, end=""):
    sys.stderr.write(ANSI_RE.sub("", str(msg)) + end)
    sys.stderr.flush()


import src.logger as amd_logger  # noqa: E402

amd_logger.ANSI = lambda m: m
amd_logger.print_formatted_text = _log_sink

from creart import add_creator, it  # noqa: E402

add_creator(amd_logger.LoggerCreator)
from src.config import ConfigCreator  # noqa: E402

add_creator(ConfigCreator)
from src.api import APICreator  # noqa: E402

add_creator(APICreator)
from src.grpc.manager import WMCreator  # noqa: E402

add_creator(WMCreator)
from src.measurer import MeasurerCreator  # noqa: E402

add_creator(MeasurerCreator)

import src.rip as rip_mod  # noqa: E402
from src.api import WebAPI  # noqa: E402
from src.config import Config  # noqa: E402
from src.flags import Flags  # noqa: E402
from src.grpc.manager import WrapperManager  # noqa: E402
from src.rip import DownloadManager, Ripper  # noqa: E402
from src.task import Status, Task  # noqa: E402
from src.url import AppleMusicURL, Song, URLType  # noqa: E402
from src.utils import check_dep, get_song_name_and_dir_path, safely_create_task  # noqa: E402

from postprocess import process_file  # noqa: E402

AUDIO_EXTS = (".flac", ".mp3", ".m4a", ".ec3", ".ac3")
LOSSLESS_MISSING = "Lossless audio does not exist"
MUSIC_DIR = Path(os.environ.get("MUSIC_DIR", "/music"))
STAGING_ROOT = MUSIC_DIR / ".amusic-staging"

saved_paths: dict[int, Path] = {}
existing_paths: dict[int, Path] = {}
results: dict[str, dict] = {}
staging_dir: Path | None = None


def to_final(path: Path) -> Path:
    """Map a path inside the job's staging dir to its place in the library."""
    if staging_dir and path.is_relative_to(staging_dir):
        return MUSIC_DIR / path.relative_to(staging_dir)
    return path


def setup_staging(cfg, job_id) -> None:
    # Download/convert outside Navidrome's view (it skips dot-folders and .ndignore'd dirs) so the
    # watcher never indexes temporary .m4a files that would later show up as "missing" albums.
    global staging_dir
    fmt = cfg.download.dirPathFormat
    prefix = str(MUSIC_DIR).rstrip("/") + "/"
    if not fmt.startswith(prefix):
        print(f"dirPathFormat is not under {MUSIC_DIR}; staging disabled", file=sys.stderr)
        return
    STAGING_ROOT.mkdir(exist_ok=True)
    (STAGING_ROOT / ".ndignore").write_text("*\n")
    staging_dir = STAGING_ROOT / f"job-{job_id or os.getpid()}"
    shutil.rmtree(staging_dir, ignore_errors=True)
    staging_dir.mkdir()
    cfg.download.dirPathFormat = str(staging_dir) + "/" + fmt[len(prefix):]


def finalize_staging() -> None:
    """Move finished files into the library: sidecars (cover, lyrics) first, then audio."""
    if not staging_dir or not staging_dir.exists():
        return
    files = [p for p in staging_dir.rglob("*") if p.is_file()]
    files.sort(key=lambda p: p.suffix.lower() in AUDIO_EXTS)
    for src in files:
        dst = to_final(src)
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dst)
    for res in results.values():
        if res.get("path"):
            res["path"] = str(to_final(Path(res["path"])))
    shutil.rmtree(staging_dir, ignore_errors=True)


def _save_hook(orig):
    def save(song, codec, metadata, playlist=None):
        path = orig(song, codec, metadata, playlist)
        saved_paths[id(metadata)] = Path(path)
        return path
    return save


def _exists_hook(metadata, codec, playlist=None):
    # Check the library (not just staging) and treat converted .flac/.mp3 files as existing
    song_name, dir_path = get_song_name_and_dir_path(codec, metadata, playlist)
    for base in dict.fromkeys((to_final(Path(dir_path)), Path(dir_path))):
        for ext in AUDIO_EXTS:
            p = base / (song_name + ext)
            if p.exists():
                existing_paths[id(metadata)] = p
                return True
    return False


_orig_update_status = Task.update_status


def _update_status_hook(self, status):
    prev = self.status
    _orig_update_status(self, status)
    if status != prev and status in (Status.DOWNLOADING, Status.DECRYPTING):
        emit("track", id=self.adamId, status=str(status).lower())


_orig_unregister = DownloadManager.unregister_task


async def _unregister_hook(self, task):
    _record(task)
    await _orig_unregister(self, task)


def _record(task: Task):
    md = task.metadata
    res = {"id": task.adamId}
    if md:
        res.update(title=md.title, artist=md.artist, album=md.album, isrc=md.isrc,
                   track=md.tracknum, disc=md.disk)
    err = str(task.error) if task.error else None
    if task.status == Status.DONE:
        key = id(md)
        if key in saved_paths:
            res.update(status="warning" if err else "saved", path=str(saved_paths[key]), message=err)
        elif key in existing_paths:
            res.update(status="exists", path=str(existing_paths[key]), message="Already downloaded")
        else:
            res.update(status="saved", message=err)
    else:
        res.update(status="failed", message=err or "Unknown error",
                   lossless_missing=bool(err and LOSSLESS_MISSING in err))
    results[task.adamId] = res
    emit("track", **res)


rip_mod.save = _save_hook(rip_mod.save)
rip_mod.check_song_exists = _exists_hook
Task.update_status = _update_status_hook
DownloadManager.unregister_task = _unregister_hook


def _artwork(attrs, size=600):
    art = getattr(attrs, "artwork", None)
    if art and getattr(art, "url", None):
        return art.url.replace("{w}", str(size)).replace("{h}", str(size))
    return None


def _track_dict(tid, attrs, album=None):
    return {
        "id": tid,
        "title": getattr(attrs, "name", None),
        "artist": getattr(attrs, "artistName", None),
        "album": getattr(attrs, "albumName", None) or album,
        "track": getattr(attrs, "trackNumber", None),
        "disc": getattr(attrs, "discNumber", None),
        "isrc": getattr(attrs, "isrc", None),
    }


async def _album_tracks(album_id, storefront, lang):
    info = await it(WebAPI).get_album_info(album_id, storefront, lang)
    d = info.data[0]
    tracks = [_track_dict(t.id, t.attributes, d.attributes.name)
              for t in d.relationships.tracks.data if (t.type or "songs") == "songs"]
    return d, tracks


async def expand(url, lang, include_participate):
    api = it(WebAPI)
    if url.type == URLType.Song:
        s = await api.get_song_info(url.id, url.storefront, lang)
        if not s:
            raise RuntimeError("Song not found")
        emit("info", kind="song", title=s.attributes.name, artist=s.attributes.artistName,
             artwork=_artwork(s.attributes))
        return [_track_dict(s.id, s.attributes)]
    if url.type == URLType.Album:
        d, tracks = await _album_tracks(url.id, url.storefront, lang)
        emit("info", kind="album", title=d.attributes.name, artist=d.attributes.artistName,
             artwork=_artwork(d.attributes))
        return tracks
    if url.type == URLType.Playlist:
        p = await api.get_playlist_info_and_tracks(url.id, url.storefront, lang)
        d = p.data[0]
        tracks = [_track_dict(t.id, t.attributes) for t in d.relationships.tracks.data
                  if (t.type or "songs") == "songs"]
        emit("info", kind="playlist", title=d.attributes.name, artist=d.attributes.curatorName,
             artwork=_artwork(d.attributes))
        return tracks
    if url.type == URLType.Artist:
        a = await api.get_artist_info(url.id, url.storefront, lang)
        emit("info", kind="artist", title=a.data[0].attributes.name, artist=a.data[0].attributes.name,
             artwork=_artwork(a.data[0].attributes))
        tracks = []
        if include_participate:
            for song_url in await api.get_songs_from_artist(url.id, url.storefront, lang):
                s = Song.parse_url(song_url)
                if s:
                    tracks.append({"id": s.id})
        else:
            for album_url in await api.get_albums_from_artist(url.id, url.storefront, lang):
                album = AppleMusicURL.parse_url(album_url)
                if album:
                    _, album_tracks = await _album_tracks(album.id, url.storefront, lang)
                    tracks.extend(album_tracks)
        return tracks
    raise RuntimeError(f"Unsupported URL type: {url.type}")


async def convert_all(ids, spec):
    sem = asyncio.Semaphore(4)

    async def one(tid):
        res = results[tid]
        path = Path(res["path"])
        async with sem:
            try:
                final, label, notes = await asyncio.to_thread(process_file, path, res.get("isrc"))
                res.update(path=str(final), format=label)
                if notes:
                    res["notes"] = notes
            except Exception as e:  # keep the original file, flag the track
                res.update(status="warning", message=f"Conversion failed: {e}")
        emit("track", **res)

    await asyncio.gather(*(one(t) for t in ids))


async def run(spec: dict) -> int:
    ok, missing = check_dep()
    if not ok:
        emit("fatal", message=f"Missing dependency: {missing}")
        return 1

    cfg = it(Config)
    setup_staging(cfg, spec.get("job_id"))
    it(WebAPI)
    wm = it(WrapperManager)
    await wm.init(cfg.instance.url, cfg.instance.secure)
    ripper = Ripper()
    safely_create_task(wm.decrypt_init(on_success=ripper.on_decrypt_success,
                                       on_failure=ripper.on_decrypt_failed))
    try:
        status = await wm.status()
    except Exception as e:
        emit("fatal", message=f"Unable to connect to wrapper-manager at {cfg.instance.url}: {e}")
        return 1
    if not status.regions:
        emit("fatal", message="wrapper-manager has no logged-in Apple Music account")
        return 1

    lang = spec.get("language") or cfg.region.language
    codec = spec.get("codec") or "alac"

    if spec.get("tracks") is not None:
        # Pre-resolved track list (library playlists, which have no catalog URL)
        storefront = spec["storefront"]
        emit("info", **spec.get("info", {}))
        tracks = spec["tracks"]
    else:
        raw_url = spec["url"].strip()
        url = AppleMusicURL.parse_url(raw_url)
        if not url:
            url = AppleMusicURL.parse_url(await it(WebAPI).get_real_url(raw_url))
        if not url:
            emit("fatal", message=f"Unsupported URL: {raw_url}")
            return 1
        storefront = url.storefront
        tracks = await expand(url, lang, spec.get("include_participate", False))

    seen, unique = set(), []
    for t in tracks:
        if t["id"] not in seen:
            seen.add(t["id"])
            unique.append(t)
    only = set(spec.get("only_ids") or [])
    if only:
        unique = [t for t in unique if t["id"] in only]
    skip = set(spec.get("skip_ids") or [])
    emit("tracks", tracks=unique)

    for t in unique:
        if t["id"] in skip:
            results[t["id"]] = {**t, "status": "in_library", "message": "Already in Navidrome"}
            emit("track", **results[t["id"]])
    todo = [t["id"] for t in unique if t["id"] not in skip]

    flags = Flags(force_save=bool(spec.get("force")), language=lang,
                  include_participate_in_works=bool(spec.get("include_participate")))

    async def rip(tid, c):
        await ripper.rip_song(Song(id=tid, storefront=storefront, url="", type=URLType.Song), c, flags)

    await asyncio.gather(*(rip(t, codec) for t in todo))

    if spec.get("fallback_aac") and codec != "aac-legacy":
        retry = [tid for tid in todo if results.get(tid, {}).get("lossless_missing")]
        if retry:
            print(f"Lossless missing for {len(retry)} track(s), retrying with aac-legacy", file=sys.stderr)
            await asyncio.gather(*(rip(t, "aac-legacy") for t in retry))

    if spec.get("convert", True):
        to_convert = [tid for tid, r in results.items()
                      if r.get("status") in ("saved", "warning") and r.get("path", "").endswith(".m4a")]
        if to_convert:
            emit("phase", phase="converting")
            await convert_all(to_convert, spec)

    if staging_dir:
        emit("phase", phase="moving to library")
        await asyncio.to_thread(finalize_staging)
        for res in results.values():
            if res.get("path"):
                emit("track", **res)

    counts = {}
    for r in results.values():
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    emit("done", counts=counts)
    return 0


def main():
    spec = json.loads(sys.argv[1])
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        # The gRPC decrypt stream never ends on its own, so exit without loop cleanup
        code = loop.run_until_complete(run(spec))
    except Exception as e:
        traceback.print_exc()
        emit("fatal", message=str(e) or e.__class__.__name__)
        code = 1
    sys.stderr.flush()
    _events.flush()
    os._exit(code)


if __name__ == "__main__":
    main()
