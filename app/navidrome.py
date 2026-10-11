import asyncio
import re
import time
import unicodedata

import httpx

from settings import NAVIDROME_PUBLIC_URL, NAVIDROME_URL

CLIENT = "amusic-web"
_http = httpx.AsyncClient(timeout=30)


class NavidromeError(Exception):
    pass


class Creds:
    def __init__(self, username: str, token: str, salt: str):
        self.username, self.token, self.salt = username, token, salt

    def params(self) -> dict:
        return {"u": self.username, "t": self.token, "s": self.salt, "v": "1.16.1", "c": CLIENT, "f": "json"}


async def login(username: str, password: str) -> dict:
    r = await _http.post(f"{NAVIDROME_URL}/auth/login", json={"username": username, "password": password})
    if r.status_code != 200:
        raise NavidromeError("Invalid username or password")
    return r.json()


async def call(creds: Creds, endpoint: str, **params) -> dict:
    p = list(creds.params().items())
    for k, v in params.items():
        if v is None:
            continue
        if isinstance(v, (list, tuple)):
            p.extend((k, x) for x in v)
        else:
            p.append((k, v))
    method = "POST" if len(p) > 60 else "GET"
    if method == "POST":
        r = await _http.post(f"{NAVIDROME_URL}/rest/{endpoint}", data=p)
    else:
        r = await _http.get(f"{NAVIDROME_URL}/rest/{endpoint}", params=p)
    r.raise_for_status()
    body = r.json()["subsonic-response"]
    if body.get("status") != "ok":
        raise NavidromeError(body.get("error", {}).get("message", "Navidrome error"))
    return body


def album_link(album_id: str) -> str:
    return f"{NAVIDROME_PUBLIC_URL}/app/#/album/{album_id}/show"


def playlist_link(playlist_id: str) -> str:
    return f"{NAVIDROME_PUBLIC_URL}/app/#/playlist/{playlist_id}/show"


_EDITION_RE = re.compile(r"\s+-\s+(ep|single)$|\s*[\(\[](deluxe|remaster(ed)?|expanded)[^\)\]]*[\)\]]", re.I)


def norm(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", s or "").lower()
    s = _EDITION_RE.sub("", s)
    return re.sub(r"[^\w]+", "", s)


def _song_isrcs(song: dict) -> set[str]:
    v = song.get("isrc") or []
    return {x.upper() for x in ([v] if isinstance(v, str) else v)}


async def find_song(creds: Creds, isrc: str | None, title: str | None, artist: str | None = None,
                    album: str | None = None) -> dict | None:
    """Find a library song by ISRC; title/album searches narrow the candidate set."""
    if not isrc:
        return None
    isrc = isrc.upper()
    queries = [q for q in (title, f"{title} {artist}" if title and artist else None, album) if q]
    for query in queries:
        try:
            body = await call(creds, "search3", query=query, songCount=100, albumCount=0, artistCount=0)
        except (NavidromeError, httpx.HTTPError):
            continue
        for song in body.get("searchResult3", {}).get("song", []):
            if isrc in _song_isrcs(song):
                return song
    return None


async def find_album(creds: Creds, name: str, artist: str | None) -> dict | None:
    try:
        body = await call(creds, "search3", query=name, albumCount=30, songCount=0, artistCount=0)
    except (NavidromeError, httpx.HTTPError):
        return None
    target, target_artist = norm(name), norm(artist)
    best = None
    for al in body.get("searchResult3", {}).get("album", []):
        if norm(al.get("name")) != target:
            continue
        artists = {norm(al.get("artist"))} | {norm(a.get("name")) for a in al.get("artists", []) or []}
        if target_artist and target_artist not in artists and not any(
                target_artist in a or a in target_artist for a in artists if a):
            continue
        if not best or al.get("songCount", 0) > best.get("songCount", 0):
            best = al
    return best


async def check_library(creds: Creds, albums: list[dict], songs: list[dict]) -> dict:
    sem = asyncio.Semaphore(8)
    out = {"albums": {}, "songs": {}}

    async def chk_album(a):
        async with sem:
            al = await find_album(creds, a.get("name", ""), a.get("artist"))
        if al:
            out["albums"][a["id"]] = {"id": al["id"], "songCount": al.get("songCount", 0),
                                      "link": album_link(al["id"])}

    async def chk_song(s):
        async with sem:
            song = await find_song(creds, s.get("isrc"), s.get("title"), s.get("artist"), s.get("album"))
        if song:
            out["songs"][s["id"]] = {"id": song["id"], "albumId": song.get("albumId"),
                                     "path": song.get("path"), "suffix": song.get("suffix"),
                                     "link": album_link(song["albumId"]) if song.get("albumId") else None}

    await asyncio.gather(*(chk_album(a) for a in albums), *(chk_song(s) for s in songs))
    return out


async def start_scan_and_wait(creds: Creds, timeout: int) -> str:
    """Trigger a quick scan (admin only) and wait for it to finish. Returns a short status note."""
    note = "scan triggered"
    try:
        await call(creds, "startScan", fullScan="false")
    except NavidromeError as e:
        note = f"could not trigger scan ({e}); relying on Navidrome's watcher"
    deadline = time.time() + timeout
    await asyncio.sleep(3)
    while time.time() < deadline:
        try:
            body = await call(creds, "getScanStatus")
            if not body.get("scanStatus", {}).get("scanning"):
                return note
        except (NavidromeError, httpx.HTTPError):
            pass
        await asyncio.sleep(3)
    return note + " (timed out waiting for scan)"


async def upsert_playlist(creds: Creds, name: str, song_ids: list[str], comment: str) -> dict:
    body = await call(creds, "getPlaylists")
    existing = next((p for p in body.get("playlists", {}).get("playlist", [])
                     if p.get("name") == name and p.get("owner") == creds.username), None)
    if existing:
        body = await call(creds, "createPlaylist", playlistId=existing["id"], songId=song_ids)
    else:
        body = await call(creds, "createPlaylist", name=name, songId=song_ids)
    pl = body.get("playlist") or {}
    pid = pl.get("id") or (existing or {}).get("id")
    if pid:
        await call(creds, "updatePlaylist", playlistId=pid, comment=comment)
    return {"id": pid, "name": name, "updated": bool(existing), "link": playlist_link(pid) if pid else None}
