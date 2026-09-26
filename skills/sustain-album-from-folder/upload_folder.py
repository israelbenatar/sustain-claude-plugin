#!/usr/bin/env python3
"""Upload a folder of photos to a Sustain album through a public upload ticket.

The endpoint is rate limited at 20 requests/hour, so the only thing that makes a
folder of any size uploadable is batching: as many files per request as the
server will take. The caps below come from the server, not from taste --
`SKILL.md` lists each one with its source. Staying under them is why this script
exists instead of a loop of `curl` calls.

Shells out to curl rather than building multipart bodies here: curl streams each
file from disk, so a 400-photo folder never lands in memory.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

#: Server-side truth (`curation.album_views.ALLOWED_EXTENSIONS`). `.heif` and
#: `.gif` are deliberately absent -- the server rejects them.
ALLOWED = {".jpg", ".jpeg", ".png", ".heic", ".tiff", ".tif", ".webp"}

#: Per-request ceilings, set below the server's so a boundary case cannot turn
#: a whole batch into a 413. Server: 100 files, 200MB body.
MAX_FILES = 60
MAX_BYTES = 150 * 1024 * 1024

#: The server's floor. Paid plans raise it, so an oversized file is reported as
#: a likely rejection rather than skipped outright.
FILE_FLOOR = 50 * 1024 * 1024

#: `qr_upload` throttle. Exceeding it earns a 429 and an hour's wait.
REQUEST_BUDGET = 20


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024.0
    return f"{n:.1f}GB"


def from_scan(scan_path: str, event: int) -> list[str]:
    """The file list of one event from scan_photos.py --json output.

    Uploading a chosen trip rather than a whole folder is the point of the
    pairing: the scan proposes albums, the user picks one, and only that
    event's files move.
    """
    with open(scan_path) as fh:
        data = json.load(fh)
    events = data.get("events") or []
    if not events:
        raise SystemExit(f"error: no events in {scan_path}")
    if not 1 <= event <= len(events):
        raise SystemExit(
            f"error: --event {event} is out of range; {scan_path} has "
            f"{len(events)} (numbered 1-{len(events)}, as the scan printed them)"
        )
    return events[event - 1].get("files") or []


def filter_paths(paths: list[str]) -> tuple[list[tuple[str, int]], list[str]]:
    """Apply the same eligibility rules to an explicit list of files."""
    eligible: list[tuple[str, int]] = []
    skipped: list[str] = []
    for path in paths:
        name = os.path.basename(path)
        if not os.path.isfile(path):
            skipped.append(f"{name} (missing since the scan)")
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext not in ALLOWED:
            skipped.append(f"{name} (unsupported type {ext or 'none'})")
            continue
        size = os.path.getsize(path)
        if size == 0:
            skipped.append(f"{name} (empty)")
            continue
        if size > FILE_FLOOR:
            skipped.append(f"{name} ({human(size)} -- over the 50MB floor, may be rejected)")
        eligible.append((path, size))
    return eligible, skipped


def collect(folder: str, recursive: bool) -> tuple[list[tuple[str, int]], list[str]]:
    """Return (eligible files as (path, size), skipped descriptions)."""
    eligible: list[tuple[str, int]] = []
    skipped: list[str] = []

    walker = os.walk(folder) if recursive else [(folder, [], os.listdir(folder))]
    for root, dirs, names in walker:
        # Hidden directories hold caches and sync metadata, never the user's photos.
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in sorted(names):
            if name.startswith("."):
                continue  # .DS_Store and friends
            path = os.path.join(root, name)
            if not os.path.isfile(path):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in ALLOWED:
                skipped.append(f"{name} (unsupported type {ext or 'none'})")
                continue
            size = os.path.getsize(path)
            if size == 0:
                skipped.append(f"{name} (empty)")
                continue
            if size > FILE_FLOOR:
                # Not skipped: a paid plan may well accept it. Flagged so the
                # user is not surprised by a per-file error in the response.
                skipped.append(f"{name} ({human(size)} -- over the 50MB floor, may be rejected)")
            eligible.append((path, size))
    return eligible, skipped


def plan(files: list[tuple[str, int]]) -> list[list[str]]:
    """Group files into requests that respect both the count and byte ceiling."""
    batches: list[list[str]] = []
    current: list[str] = []
    current_bytes = 0
    for path, size in files:
        too_many = len(current) >= MAX_FILES
        too_big = current and current_bytes + size > MAX_BYTES
        if too_many or too_big:
            batches.append(current)
            current, current_bytes = [], 0
        current.append(path)
        current_bytes += size
    if current:
        batches.append(current)
    return batches


def post(endpoint: str, batch: list[str], timeout: int) -> tuple[int, dict | None, str]:
    """POST one batch. Returns (http_status, parsed_body_or_None, raw)."""
    cmd = ["curl", "-sS", "--max-time", str(timeout), "-w", "\n%{http_code}", "-X", "POST"]
    for path in batch:
        cmd += ["-F", f"files=@{path}"]
    cmd.append(endpoint)

    done = subprocess.run(cmd, capture_output=True, text=True)
    if done.returncode != 0:
        return 0, None, (done.stderr or "").strip() or f"curl exit {done.returncode}"

    raw = done.stdout.rsplit("\n", 1)
    body, status = (raw[0], raw[1]) if len(raw) == 2 else ("", "0")
    try:
        return int(status), json.loads(body) if body.strip() else None, body
    except (ValueError, json.JSONDecodeError):
        return int(status) if status.isdigit() else 0, None, body


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--folder", help="Directory of photos to upload")
    ap.add_argument("--files-from", metavar="SCAN_JSON",
                    help="scan_photos.py --json output; upload one event from it")
    ap.add_argument("--event", type=int, metavar="N",
                    help="Which event from --files-from, as numbered in the scan")
    ap.add_argument("--endpoint", help="upload_endpoint.url from request_photos")
    ap.add_argument("--dry-run", action="store_true", help="Survey and plan only")
    ap.add_argument("--no-recursive", action="store_true", help="Top level only")
    ap.add_argument("--timeout", type=int, default=600, help="Per-request seconds")
    args = ap.parse_args()

    if not args.folder and not args.files_from:
        print("error: give --folder, or --files-from with --event", file=sys.stderr)
        return 2
    if args.files_from and not args.event:
        print("error: --files-from needs --event N (the number the scan printed)",
              file=sys.stderr)
        return 2
    if args.folder and not os.path.isdir(args.folder):
        print(f"error: not a directory: {args.folder}", file=sys.stderr)
        return 2
    if not args.dry_run and not args.endpoint:
        print("error: --endpoint is required unless --dry-run", file=sys.stderr)
        return 2

    if args.files_from:
        source = f"{args.files_from} event {args.event}"
        files, skipped = filter_paths(from_scan(args.files_from, args.event))
    else:
        source = args.folder
        files, skipped = collect(args.folder, not args.no_recursive)
    total_bytes = sum(size for _, size in files)
    batches = plan(files)

    print(f"source    : {source}")
    print(f"eligible  : {len(files)} files, {human(total_bytes)}")
    print(f"batches   : {len(batches)} request(s) of up to {MAX_FILES} files / {human(MAX_BYTES)}")
    if skipped:
        print(f"skipped   : {len(skipped)}")
        for note in skipped[:15]:
            print(f"  - {note}")
        if len(skipped) > 15:
            print(f"  ... and {len(skipped) - 15} more")

    if not files:
        print("\nNothing to upload.")
        return 1

    if len(batches) > REQUEST_BUDGET:
        print(
            f"\nWARNING: {len(batches)} requests exceeds the {REQUEST_BUDGET}/hour limit.\n"
            f"         The first {REQUEST_BUDGET} will land; the rest will be throttled (429).\n"
            f"         Upload roughly {REQUEST_BUDGET * MAX_FILES} photos now and the remainder "
            f"in an hour, or split the folder."
        )

    if args.dry_run:
        print("\nDry run -- nothing uploaded.")
        return 0

    uploaded = failed = server_skipped = 0
    errors: list[dict] = []

    for i, batch in enumerate(batches, 1):
        batch_bytes = sum(os.path.getsize(p) for p in batch)
        print(f"\n[{i}/{len(batches)}] posting {len(batch)} files ({human(batch_bytes)})...", flush=True)

        status, body, raw = post(args.endpoint, batch, args.timeout)

        if status == 201 and body is not None:
            got = body.get("uploaded", 0)
            uploaded += got
            server_skipped += body.get("skipped", 0)
            batch_errors = body.get("errors") or []
            errors.extend(batch_errors)
            print(f"        ok: {got} uploaded, {body.get('skipped', 0)} skipped"
                  + (f", {len(batch_errors)} errored" if batch_errors else ""))
            continue

        failed += len(batch)
        if status == 429:
            print("        THROTTLED (429) -- the 20/hour budget is spent. Stopping.")
            break
        if status == 410:
            print("        TICKET EXPIRED (410) -- mint a new one with request_photos. Stopping.")
            break
        if status == 404:
            print("        BAD TICKET (404) -- the endpoint URL is wrong or revoked. Stopping.")
            break
        if status == 403:
            detail = (body or {}).get("error", "forbidden")
            print(f"        REFUSED (403) {detail}: limit={(body or {}).get('limit')} "
                  f"current={(body or {}).get('current')}. Stopping.")
            break
        if status == 0:
            print(f"        NETWORK FAILURE: {raw[:300]}")
            print("        If this is a proxy/CONNECT denial, the sandbox egress is blocking "
                  "sustain-app.com -- see SKILL.md. Stopping.")
            break
        print(f"        HTTP {status}: {raw[:300]}. Stopping.")
        break

    print(f"\n{'-' * 52}")
    print(f"uploaded      : {uploaded}")
    print(f"server-skipped: {server_skipped}  (already present / duplicate)")
    if errors:
        print(f"file errors   : {len(errors)}")
        for e in errors[:10]:
            print(f"  - {e.get('file')}: {e.get('error')}")
    if failed:
        print(f"not attempted : {failed} (run stopped early -- see above)")
        print("\nNothing further to do until the cause above is resolved. Do NOT "
              "re-run blindly; re-mint the ticket or wait out the throttle as directed.")
        return 1
    print("\nNext: album_status to confirm the count, then quick_place (free) "
          "or build_album (spends credits, confirm first).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
