"""Python port of /usr/local/bin/process-amusic-rip (without the torrent step).

ALAC (.m4a) -> FLAC, AAC (.m4a) -> MP3 320k, then validates/fixes tags.
Also guarantees an ISRC tag so Navidrome songs can be matched back to Apple Music tracks.
"""
import re
import subprocess
from pathlib import Path

from mutagen.flac import FLAC
from mutagen.id3 import ID3, TSRC, ID3NoHeaderError

NUM_RE = re.compile(r"^\d+(/\d+)?$")
DATE_RE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")
DATE_EXTRACT_RE = re.compile(r"\d{4}(-\d{2}(-\d{2})?)?")
FLOAT_RE = re.compile(r"^\d+(\.\d+)?$")
RG_RE = re.compile(r"^-?\d+(\.\d+)?( dB)?$")


def probe_codec(path: Path) -> str:
    res = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True)
    return res.stdout.strip()


def _ffmpeg(args: list[str]):
    res = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
                         capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {res.stderr.strip()[-500:]}")


def _fix_flac_tags(path: Path, isrc: str | None) -> list[str]:
    notes = []
    f = FLAC(path)

    def first(key):
        v = f.get(key)
        return v[0] if v else None

    tn = first("tracknumber")
    if tn and not NUM_RE.match(tn):
        m = re.match(r"(\d+)", path.stem)
        if m:
            f["tracknumber"] = str(int(m.group(1)))
            notes.append(f"TRACKNUMBER '{tn}' -> '{int(m.group(1))}'")
        else:
            del f["tracknumber"]
            notes.append(f"removed malformed TRACKNUMBER '{tn}'")

    dn = first("discnumber")
    if dn and not NUM_RE.match(dn):
        f["discnumber"] = "1"
        notes.append(f"DISCNUMBER '{dn}' -> '1'")

    for key in ("totaltracks", "totaldiscs", "tracktotal", "disctotal"):
        v = first(key)
        if v and not v.isdigit():
            del f[key]
            notes.append(f"removed malformed {key.upper()} '{v}'")

    for key, pattern, extract in (("date", DATE_RE, DATE_EXTRACT_RE), ("year", re.compile(r"^\d{4}$"), re.compile(r"\d{4}"))):
        v = first(key)
        if v and not pattern.match(v):
            m = extract.search(v)
            if m:
                f[key] = m.group(0)
                notes.append(f"{key.upper()} '{v}' -> '{m.group(0)}'")
            else:
                del f[key]
                notes.append(f"removed malformed {key.upper()} '{v}'")

    bpm = first("bpm")
    if bpm and not FLOAT_RE.match(bpm):
        del f["bpm"]
        notes.append(f"removed malformed BPM '{bpm}'")

    comp = first("compilation")
    if comp and comp not in ("0", "1"):
        del f["compilation"]
        notes.append(f"removed malformed COMPILATION '{comp}'")

    for key in ("replaygain_track_gain", "replaygain_track_peak", "replaygain_album_gain", "replaygain_album_peak"):
        v = first(key)
        if v and not RG_RE.match(v):
            del f[key]
            notes.append(f"removed malformed {key.upper()} '{v}'")

    if isrc and not first("isrc"):
        f["isrc"] = isrc

    f.save()
    return notes


def _ensure_mp3_isrc(path: Path, isrc: str | None):
    if not isrc:
        return
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    if not tags.getall("TSRC"):
        tags.add(TSRC(encoding=3, text=[isrc]))
        tags.save(path)


def process_file(path: Path, isrc: str | None = None) -> tuple[Path, str, list[str]]:
    """Convert a downloaded .m4a in place. Returns (final_path, format_label, notes)."""
    if path.suffix.lower() != ".m4a" or not path.exists():
        return path, path.suffix.lstrip(".").upper(), []

    codec = probe_codec(path)
    if codec == "alac":
        out = path.with_suffix(".flac")
        _ffmpeg(["-i", str(path), "-vn", "-c:a", "flac", str(out)])
        path.unlink()
        notes = _fix_flac_tags(out, isrc)
        f = FLAC(out)
        label = f"FLAC {f.info.bits_per_sample}-bit / {f.info.sample_rate / 1000:g} kHz"
        return out, label, notes
    if codec == "aac":
        out = path.with_suffix(".mp3")
        _ffmpeg(["-i", str(path), "-vn", "-c:a", "libmp3lame", "-b:a", "320k", str(out)])
        path.unlink()
        _ensure_mp3_isrc(out, isrc)
        return out, "MP3 320 kbps", []
    return path, f"M4A ({codec or 'unknown codec'})", [f"left as .m4a (codec '{codec}')"]
