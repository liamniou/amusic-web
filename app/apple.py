import asyncio
import re
from urllib.parse import parse_qs, urlparse

import httpx

from settings import APPLE_STOREFRONT, WM_DATA_DIR, apple_language

API = "https://amp-api.music.apple.com/v1/catalog"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
URL_RE = re.compile(r"https://music\.apple\.com/([a-z]{2})/(song|album|playlist|artist)/", re.I)
LIBRARY_RE = re.compile(r"https://music\.apple\.com/(?:[a-z]{2}/)?library/(albums?|songs?|playlists?|artists?)/([^/?#]+)", re.I)

_http = httpx.AsyncClient(timeout=30, headers={"User-Agent": UA, "Origin": "https://music.apple.com"})
_token: str | None = None
_token_lock = asyncio.Lock()


class AppleError(Exception):
    pass


async def _fetch_token() -> str:
    # Same approach as AppleMusicDecrypt: the public web player bundles a developer token
    r = await _http.get("https://music.apple.com", follow_redirects=True)
    js = re.findall(r"/assets/index~[^/]+\.js", r.text)
    if not js:
        raise AppleError("Could not locate Apple Music web player bundle")
    r = await _http.get("https://music.apple.com" + js[0])
    m = re.search(r"(eyJ[A-Za-z0-9-_=]+\.[A-Za-z0-9-_=]+\.[A-Za-z0-9-_=]+)", r.text)
    if not m:
        raise AppleError("Could not extract Apple Music token")
    return m.group(1)


async def _get(path: str, params: dict | None = None) -> dict:
    global _token
    for attempt in range(2):
        if not _token:
            async with _token_lock:
                if not _token:
                    _token = await _fetch_token()
        r = await _http.get(f"{API}/{path}", params={"l": apple_language(), **(params or {})},
                            headers={"Authorization": f"Bearer {_token}"})
        if r.status_code in (401, 403) and attempt == 0:
            _token = None
            continue
        if r.status_code == 404:
            raise AppleError("Not found on Apple Music")
        r.raise_for_status()
        return r.json()
    raise AppleError("Apple Music authorization failed")


def art(attrs: dict, size: int = 300) -> str | None:
    a = attrs.get("artwork") or {}
    u = a.get("url")
    return u.replace("{w}", str(size)).replace("{h}", str(size)).replace("{f}", "jpg") if u else None


def quality(traits: list[str] | None) -> list[str]:
    traits = traits or []
    out = []
    if "atmos" in traits:
        out.append("Atmos")
    if "hi-res-lossless" in traits:
        out.append("Hi-Res")
    elif "lossless" in traits:
        out.append("Lossless")
    if not out:
        out.append("AAC")
    return out


def _album(d: dict) -> dict:
    a = d.get("attributes", {})
    return {"type": "album", "id": d["id"], "name": a.get("name"), "artist": a.get("artistName"),
            "year": (a.get("releaseDate") or "")[:4], "trackCount": a.get("trackCount"),
            "artwork": art(a), "url": a.get("url"), "quality": quality(a.get("audioTraits")),
            "isSingle": a.get("isSingle"), "label": a.get("recordLabel"), "upc": a.get("upc")}


def _song(d: dict) -> dict:
    a = d.get("attributes", {})
    return {"type": "song", "id": d["id"], "name": a.get("name"), "artist": a.get("artistName"),
            "album": a.get("albumName"), "isrc": a.get("isrc"), "track": a.get("trackNumber"),
            "disc": a.get("discNumber"), "duration": a.get("durationInMillis"), "artwork": art(a),
            "url": a.get("url"), "quality": quality(a.get("audioTraits")),
            "year": (a.get("releaseDate") or "")[:4]}


def _artist(d: dict) -> dict:
    a = d.get("attributes", {})
    return {"type": "artist", "id": d["id"], "name": a.get("name"), "artwork": art(a),
            "url": a.get("url"), "genres": a.get("genreNames")}


