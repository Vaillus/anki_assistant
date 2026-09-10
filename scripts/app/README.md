# scripts/app/

Desktop launcher for macOS: a real `.app` bundle that starts Anki, starts the server,
and renders the UI in its own window — no browser tab, its own Dock icon.

## Install

```bash
scripts/app/install.sh          # builds ~/Applications/Anki Assistant.app
```

Needs the Xcode command line tools (`xcode-select --install`).

Open the app once from Finder, then right-click → Options → Keep in Dock.

## Usage

Double-click the app, or from a terminal:

```bash
scripts/app/launch.sh               # start everything, open the window
scripts/app/launch.sh serve         # servers only, no window (what the app calls)
scripts/app/launch.sh stop          # stop the web server
scripts/app/launch.sh status        # check what's running
```

## Files

| File | What it does |
|---|---|
| `launch.sh` | Starts Anki (if not running) and the web server (if not running), then opens the window. Idempotent. |
| `install.sh` | Compiles `main.swift`, rasterises the icon, assembles and codesigns the `.app` bundle. |
| `main.swift` | A `WKWebView` in a Cocoa window — the native host. Keeps ⌘C/⌘V alive, hands `obsidian://` links to the system, saves window position across quits. |
| `make_icon.py` | Rasterises `star.svg` into an `.icns` in `img/` — pure Python, no image library. |
| `img/` | Generated icons (gitignored). |

## How it fits together

The bundle stores this folder's path in `Info.plist` and calls `launch.sh` from it, so
edits to the shell script take effect without rebuilding. Rebuild only after touching
`main.swift`, the icon, or the install location.

The server runs with `ANKI_WEB_RELOAD=0` (no file watcher) and logs to
`~/Library/Logs/anki-assistant.log`.
