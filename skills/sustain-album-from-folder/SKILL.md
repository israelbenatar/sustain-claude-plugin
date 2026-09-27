---
name: sustain-album-from-folder
description: Work with a folder of photos on this machine and Sustain's remote MCP server — scan the folder to suggest which albums are worth making (from EXIF dates and GPS, never by reading the images), then create the chosen album, upload its files directly, and lay them out. Use when the user asks to make/build a Sustain album from a local folder or set of photos on disk, says "upload these to Sustain", or asks what albums are in a folder / to find trips or events in their photos.
---

# Sustain album from a local folder

Turn a directory of photos into a laid-out Sustain album without the user
touching a browser. The agent does the uploading itself.

Requires the **Sustain remote MCP connector** to be connected (tools named
`whoami`, `create_album`, `request_photos`, `album_status`, `quick_place`,
`build_album`). If those tools are absent, stop and tell the user to connect
the Sustain connector — do not try to drive the website instead.

## Why this skill exists

The upload endpoint is public and ticket-authorised, so an agent *can* POST the
files itself. But it is rate limited at **20 requests per hour**. An agent that
improvises one-file-per-request succeeds for 20 photos and then silently starves
for an hour. Batching correctly is the whole point of this skill; the numbers in
"Upload" below are not suggestions.

## Two ways in

**They already know what they want.** Skip to *Flow* below and upload the folder.

**They have a pile of photos and no idea what is in it.** Start with *Suggest
albums*, which reads metadata only, proposes events, and hands the chosen one
to the same upload path.

## What machine this is running on

Check before offering a path, because two of them are macOS-only and the user may
be on anything.

| | macOS | Linux | Windows |
|---|---|---|---|
| Scan a folder (EXIF, stdlib) | yes | yes | yes |
| Upload (stdlib, nothing to install) | yes | yes | yes |
| `mdls` date recovery for unparseable EXIF | yes | no | no |
| `--photos-library` (Apple Photos) | yes | no | no |

Off macOS, do not offer `--photos-library` at all — ask for a folder. The scan
still works everywhere; it just reports a file with no readable EXIF date as
undated instead of recovering it from Spotlight.

Both scripts need only `python3`. If the upload fails for any other reason, do
not work around it by reading file bytes into the conversation — give the user
`upload_page_url` from `request_photos` instead.

## What the user has to allow

Check this before promising anything, and never talk someone into a permission
they do not need.

| Doing | Needs | Ask |
|---|---|---|
| Scan a **folder** | nothing special | macOS may ask the terminal or Claude app once for Desktop/Documents/Downloads. One click. |
| Scan the **Photos library** | **Full Disk Access** for the app running the command | Broad. It covers Mail, Messages and everything else, not just photos. |
| **Upload** | `sustain-app.com` on the sandbox allowlist | See the network section at the end. |

Default to the folder. Only raise the Photos library if the user asks for it or
says their photos live in Photos, and when you do, say plainly what the
permission costs — then offer the export route as the alternative, because it
needs no permission at all.

To grant it: **System Settings → Privacy & Security → Full Disk Access**, switch
on the app running the command (Terminal, iTerm, or the Claude app), then **quit
and reopen that app**. The grant only takes effect on a fresh launch, which is
the step people miss.

## Suggest albums from the macOS Photos library

```bash
python3 "$SC" --photos-library --json /tmp/sustain-scan.json
```

Reads Photos' own catalogue, read-only and unlocked (`immutable=1`) — nothing is
written, and Photos can stay open. Photos has already extracted every capture
date and location, so this is instant on a library of tens of thousands, needs
no EXIF parsing, and covers HEIC. The bin, the Hidden album and videos are
excluded.

**Expect most originals not to be on this Mac.** With iCloud's *Optimise
Storage* the files live in the cloud while their metadata stays local — a
library can easily be 90% cloud-only. That does not weaken the suggestions:
every date and place is known, so the albums it proposes are complete and
correct. It only affects uploading, and the scan says how many photos of each
album would need fetching, as `N to download`.

To upload one of those, the originals have to come down first. In Photos, select
the album's date range, then **File → Export → Export Unmodified Original** into
a folder, and scan *that* folder with `--folder`. Exporting is what pulls them
out of iCloud. This skill deliberately never triggers a download itself: on a
large library that is a silent multi-gigabyte transfer, and it is not the
agent's call to start one.

At library scale raise `--min-photos` (try 100–200) or the whole decade arrives
at once.

## Suggest albums from a folder

```bash
SC=~/.claude/skills/sustain-album-from-folder/scan_photos.py   # or ${CLAUDE_PLUGIN_ROOT}/skills/sustain-album-from-folder/scan_photos.py
python3 "$SC" --folder "<folder>" --json /tmp/sustain-scan.json
```

This **never opens an image**. It reads EXIF capture times and GPS, clusters
them into bursts, then joins bursts within three days of each other into trips,
and prints a numbered table. A 1,800-file folder takes about a second.

Present the table as it is. Then help them choose, using the signals it gives:

- **`N stops`** means one journey that moved around — usually the best album in
  the folder, and the stop list underneath is roughly its chapters.
- **`no GPS` with hundreds of photos in two days** is often a bulk export or a
  download, not an event. Say so before they make an album of it.
- **`weak dates`** counts files with no capture time, grouped by when they were
  *copied*. Those groupings are guesses; treat them with suspicion.
- **`screenshot-ish`** counts files named like screenshots. A cluster that is
  mostly those is probably not worth an album.

