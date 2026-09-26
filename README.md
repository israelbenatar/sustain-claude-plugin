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
| `scan_photos.py` | Reads EXIF dates and GPS from the files on disk and clusters them into candidate trips or events. **It never reads image content** — no pixels, no AI, no network. Falls back to macOS `mdls` when a file carries no EXIF. |
| `upload_folder.py` | Uploads the chosen files to Sustain over HTTPS, in batches, using a short-lived upload ticket the agent mints through the connector. |

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
marketplace archive — so a change made only here would be overwritten by the next
build there. Report problems as issues; send changes to the Sustain repository.
