# amusic-web

Web UI for downloading from Apple Music into the Navidrome library. It replaces the `dfam` / `process-amusic-rip` terminal flow.

- **URL:** http://192.168.0.237:4534 on the LAN, or https://amusic.tokyo3.eu from outside. The public hostname goes through the Cloudflare tunnel, with Cloudflare Access (GitHub login) in front; it's managed in `~/terraform/cloudflare-tunnel/services.auto.tfvars`. Either way, sign in with a Navidrome account (admins only, unless `ALLOW_NON_ADMIN=true`).
- **Search or paste:** search Apple Music, or paste a `music.apple.com` album, song, artist or playlist link. Personal library links (`music.apple.com/library/album/l.…`, `/library/playlist/p.…`) work too, using the token of the Apple account logged into wrapper-manager. That's why `wrapper-manager_data` is mounted read-only. Library albums, songs and artists resolve to their catalog item. Library playlists, including private ones, are read track by track from the library, and tracks with no catalog version are skipped.
- **Library badges:** results and tracklists show what's already in Navidrome. Songs are matched by ISRC, albums by name and artist.
- **Download pipeline:** AppleMusicDecrypt rips via `wrapper-manager`, then ALAC→FLAC or AAC→MP3 320. The tag fixes from `process-amusic-rip` are applied and an ISRC tag is ensured. Files land in `/media/downloads/music/(year) - artist - album - WEB`.
- **Staging:** ripping and conversion happen in `/media/downloads/music/.amusic-staging/job-<id>/`, which Navidrome ignores. Only finished files are moved into the album folder, so Navidrome never indexes the temporary `.m4a` files. Indexing them is what used to leave greyed-out "missing" duplicate albums.
- **After each job:** triggers a Navidrome scan, matches the new tracks and links straight to the albums in Navidrome.
- **Playlists:** tracks are saved into their normal album folders, then a Navidrome playlist is created, or updated if one with the same name exists. The job shows a link to the playlist and a "Copy ID" button for its Navidrome playlist ID.
- **Tracks you already have:** one setting in the download dialog. Playlists default to skipping any track already in Navidrome (matched by ISRC, wherever it lives). Albums and everything else default to skipping only files this tool already saved at the same path, so albums come out complete. "Re-download and overwrite" replaces existing files.
- **Problem tracks:** "Retry failed tracks" re-downloads only the tracks that failed or failed the integrity check.
- **wrapper-manager login:** clicking the `wrapper` pill in the header logs an Apple ID into wrapper-manager, with 2FA. This is needed after wrapper-manager restarts.
- **Not included:** torrent creation. Use `dfam` for Orpheus uploads.

## Layout

| Path | Purpose |
|------|---------|
| `app/main.py` | FastAPI app: auth, Apple catalog proxy, jobs API, static UI |
| `app/worker.py` | Runs one job in a subprocess by driving AppleMusicDecrypt directly and emitting JSON events |
| `app/postprocess.py` | Python port of `process-amusic-rip`, without the torrent step |
| `app/navidrome.py` | Subsonic API: login, ISRC/album matching, scans, playlists |
| `app/static/` | Single-page UI (no build step) |
| `/home/lestar/_volumes/amusic-web/config.toml` | AppleMusicDecrypt config used inside the container (wrapper-manager address, naming, codecs) |
| `/home/lestar/_volumes/amusic-web/data/` | SQLite DB: jobs, sessions, Navidrome tokens |

The image bundles AppleMusicDecrypt from the `amd/` git submodule: [liamniou/AppleMusicDecrypt](https://github.com/liamniou/AppleMusicDecrypt), branch `v2`, which carries the local patches. Clone with `git clone --recurse-submodules`.

## Build and deploy

- Every push to `main` builds `ghcr.io/liamniou/amusic-web` (tags `latest` and `sha-<commit>`) in GitHub Actions.
- The deployment lives in [liamniou/gitops](https://github.com/liamniou/gitops) at `eva00/amusic-web/compose.yaml`, pinned as `latest@sha256:…`, and doco-cd deploys it. Dependabot in the gitops repo opens a PR whenever a new image is published; merging it redeploys.
- To pick up AppleMusicDecrypt changes, push them to the fork's `v2` branch, then bump the submodule here (Dependabot proposes this weekly):

```bash
git submodule update --remote amd && git commit -am "Bump amd" && git push
```

## Configuration (environment)

| Variable | Default | Notes |
|----------|---------|-------|
| `NAVIDROME_URL` | `http://192.168.0.237:4533` | Used for API calls |
| `NAVIDROME_PUBLIC_URL` | `https://music.tokyo3.eu` | Used for album and playlist links |
| `APPLE_STOREFRONT` | `tr` | Fallback search storefront. Search normally uses the region of the account logged into wrapper-manager, since only that catalog can be downloaded |
| `ALLOW_NON_ADMIN` | `false` | Lets non-admin Navidrome users queue downloads |
| `SESSION_DAYS` | `30` | Login cookie lifetime |
| `COOKIE_SECURE` | `false` | Set `true` when served only over HTTPS |

## Troubleshooting

- **"wrapper offline" / "no logged-in Apple Music account":** click the pill and log in. Check `docker logs wrapper-manager` for `Wrapper Down` loops.
- **Job log:** each job page has a collapsible log with AppleMusicDecrypt's own output.
- **Validate compose:** `docker compose -f /home/lestar/gitops/eva00/amusic-web/compose.yaml config`
