#!/usr/bin/env python3
"""Find the albums hiding in a folder of photos, from metadata alone.

The agent must never read the pictures themselves -- a single full-resolution
photo can exceed a context window, and a folder of them is hopeless. So nothing
here looks at an image. It reads EXIF timestamps and GPS, clusters them into
events, and prints candidates for a human to choose from. The photos stay on
disk; only numbers and dates come back.

Timestamps come from three sources, best first, because no single one covers a
real folder:

1. A small EXIF reader (stdlib only, no dependencies) for JPEG/TIFF. This is
   the true capture time and the only one worth trusting.
2. `mdls` on macOS, one batched call for whatever step 1 could not read --
   mainly HEIC, whose ISO-BMFF container is a different format entirely and
   which Spotlight has already indexed.
3. The file's modification time, last, and marked low-confidence. A copied or
   exported file has an mtime of when it was copied, which invents events that
   never happened, so anything resting on it is flagged in the output.

No coordinate ever leaves the machine: places are clustered by raw distance and
left unnamed rather than sent to a geocoder.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import struct
import subprocess
import sys
from datetime import datetime, timedelta

ALLOWED = {".jpg", ".jpeg", ".png", ".heic", ".tiff", ".tif", ".webp"}
#: Formats the stdlib reader understands. Everything else goes to `mdls`.
EXIF_READABLE = {".jpg", ".jpeg", ".tiff", ".tif"}

#: Files that are pictures but almost never album material. Reported, not hidden.
JUNK = re.compile(r"^(screenshot|screen shot|image_\d+|photo_\d+|img_\d{4}\(\d\))", re.I)

EXIF_HEAD_BYTES = 256 * 1024   # EXIF lives at the front of the file


# ── EXIF (stdlib) ──────────────────────────────────────────────────────────

def _rational(blob, off, endian):
    num, den = struct.unpack(endian + "II", blob[off:off + 8])
    return num / den if den else 0.0


def _ifd_entries(blob, offset, endian):
    """Yield (tag, type, count, value_offset) for one IFD."""
    if offset + 2 > len(blob):
        return
    (count,) = struct.unpack(endian + "H", blob[offset:offset + 2])
    for i in range(count):
        e = offset + 2 + i * 12
        if e + 12 > len(blob):
            return
        tag, typ, cnt = struct.unpack(endian + "HHI", blob[e:e + 8])
        yield tag, typ, cnt, e + 8


def _ascii(blob, endian, typ, cnt, voff):
    if typ != 2:
        return None
    if cnt <= 4:
        raw = blob[voff:voff + cnt]
    else:
        (ptr,) = struct.unpack(endian + "I", blob[voff:voff + 4])
        raw = blob[ptr:ptr + cnt]
    return raw.split(b"\x00")[0].decode("ascii", "replace").strip() or None


def _gps_coord(blob, endian, cnt, voff):
    """Three rationals -> signed decimal degrees (sign applied by caller)."""
    if cnt != 3:
        return None
    (ptr,) = struct.unpack(endian + "I", blob[voff:voff + 4])
    if ptr + 24 > len(blob):
        return None
    d = _rational(blob, ptr, endian)
    m = _rational(blob, ptr + 8, endian)
    s = _rational(blob, ptr + 16, endian)
    return d + m / 60 + s / 3600


def read_exif(path):
    """Return {taken, lat, lon, model} from a JPEG/TIFF, any field possibly None."""
    out = {"taken": None, "lat": None, "lon": None, "model": None}
    try:
        with open(path, "rb") as fh:
            head = fh.read(EXIF_HEAD_BYTES)
    except OSError:
        return out

    if head[:2] == b"\xff\xd8":                      # JPEG: walk to APP1
        i, tiff = 2, None
        while i + 4 <= len(head):
            if head[i] != 0xFF:
                break
            marker = head[i + 1]
            (seglen,) = struct.unpack(">H", head[i + 2:i + 4])
            if marker == 0xE1 and head[i + 4:i + 10] == b"Exif\x00\x00":
                tiff = i + 10
                break
            if marker in (0xD8, 0xD9) or seglen < 2:
                break
            i += 2 + seglen
        if tiff is None:
            return out
        blob = head[tiff:]
    elif head[:2] in (b"II", b"MM"):                 # bare TIFF
        blob = head
    else:
        return out

    if blob[:2] == b"II":
        endian = "<"
    elif blob[:2] == b"MM":
        endian = ">"
    else:
        return out
    try:
        (ifd0,) = struct.unpack(endian + "I", blob[4:8])
        exif_ptr = gps_ptr = None
        for tag, typ, cnt, voff in _ifd_entries(blob, ifd0, endian):
            if tag == 0x8769:
                (exif_ptr,) = struct.unpack(endian + "I", blob[voff:voff + 4])
            elif tag == 0x8825:
                (gps_ptr,) = struct.unpack(endian + "I", blob[voff:voff + 4])
            elif tag == 0x0110:
                out["model"] = _ascii(blob, endian, typ, cnt, voff)

        if exif_ptr:
            for tag, typ, cnt, voff in _ifd_entries(blob, exif_ptr, endian):
                # 0x9003 DateTimeOriginal, 0x9004 DateTimeDigitized
                if tag in (0x9003, 0x9004) and out["taken"] is None:
                    raw = _ascii(blob, endian, typ, cnt, voff)
                    if raw:
                        try:
                            out["taken"] = datetime.strptime(raw[:19], "%Y:%m:%d %H:%M:%S")
                        except ValueError:
                            pass

        if gps_ptr:
            lat = lon = None
            lat_ref = lon_ref = ""
            for tag, typ, cnt, voff in _ifd_entries(blob, gps_ptr, endian):
                if tag == 0x0001:
                    lat_ref = _ascii(blob, endian, typ, cnt, voff) or ""
                elif tag == 0x0002:
                    lat = _gps_coord(blob, endian, cnt, voff)
                elif tag == 0x0003:
                    lon_ref = _ascii(blob, endian, typ, cnt, voff) or ""
                elif tag == 0x0004:
                    lon = _gps_coord(blob, endian, cnt, voff)
            if lat is not None and lon is not None:
                out["lat"] = -lat if lat_ref.upper().startswith("S") else lat
                out["lon"] = -lon if lon_ref.upper().startswith("W") else lon
    except (struct.error, IndexError, ValueError):
        return out
    return out


# ── macOS Spotlight fallback ───────────────────────────────────────────────

MDLS_DATE = re.compile(r"kMDItemContentCreationDate\s*=\s*([\d]{4}-[\d]{2}-[\d]{2} [\d:]{8})")
MDLS_LAT = re.compile(r"kMDItemLatitude\s*=\s*(-?[\d.]+)")
MDLS_LON = re.compile(r"kMDItemLongitude\s*=\s*(-?[\d.]+)")


ATTRS = ("kMDItemContentCreationDate", "kMDItemLatitude", "kMDItemLongitude")


def _parse_mdls_block(lines):
    rec = {"taken": None, "lat": None, "lon": None}
    text = "\n".join(lines)
    m = MDLS_DATE.search(text)
    if m:
        try:
            rec["taken"] = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    for key, rx in (("lat", MDLS_LAT), ("lon", MDLS_LON)):
        mm = rx.search(text)
        if mm:
            try:
                rec[key] = float(mm.group(1))
            except ValueError:
                pass
    return rec


def _run_mdls(paths):
    try:
        done = subprocess.run(
            ["mdls"] + [a for attr in ATTRS for a in ("-name", attr)] + paths,
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout


def mdls_batch(paths, chunk=200):
    """{path: {taken, lat, lon}} from Spotlight. macOS only; {} elsewhere.

    With several files and `-name` flags, mdls prints exactly one line per
    requested attribute per file, in the order the files were given, with no
    separator and no filename. So the only safe parse is a fixed stride of
    len(ATTRS) lines. If the line count does not match, the alignment is
    unknown -- and a misaligned parse silently hands every file its neighbour's
    date, inventing events that never happened -- so fall back to one call per
    file rather than guess.
    """
    found = {}
    if sys.platform != "darwin" or not paths:
        return found
    stride = len(ATTRS)
    for i in range(0, len(paths), chunk):
        batch = paths[i:i + chunk]
        out = _run_mdls(batch)
        if out is None:
            return found
        lines = out.splitlines()
        if len(lines) == stride * len(batch):
            for n, path in enumerate(batch):
                rec = _parse_mdls_block(lines[n * stride:(n + 1) * stride])
                if rec["taken"] or rec["lat"] is not None:
                    found[path] = rec
            continue
        # Alignment lost: redo this batch one file at a time, where a block
        # cannot belong to anyone else.
        for path in batch:
            single = _run_mdls([path])
            if single is None:
                continue
            rec = _parse_mdls_block(single.splitlines())
            if rec["taken"] or rec["lat"] is not None:
                found[path] = rec
    return found


# ── the macOS Photos library ───────────────────────────────────────────────

DEFAULT_LIBRARY = "~/Pictures/Photos Library.photoslibrary"
CORE_DATA_EPOCH = datetime(2001, 1, 1)

FDA_HELP = """Cannot read the Photos catalogue. macOS guards it, so the app running
this command needs Full Disk Access:

  System Settings -> Privacy & Security -> Full Disk Access
  -> switch on the app running this (Terminal, iTerm, or the Claude app)
  -> QUIT AND REOPEN that app; the grant only applies to a fresh launch