Tunables, when the grouping is wrong: `--gap-hours` (default 20) splits on
quiet periods, `--place-km` (120) splits on travel, `--trip-gap-days` (3) joins
bursts back into trips, `--min-photos` (12) sets what is worth proposing. If a
holiday arrives split in two, raise `--trip-gap-days`. If one album swallows a
whole month, lower it.

Then upload just that event — same uploader, same ceilings:

```bash
python3 "$UP" --endpoint "<upload_endpoint.url>" --files-from /tmp/sustain-scan.json --event <n>
```

Create the album first (step 3 below) so you have somewhere to put it, and name
it from what the scan showed — the dates, the folder name, and the user's own
words for the trip. **Do not invent a place name from the coordinates**: they
are never geocoded, deliberately, so if the location matters, ask.

## Flow

### 1. Confirm the account

Call `whoami`. Report the email and credit balance. If the email is not the one
the user expects, stop — they are connected as someone else.

### 2. Survey the folder BEFORE creating anything

Run the uploader in dry-run to see what is actually there. `upload_folder.py`
sits next to this SKILL.md; when installed as a plugin that is
`${CLAUDE_PLUGIN_ROOT}/skills/sustain-album-from-folder/`, and as a personal
skill `~/.claude/skills/sustain-album-from-folder/`. Resolve it once and reuse
the path:

```bash
UP=~/.claude/skills/sustain-album-from-folder/upload_folder.py   # or ${CLAUDE_PLUGIN_ROOT}/skills/sustain-album-from-folder/upload_folder.py
python3 "$UP" --folder "<folder>" --dry-run
```

It prints the eligible file count, total bytes, the batch plan, and anything it
would skip. Show the user that summary and get a yes before uploading. If the
plan needs more than 20 requests the script says so — relay it rather than
starting an upload that will stall.

### 3. Create the album

`create_album(name)`. Derive the name from the folder name unless the user gave
one; ask if the folder name is meaningless (`DCIM`, `Downloads`, `export`).

### 4. Get an upload ticket

`request_photos(album_id)`. Use the **`upload_endpoint.url`** from the response —
that is the machine-usable one. The `upload_page_url` in the same response is
the human fallback; only offer it if uploading yourself fails.

### 5. Upload

```bash
python3 "$UP" --endpoint "<upload_endpoint.url>" --folder "<folder>"
```

**Never read image bytes into the conversation** and never pass file contents
through a tool argument. The script streams from disk via curl; that is the only
correct path. Reading a 400-photo folder into context would be both useless and
enormously expensive.

The script prints a per-batch line and a final total. Relay the totals —
uploaded, skipped, errors — honestly. Skipped files are usually duplicates the
server already had, which is normal and not a failure.

### 6. Confirm the server agrees

`album_status(album_id)` and check `photo_count` matches what the script
uploaded. The script reports what the API accepted; this confirms it landed.

### 7. Lay it out

Two options, and the difference is money:

- `quick_place(album_id)` — **free**, immediate, no AI. Do this first.
- `build_album(album_id)` — **spends the user's credits**, proportional to photo
  count. Requires explicit confirmation, every time. Check the balance from step
  1 first; if it is short the call is refused with the numbers, which you relay.

Default to `quick_place` unless the user asked for the AI pass. Then poll
`album_status(album_id, task_id)` until the job completes and give them
`/edit/<album_id>`.

## Server constraints (verified against `origin/main`, 2026-09-19)

Do not rediscover these by trial and error.

| Constraint | Value | Source |
|---|---|---|
| Requests | **20/hour** | `qr_upload` throttle, `settings.py` |
| Files per request | **100** | Django `DATA_UPLOAD_MAX_NUMBER_FILES` default |
| Body per request | **200MB** | nginx `client_max_body_size` |
| Per file | 50MB floor; paid plans raise it | `_max_upload_bytes` |
| Extensions | `.jpg .jpeg .png .heic .tiff .tif .webp` | `ALLOWED_EXTENSIONS` |

Note `.heif` and `.gif` are **not** accepted, though `.heic` is. Files are also
magic-byte validated, so a renamed non-image is rejected server-side.

The endpoint takes **no `Authorization` header and no cookies** — the ticket in
the URL is the authorisation. Sending credentials is not merely unnecessary, it
is how you leak them to a path that does not want them.

## Responses to expect

`201` with `{uploaded, skipped, photo_ids, skipped_details, errors}` is success —
note that a 201 can still carry per-file `errors`, so read the body, never just
the status.

| Status | Meaning | Do |
|---|---|---|
| `403` `photo_limit_exceeded` / `storage_limit_exceeded` | plan ceiling hit | Stop. Relay the limit and current usage. Offer `start_checkout`. |
| `410` | ticket expired | Call `request_photos` again for a fresh one. |
| `429` | throttled (20/hour) | Stop. Report how many landed and when they can resume. Do not spin. |
| `404` | bad ticket | Re-mint. Do not retry the same URL. |

## If the upload is blocked by the network

A `403 on CONNECT` or a proxy policy denial is **not** a Sustain problem and not
a file-permission problem — it is the agent's own sandbox egress refusing the
host. Say so precisely rather than claiming you lack file access, and either ask
the user to allowlist `sustain-app.com` (in Claude Code:
`sandbox.network.allowedDomains` in `~/.claude/settings.json`, then restart) or
fall back to handing them `upload_page_url`.
