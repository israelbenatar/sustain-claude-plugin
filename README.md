# Sustain Skills

Build a [Sustain](https://www.sustain-app.com) photo album from a folder on your
own machine, without opening a browser. The agent reads the folder, decides which
albums are worth making, creates one, uploads the files itself, and lays out the
pages.

## What it contains

**One skill, `sustain-album-from-folder`**, and a declaration of Sustain's remote
MCP server at `https://mcp.sustain-app.com/mcp`. Installing the plugin gives you
both: the skill teaches the agent the right order of operations, and the connector
is what it operates through.

The skill ships two Python scripts, standard library only, no dependencies to
install:

| Script | What it does |
| --- | --- |
| `scan_photos.py` | Reads EXIF dates and GPS from the files on disk and clusters them into candidate trips or events. **It never reads image content** — no pixels, no AI, no network. |
| `upload_folder.py` | Uploads the chosen files to Sustain over HTTPS, in batches, using a short-lived upload ticket the agent mints through the connector. |

## What it runs on

Anywhere `python3` runs — macOS, Linux and Windows. The EXIF reader is standard
library, so the scan behaves identically on all three.

**Nothing to install.** Both scripts are standard library only, with no external
command required — the upload builds and streams its own multipart body, so a
400-photo batch never lands in memory (measured: 33 MB of process memory while
uploading a 120 MB file).

Three extras are macOS-only and simply do not apply elsewhere: reading Apple Photos
libraries (`--photos-library`), a Spotlight (`mdls`) lookup that recovers dates for
files whose EXIF the stdlib reader cannot parse, and `--describe` below. On Linux and
Windows the scan uses EXIF alone, and a file with no readable date is reported as
undated rather than guessed at.

## Finding photos without uploading them

The scan reads dates and GPS, so it can be asked for a subset rather than a whole
folder:

```bash
scan_photos.py --folder ~/Pictures --since 2024-08 --until 2024-08
scan_photos.py --folder ~/Pictures --near 8.05,98.91 --radius-km 60
```

The agent does the translating — "last August" into dates, a place name into
coordinates — and the filters run against metadata. It never looks at a photo, which
is what keeps this instant on a library of tens of thousands. A photo with **no GPS
never matches `--near`**, so the scan prints what each filter removed rather than
leaving "nothing there" and "nothing matched" looking the same.

### Describing photos on the Mac, with nothing leaving it

`--describe` captions the matching photos using Apple's on-device model, so the agent
can answer *"the beach ones"* about files that were never uploaded:

```bash
scan_photos.py --folder ~/Pictures --near 13.75,100.50 --radius-km 40 --describe
```

**Requires macOS 27 on Apple Silicon, and one setup step.** Apple's model needs its
terms accepted once per machine, by a privileged user:

```bash
sudo fm license
```

Until that is done the scan says so in one line and carries on with dates and places.
It is skipped the same way on Linux and Windows. Nothing about the rest of the skill
depends on it.

It runs **only on photos the filters already narrowed to**, and refuses past
`--describe-limit` (120). Measured on this hardware at roughly **1.1 seconds a photo**
on both JPEG and HEIC — a minute for fifty, two and a half hours for a whole library,
which is why the limit is a refusal rather than a warning.

Nothing is uploaded, no key or account is involved, and it costs nothing. Captions
land in the `--json` output under `descriptions`, keyed by path.

## What it sends, and where

Everything goes to Sustain and nowhere else:

- **Photo files** are POSTed to `https://www.sustain-app.com/api/upload-link/<ticket>/`.
  The ticket is minted per album by the `request_photos` tool and is its own
  authorisation, so no account credential is involved in the upload.
- **Tool calls** (create an album, check progress, lay out pages) go to
  `https://mcp.sustain-app.com/mcp`.
- **Photo bytes never pass through the conversation.** A single full-resolution
  photo can exceed an entire context window, so the scripts upload from disk
  directly.

Nothing is sent to any third party, and the scan step makes no network requests at
all.

## What you need

A Sustain account, and the connector authorised once through OAuth — you land on a
Sustain page asking what the agent may do, tick the boxes, and you are connected.
There is no key to paste and no credential stored in this plugin.

Some operations spend the account's credits, the same as they do in the app. The
skill says which, tells you before it spends, and prefers the free layout path
when you have not asked for the AI pass.

Full documentation: <https://www.sustain-app.com/connect-your-agent>

## Revoking it

Settings → Connected apps on sustain-app.com. Revoking takes effect on the agent's
next call, because Sustain issues these tokens itself rather than handing out
something it cannot withdraw.

## Licence

MIT.

## Where this comes from

This repository is the published copy of the plugin. The source of truth is the
Sustain application repository, which builds the same files into the self-hosted
marketplace archive -- so a change made only here would be overwritten by the next
build there. Report problems as issues; send changes to the Sustain repository.