If you would rather not grant that -- it is a broad permission, covering far
more than photos -- export from Photos instead and scan the folder:
  in Photos select the pictures, File -> Export -> Export Unmodified Original,
  then run this with --folder on that folder."""


def read_photos_library(lib_path):
    """(photos, stats) straight from the Photos catalogue. Read-only.

    Photos has already extracted the capture date and GPS of every asset, so
    this needs no EXIF parsing and covers HEIC, which the reader above cannot
    open. It also covers pictures that are NOT on this Mac: with iCloud's
    "Optimise Storage" the originals live in the cloud, but their metadata is
    still in the catalogue, so a library can be summarised in full and only the
    chosen album ever has to come down.

    Opened `immutable=1`: no lock, no WAL file, nothing written. Photos can be
    running. The trade is that a write happening right now may not be visible,
    which for grouping years of photos does not matter.
    """
    root = os.path.expanduser(lib_path)
    db = os.path.join(root, "database", "Photos.sqlite")
    if not os.path.exists(db):
        raise SystemExit(f"error: no Photos catalogue at {db}\n\n{FDA_HELP}")
    try:
        con = sqlite3.connect(f"file:{db}?immutable=1", uri=True)
        cur = con.cursor()
        tables = {r[0].upper() for r in
                  cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.Error:
        raise SystemExit(FDA_HELP)

    # ZASSET on current macOS, ZGENERICASSET on older ones.
    table = "ZASSET" if "ZASSET" in tables else ("ZGENERICASSET" if "ZGENERICASSET" in tables else None)
    if not table:
        raise SystemExit("error: unfamiliar Photos catalogue (no ZASSET table). "
                         "Export to a folder and use --folder instead.")
    cols = {r[1].upper() for r in cur.execute(f"PRAGMA table_info({table})")}

    def have(name):
        return name if name in cols else None

    date_col = have("ZDATECREATED") or have("ZMODIFICATIONDATE")
    if not date_col:
        raise SystemExit("error: Photos catalogue has no date column this build knows. "
                         "Export to a folder and use --folder instead.")
    lat_col, lon_col = have("ZLATITUDE"), have("ZLONGITUDE")
    dir_col, file_col = have("ZDIRECTORY"), have("ZFILENAME")

    select = [date_col, lat_col or "NULL", lon_col or "NULL",
              dir_col or "NULL", file_col or "NULL"]
    where = ["1=1"]
    # Skip the bin, the Hidden album and videos -- all three are reliably wrong
    # for an album, and each column is optional across macOS versions.
    if "ZTRASHEDSTATE" in cols:
        where.append("ZTRASHEDSTATE = 0")
    if "ZHIDDEN" in cols:
        where.append("ZHIDDEN = 0")
    if "ZKIND" in cols:
        where.append("ZKIND = 0")

    photos, missing, undated = [], 0, 0
    for row in cur.execute(f"SELECT {', '.join(select)} FROM {table} WHERE {' AND '.join(where)}"):
        stamp, lat, lon, folder, name = row
        if stamp is None:
            undated += 1
            continue
        try:
            taken = CORE_DATA_EPOCH + timedelta(seconds=float(stamp))
        except (TypeError, ValueError, OverflowError):
            undated += 1
            continue
        # Photos stores -180 for "no location", not NULL.
        if lat is None or lon is None or lat <= -180 or lon <= -180:
            lat = lon = None
        path = (os.path.join(root, "originals", folder, name)
                if folder and name else None)
        on_disk = bool(path) and os.path.exists(path)
        if not on_disk:
            missing += 1
        photos.append({
            "path": path or f"<in Photos: {name or 'unknown'}>",
            "taken": taken, "lat": lat, "lon": lon, "model": None,
            "source": "photos-library",
            "size": (os.path.getsize(path) if on_disk else 0),
            "folder": "Photos Library",
            "on_disk": on_disk,
        })
    con.close()
    return photos, {"total": len(photos), "icloud_only": missing, "undated": undated}


# ── clustering ─────────────────────────────────────────────────────────────

def haversine_km(a, b):
    lat1, lon1 = a
    lat2, lon2 = b
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def cluster(photos, gap_hours, place_km):
    """Split a time-sorted list wherever there is a long gap or a big move."""
    events, current = [], []
    for p in photos:
        if not current:
            current = [p]
            continue
        prev = current[-1]
        gap = (p["taken"] - prev["taken"]).total_seconds() / 3600.0
        moved = False
        if p.get("lat") is not None and prev.get("lat") is not None:
            moved = haversine_km((prev["lat"], prev["lon"]), (p["lat"], p["lon"])) > place_km
        if gap > gap_hours or moved:
            events.append(current)
            current = [p]
        else:
            current.append(p)
    if current:
        events.append(current)
    return events


def trips(events, trip_gap_days):
    """Group events into trips.

    A holiday is one album, not seven. The place rule that correctly separates
    a beach day from a city day also fires every time you change island, so a
    three-week trip arrives here shredded. Trips re-join purely on time: if the
    next event starts within `trip_gap_days` of the last one ending, it is the
    same journey, however far it moved. That is the whole point of a trip.
    """
    out, current = [], []
    for e in sorted(events, key=lambda e: e["start"]):
        if not current:
            current = [e]
            continue
        prev_end = datetime.fromisoformat(current[-1]["end"])
        this_start = datetime.fromisoformat(e["start"])
        if (this_start - prev_end) <= timedelta(days=trip_gap_days):
            current.append(e)
        else:
            out.append(current)
            current = [e]
    if current:
        out.append(current)
    return out


def merge_trip(stops):
    """One trip summary from its stops, keeping the stops for context."""
    files, photos, size = [], 0, 0
    nd = 0
    located = []
    cameras, folders = set(), set()
    weak = junk = 0
    for s in stops:
        files.extend(s["files"])
        photos += s["photos"]
        size += s["bytes"]
        cameras.update(s["cameras"])
        nd = nd + s.get("not_downloaded", 0)
        folders.add(s["folder"])
        weak += s["weak_dates"]
        junk += s["junk_named"]
        if s["centroid"]:
            located.append((s["centroid"], s["gps_photos"]))
    centroid = None
    if located:
        total = sum(n for _, n in located)
        centroid = (
            sum(c[0] * n for c, n in located) / total,
            sum(c[1] * n for c, n in located) / total,
        )
    start, end = stops[0]["start"], stops[-1]["end"]
    return {
        "start": start, "end": end,
        "days": (datetime.fromisoformat(end).date()
                 - datetime.fromisoformat(start).date()).days + 1,
        "photos": photos, "bytes": size, "stops": len(stops),
        "folder": stops[0]["folder"], "folders": len(folders),
        "has_gps": bool(located),
        "centroid": [round(centroid[0], 4), round(centroid[1], 4)] if centroid else None,
        "cameras": sorted(cameras), "weak_dates": weak, "junk_named": junk,
        "not_downloaded": nd, "files": files,
        "stop_detail": [{"when": month_label(s["start"], s["end"]),
                         "photos": s["photos"],
                         "centroid": s["centroid"]} for s in stops],
    }


def describe(event):
    start = event[0]["taken"]
    end = event[-1]["taken"]
    days = (end.date() - start.date()).days + 1
    located = [p for p in event if p.get("lat") is not None]
    centroid = None
    spread = 0.0
    if located:
        centroid = (
            sum(p["lat"] for p in located) / len(located),
            sum(p["lon"] for p in located) / len(located),
        )
        spread = max(haversine_km(centroid, (p["lat"], p["lon"])) for p in located)
    folders = {}
    for p in event:
        folders[p["folder"]] = folders.get(p["folder"], 0) + 1
    top_folder = max(folders, key=folders.get)
    return {
        "start": start.isoformat(sep=" ", timespec="minutes"),
        "end": end.isoformat(sep=" ", timespec="minutes"),
        "days": days,
        "photos": len(event),
        "folder": top_folder,
        "folders": len(folders),
        "has_gps": bool(located),
        "gps_photos": len(located),
        "centroid": [round(centroid[0], 4), round(centroid[1], 4)] if centroid else None,
        "spread_km": round(spread, 1) if located else None,
        "cameras": sorted({p["model"] for p in event if p.get("model")}),
        "weak_dates": sum(1 for p in event if p["source"] == "mtime"),
        "not_downloaded": sum(1 for p in event if p.get("on_disk") is False),
        "junk_named": sum(1 for p in event if JUNK.match(os.path.basename(p["path"]))),
        "bytes": sum(p["size"] for p in event),
        "files": [p["path"] for p in event],
    }


def human_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024.0


def month_label(start, end):
    s, e = datetime.fromisoformat(start), datetime.fromisoformat(end)
    if s.date() == e.date():
        return s.strftime("%-d %B %Y")
    if (s.year, s.month) == (e.year, e.month):
        return f"{s.day}–{e.day} {s.strftime('%B %Y')}"
    if s.year == e.year:
        return f"{s.strftime('%-d %b')} – {e.strftime('%-d %b %Y')}"
    return f"{s.strftime('%-d %b %Y')} – {e.strftime('%-d %b %Y')}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--folder", help="A folder of photo files to scan")
    ap.add_argument("--photos-library", nargs="?", const=DEFAULT_LIBRARY, metavar="PATH",
                    help="Scan the macOS Photos library instead of a folder "
                         f"(default {DEFAULT_LIBRARY}). Needs Full Disk Access.")
    ap.add_argument("--gap-hours", type=float, default=20.0,
                    help="A gap longer than this starts a new event (default 20)")
    ap.add_argument("--place-km", type=float, default=120.0,
                    help="A jump farther than this starts a new event (default 120)")
    ap.add_argument("--trip-gap-days", type=float, default=3.0,
                    help="Bursts within this many days of each other are one trip (default 3)")
    ap.add_argument("--min-photos", type=int, default=12,
                    help="Events smaller than this are summarised, not proposed")
    ap.add_argument("--max-events", type=int, default=12)
    ap.add_argument("--no-recursive", action="store_true")
    ap.add_argument("--json", metavar="PATH", help="Also write the full result, with file lists")
    args = ap.parse_args()

    if not args.folder and not args.photos_library:
        print("error: give --folder <dir>, or --photos-library to scan the "
              "macOS Photos library", file=sys.stderr)
        return 2

    if args.photos_library:
        photos, stats = read_photos_library(args.photos_library)
        if not photos:
            print("The Photos library has no dated photos.")
            return 1
        print(f"Photos library : {stats['total']} photos")
        if stats["undated"]:
            print(f"                 ({stats['undated']} skipped, no date recorded)")
        on_disk = stats["total"] - stats["icloud_only"]
        print(f"on this Mac    : {on_disk}")
        if stats["icloud_only"]:
            pct = 100 * stats["icloud_only"] // stats["total"]
            print(f"in iCloud only : {stats['icloud_only']} ({pct}%) — their dates and "
                  f"places are known, so\n                 every suggestion below is "
                  f"complete, but uploading one\n                 means downloading "
                  f"those originals first (see below).")
        return report(photos, args)

    if not os.path.isdir(args.folder):
        print(f"error: not a directory: {args.folder}", file=sys.stderr)
        return 2

    # 1. enumerate
    found = []
    walker = os.walk(args.folder) if not args.no_recursive else [
        (args.folder, [], os.listdir(args.folder))]
    for root, dirs, names in walker:
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in names:
            if name.startswith("."):
                continue
            if os.path.splitext(name)[1].lower() not in ALLOWED:
                continue
            path = os.path.join(root, name)
            if os.path.isfile(path):
                found.append(path)
    if not found:
        print("No photos found.")
        return 1

    print(f"scanning {len(found)} files in {args.folder} ...", flush=True)

    # 2. read metadata, best source first
    photos, needs_fallback = [], []
    for path in found:
        ext = os.path.splitext(path)[1].lower()
        meta = read_exif(path) if ext in EXIF_READABLE else {
            "taken": None, "lat": None, "lon": None, "model": None}
        if meta["taken"]:
            photos.append({"path": path, "taken": meta["taken"], "lat": meta["lat"],
                           "lon": meta["lon"], "model": meta["model"], "source": "exif",
                           "size": os.path.getsize(path),
                           "folder": os.path.basename(os.path.dirname(path))})
        else:
            needs_fallback.append(path)

    spotlight = mdls_batch(needs_fallback)
    still_missing = []
    for path in needs_fallback:
        rec = spotlight.get(path)
        if rec and rec["taken"]:
            photos.append({"path": path, "taken": rec["taken"], "lat": rec["lat"],
                           "lon": rec["lon"], "model": None, "source": "spotlight",
                           "size": os.path.getsize(path),
                           "folder": os.path.basename(os.path.dirname(path))})
        else:
            still_missing.append(path)
    for path in still_missing:
        photos.append({"path": path,
                       "taken": datetime.fromtimestamp(os.path.getmtime(path)),
                       "lat": None, "lon": None, "model": None, "source": "mtime",
                       "size": os.path.getsize(path),
                       "folder": os.path.basename(os.path.dirname(path))})

    by_source = {}
    for p in photos:
        by_source[p["source"]] = by_source.get(p["source"], 0) + 1
    print("dates from   : " + ", ".join(f"{k} {v}" for k, v in sorted(by_source.items())))
    if by_source.get("mtime"):
        print(f"               ({by_source['mtime']} have no capture time — file dates "
              f"are when they were copied, so their grouping is a guess)")

    return report(photos, args)


def report(photos, args):
    # 3. cluster
    photos.sort(key=lambda p: p["taken"])
    events = [describe(e) for e in cluster(photos, args.gap_hours, args.place_km)]
    merged = [merge_trip(stops) for stops in trips(events, args.trip_gap_days)]
    merged.sort(key=lambda e: e["photos"], reverse=True)
    big = [e for e in merged if e["photos"] >= args.min_photos]
    small = [e for e in merged if e["photos"] < args.min_photos]

    shown = min(len(big), args.max_events)
    print(f"\nfound {len(events)} bursts, grouped into {len(merged)} trips; "
          f"{len(big)} hold at least {args.min_photos} photos.")
    print(f"Showing the {shown} biggest — raise --min-photos or --max-events to "
          f"see more.\n")
    print(f"{'#':>3}  {'WHEN':30} {'PHOTOS':>7} {'SIZE':>9}  WHERE / NOTES")
    print("-" * 104)
    for n, e in enumerate(big[:args.max_events], 1):
        notes = []
        if e["has_gps"]:
            notes.append(f"GPS {e['centroid'][0]:.2f},{e['centroid'][1]:.2f}")
        else:
            notes.append("no GPS")
        if e["days"] > 1:
            notes.append(f"{e['days']} days")
        if e.get("stops", 1) > 1:
            notes.append(f"{e['stops']} stops")
        if e["folders"] > 1:
            notes.append(f"{e['folders']} folders")
        if e["weak_dates"]:
            notes.append(f"{e['weak_dates']} weak dates")
        if e["junk_named"]:
            notes.append(f"{e['junk_named']} screenshot-ish")
        if e.get("not_downloaded"):
            notes.append(f"{e['not_downloaded']} to download")
        size = (human_bytes(e["bytes"]) if not e.get("not_downloaded")
                else ("—" if e["bytes"] == 0 else f"{human_bytes(e['bytes'])}+"))
        print(f"{n:3}  {month_label(e['start'], e['end']):30} {e['photos']:>7} "
              f"{size:>9}  {' · '.join(notes)}")
        print(f"     {'':30} {'':7} {'':9}  in {e['folder']}/")
        for stop in (e.get("stop_detail") or [])[:6]:
            where = (f"{stop['centroid'][0]:.2f},{stop['centroid'][1]:.2f}"
                     if stop["centroid"] else "no GPS")
            print(f"     {'':30} {'':7} {'':9}    · {stop['when']}, "
                  f"{stop['photos']} photos, {where}")
    if small:
        loose = sum(e["photos"] for e in small)
        print(f"\n     plus {len(small)} smaller clusters holding {loose} photos "
              f"(under --min-photos {args.min_photos})")

    if args.json:
        with open(args.json, "w") as fh:
            json.dump({"folder": args.folder, "events": big, "small": small}, fh, indent=2)
        print(f"\nfull result (with file lists) -> {args.json}")

    pending = sum(e.get("not_downloaded", 0) for e in big[:args.max_events])
    print("\nNothing has been uploaded and nothing was changed in Photos.")
    if pending:
        print(f"\n{pending} of the photos above are not on this Mac. To upload one of")
        print("these albums, get its originals down first — in Photos, select the")
        print("date range, then File -> Export -> Export Unmodified Original into a")
        print("folder, and scan that folder with --folder instead. Exporting is what")
        print("pulls them out of iCloud; this scan deliberately does not touch them.")
    print("\nWhen the files are on disk, upload just one event with:")
    print("  upload_folder.py --endpoint <url> --files-from <json> --event <n>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