def _playlist(d: dict) -> dict:
    a = d.get("attributes", {})
    return {"type": "playlist", "id": d["id"], "name": a.get("name"), "artist": a.get("curatorName"),
            "artwork": art(a), "url": a.get("url"),
            "description": (a.get("description") or {}).get("short") or (a.get("description") or {}).get("standard")}


async def search(term: str, storefront: str | None = None, limit: int = 15) -> dict:
    sf = (storefront or APPLE_STOREFRONT).lower()
    data = await _get(f"{sf}/search", {"term": term, "types": "albums,songs,artists,playlists", "limit": limit})
    res = data.get("results", {})
    return {
        "storefront": sf,
        "albums": [_album(d) for d in res.get("albums", {}).get("data", [])],
        "songs": [_song(d) for d in res.get("songs", {}).get("data", [])],
        "artists": [_artist(d) for d in res.get("artists", {}).get("data", [])],
        "playlists": [_playlist(d) for d in res.get("playlists", {}).get("data", [])],
    }


async def _paged(path: str, first: dict, limit: int = 100, cap: int = 1000) -> list[dict]:
    items = list(first.get("data", []))
    nxt = first.get("next")
    while nxt and len(items) < cap:
        data = await _get(path, {"offset": len(items), "limit": limit})
        items.extend(data.get("data", []))
        nxt = data.get("next")
    return items


async def album(album_id: str, storefront: str | None = None) -> dict:
    sf = (storefront or APPLE_STOREFRONT).lower()
    data = await _get(f"{sf}/albums/{album_id}")
    d = data["data"][0]
    tracks_rel = d.get("relationships", {}).get("tracks", {})
    tracks = await _paged(f"{sf}/albums/{album_id}/tracks", tracks_rel, limit=300)
    out = _album(d)
    out["storefront"] = sf
    out["copyright"] = d.get("attributes", {}).get("copyright")
    out["tracks"] = [_song(t) for t in tracks if t.get("type") == "songs"]
    return out


async def playlist(playlist_id: str, storefront: str | None = None) -> dict:
    sf = (storefront or APPLE_STOREFRONT).lower()
    data = await _get(f"{sf}/playlists/{playlist_id}")
    d = data["data"][0]
    tracks_rel = d.get("relationships", {}).get("tracks", {})
    tracks = await _paged(f"{sf}/playlists/{playlist_id}/tracks", tracks_rel)
    out = _playlist(d)
    out["storefront"] = sf
    out["tracks"] = [_song(t) for t in tracks if t.get("type") == "songs"]
    return out


async def artist(artist_id: str, storefront: str | None = None) -> dict:
    sf = (storefront or APPLE_STOREFRONT).lower()
    data = await _get(f"{sf}/artists/{artist_id}")
    out = _artist(data["data"][0])
    albums_data = await _get(f"{sf}/artists/{artist_id}/albums", {"limit": 100})
    albums = await _paged(f"{sf}/artists/{artist_id}/albums", albums_data)
    out["storefront"] = sf
    out["albums"] = sorted((_album(a) for a in albums), key=lambda a: a.get("year") or "", reverse=True)
    return out


async def song(song_id: str, storefront: str | None = None) -> dict:
    sf = (storefront or APPLE_STOREFRONT).lower()
    data = await _get(f"{sf}/songs/{song_id}")
    out = _song(data["data"][0])
    out["storefront"] = sf
    return out


def parse_url(url: str) -> dict | None:
    url = url.strip()
    m = URL_RE.match(url)
    if not m:
        return None
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    storefront, kind, item_id = m.group(1).lower(), m.group(2).lower(), parts[-1]
    qs = parse_qs(parsed.query)
    if kind == "album" and qs.get("i"):
        kind, item_id = "song", qs["i"][0]
    return {"type": kind, "id": item_id, "storefront": storefront, "url": url}


def _user_token() -> str | None:
    # Media-User-Token of the Apple account logged into wrapper-manager
    for path in sorted(WM_DATA_DIR.glob("wrapper/rootfs/data/instances/*/MUSIC_TOKEN")):
        try:
            tok = path.read_text().strip()
        except OSError:
            continue
        if tok:
            return tok
    return None


def parse_library_url(url: str) -> dict | None:
    m = LIBRARY_RE.match((url or "").strip())
    if not m:
        return None
    kind = m.group(1).lower().rstrip("s") + "s"
    return {"kind": kind, "id": m.group(2)}


async def _me_get(path: str, params: dict | None = None) -> dict:
    """GET on the /v1/me API with the wrapper-manager account's user token."""
    tok = _user_token()
    if not tok:
        raise AppleError("Library links need the Apple account logged into wrapper-manager")
    global _token
    for attempt in range(2):
        if not _token:
            async with _token_lock:
                if not _token:
                    _token = await _fetch_token()
        r = await _http.get(f"https://amp-api.music.apple.com/v1/me/{path}", params=params,
                            headers={"Authorization": "Bearer " + _token, "Media-User-Token": tok})
        if r.status_code == 401 and attempt == 0:
            _token = None
            continue
        if r.status_code == 404:
            raise AppleError("Not found in the library of the wrapper-manager account")
        if r.status_code in (401, 403):
            raise AppleError("wrapper-manager account token was rejected; log in again")
        r.raise_for_status()
        return r.json()
    raise AppleError("Apple Music authorization failed")


_user_sf: str | None = None


async def user_storefront() -> str:
    global _user_sf
    if not _user_sf:
        data = await _me_get("storefront")
        _user_sf = data["data"][0]["id"].lower()
    return _user_sf


async def resolve_library_url(url: str) -> dict | None:
    """Map a personal-library link (music.apple.com/library/...) to something the app can open.

    Albums/songs/artists resolve to their catalog item. Playlists stay library playlists: their
    tracks are read from the library, so private (never shared) playlists work too.
    """
    lib = parse_library_url(url)
    if not lib:
        return None
    if lib["kind"] == "playlists":
        return {"type": "libplaylist", "id": lib["id"], "storefront": "me",
                "url": f"https://music.apple.com/library/playlist/{lib['id']}"}
    data = (await _me_get(f"library/{lib['kind']}/{lib['id']}", {"include": "catalog"})).get("data") or []
    catalog = (data[0].get("relationships", {}).get("catalog", {}).get("data") or []) if data else []
    if not catalog:
        name = data[0].get("attributes", {}).get("name") if data else lib["id"]
        raise AppleError(f"“{name}” has no Apple Music catalog version (probably an uploaded file)")
    cat = catalog[0]
    cat_url = cat.get("attributes", {}).get("url") or \
        f"https://music.apple.com/{await user_storefront()}/{lib['kind'][:-1]}/{cat['id']}"
    return parse_url(cat_url)


async def library_playlist(playlist_id: str) -> dict:
    data = (await _me_get(f"library/playlists/{playlist_id}")).get("data") or []
    if not data:
        raise AppleError("Playlist not found in the library")
    a = data[0].get("attributes", {})
    tracks, unavailable, offset = [], 0, 0
    while True:
        page = await _me_get(f"library/playlists/{playlist_id}/tracks",
                             {"include": "catalog", "limit": 100, "offset": offset})
        items = page.get("data") or []
        for t in items:
            cat = t.get("relationships", {}).get("catalog", {}).get("data") or []
            if cat and cat[0].get("attributes"):
                tracks.append(_song(cat[0]))
            else:
                unavailable += 1
        offset += len(items)
        if not page.get("next") or not items:
            break
    desc = a.get("description") or {}
    return {"type": "playlist", "id": playlist_id, "name": a.get("name"), "artist": "Your library",
            "artwork": art(a), "url": f"https://music.apple.com/library/playlist/{playlist_id}",
            "description": desc.get("short") or desc.get("standard"), "storefront": await user_storefront(),
            "tracks": tracks, "unavailable": unavailable, "library": True}
