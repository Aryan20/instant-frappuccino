# Instant Frappuccino

A native desktop app for **Frappe Manager** (`fm`) and **Frappe Deployer** (`fmd`) — Frappe
Cloud's workflow, on your own machine and on your servers. The goal: **never open Docker
Desktop or a terminal** for day-to-day Frappe work. Instant Frappuccino drives the Docker engine
headlessly, keeps the global proxy and database up, restarts exactly the part of a bench you
need, and fixes the usual "site won't load" problems in one click. Staging and production
benches are managed from the same window over SSH.

Built with **Python + Qt (PySide6)**: a native app for macOS and Linux from one codebase,
with no web view. It uses one consistent, Frappe-UI-like design in light and dark mode.

## Features

| | |
|---|---|
| **System** | Replaces the Docker Desktop window. Engine status with Start, Stop and Restart, run through Docker Desktop's own CLI (`docker desktop …`), OrbStack, Colima, or systemd on Linux. Also: the global MariaDB and nginx proxy with start/stop/restart/logs, every FM container grouped by bench with live CPU and memory, per-container restart/stop/logs, disk usage, and safe cleanup (dangling images and build cache only, never volumes). |
| **Self-healing** | Banners with one-click fixes: *Start Docker* when the engine is down, *Start services* when the proxy is down, *Free ports* when another container (e.g. a Lando proxy) holds 80/443. Each site has **Repair**: engine → services → proxy reload → recreate containers. |
| **Menu bar / tray** | Start or stop everything, each site (open/start/stop/restart/repair), restart proxy/DB/engine. Closing the window keeps Instant Frappuccino running. |
| **Auto-start** | Configurable: off, when the app opens, or **at login** (LaunchAgent on macOS, XDG autostart on Linux; it starts in the background in the tray). It brings up the engine, global services and the sites you pick. |
| **Sites** | Every bench under `~/frappe/sites` with live Docker status, type (FM / Deployer), env, Frappe version, app count. Start, stop, restart, open site/desk, delete (type-to-confirm, optional DB drop). |
| **New Site wizard** | Name → *Frappe Manager* (dev bench) or *Frappe Deployer* (immutable releases) → Frappe version → apps → review the exact commands → create. |
| **Import fmd config** | Already have a Frappe Deployer `site.toml`? Choose it in the New Site wizard (or on the Sites page, or drop it onto the wizard) for instant setup: name, Frappe version, apps, runtimes and release options are filled in and you go straight to Review, which shows the exact config that will be written. Everything the wizard doesn't edit is kept verbatim: per-app hooks, `[ship]`, `[remote_worker]`, `[fc]`, `[switch.site_config]` and unknown keys. Credentials in the file are masked in all previews and logs, and the stored copy is mode 600. Existing Deployer sites can redeploy from a file via **Releases → Import fmd config…**. |
| **Apps** | Install from the **Marketplace** (360+ apps), **My Apps**, or **any git URL** (GitHub/GitLab, public or private, monorepo subdirs). Branches are looked up from GitHub and matched to your Frappe version automatically. |
| **Site detail** | Config overview. **Site info & credentials** loads `fm info` on demand: Administrator, DB and root-DB passwords, plus Mailpit/Adminer URLs and basic-auth login, each with Show and Copy. Toggles for developer mode, admin tools and environment. Targeted restarts (web, workers, redis, nginx, all containers), migrate, backup and **build**: `bench build` for the whole bench or just the apps you pick, with production/force/clear-cache options and the exact command shown. You can also multi-select apps on the Apps tab and click *Build selected*. Installed apps with branch, commit and source, add/remove apps, a Containers tab, and live `fm logs -f` per service. |
| **Everyday tools** | **Log in as Admin**: one click opens the desk as Administrator via a one-time session from `bench browse`, no password typing; the session URL never touches the logs. **Clear cache**: site, website or both. **Terminal** and **Console** open your own terminal app on `fm shell` / `--bench-console`. **VS Code** runs `fm code`. **Run command…** runs any bench or shell command in the container, with templates and remembered history. **Maintenance mode** and **Pause scheduler** toggles show the live state from `site_config.json`. **Administrator password…** sets a new one. All of these are also in the site's More menu and the Sites right-click menu; Log in as Admin and Clear cache are in the tray as well. |
| **Backups** | Lists the site's backups (database plus public/private files) with size and time. **Back up now**, **Show in folder**, and **Restore…** (type the site name to confirm; files optional; migrate runs afterwards). The MariaDB root password comes from fm's secrets folder and is masked in logs. |
| **App workflow** | Multi-select apps: **Pull latest** (`git pull --ff-only`, then optional migrate and build of just those apps), **Run tests** (whole app or one module, enabling `allow_tests`), **Build selected**. |
| **Releases** (Deployer) | Release history with the active one marked, *Deploy now*, *Switch to selected* (rollback), cleanup. Any FM site can be converted with **Enable release deploys**. |
| **Activity** | Every job with step progress and full streamed output; cancel, re-run, copy log. Jobs on the same bench are serialized, different benches run in parallel. |
| **Servers (SSH)** | Manage benches on staging or production servers as well. Add a server (an SSH alias or `user@host`, port, FM home) in **Settings → Servers**, then pick it from the switcher at the top of the sidebar. Sites, site detail, global services, containers, logs, backups/restore, deploys and releases, Log in as Admin, Terminal and Console all work on the server. The server needs `fm` (plus `fmd` for Deployer sites) and `python3`. Login uses your SSH keys or ssh-agent with connection reuse; passwords are not supported. A server marked **Production** shows the exact commands and asks before every change. Your GitHub token is never sent to servers, and a server's Docker engine, VS Code and disk cleanup stay on the server. |
| **Settings** | Tool health (Docker, fm, fmd, uv, git) with one-click install/update of fm and fmd via `uv`, GitHub token, defaults, paths, light/dark theme. |

## Requirements

- **Docker** (Docker Desktop, OrbStack, Colima, or Docker Engine on Linux)
- **Frappe Manager** — `uv tool install frappe-manager` (or from the app's Settings)
- **Frappe Deployer** (optional) — `uv tool install frappe-deployer --python 3.13` (or from Settings)
- **Servers** (optional) — SSH key or ssh-agent login, and `fm` plus `python3` on the server
  (`fmd` too for Deployer sites)
- To run from source: [uv](https://docs.astral.sh/uv/) — it installs Python 3.13 and Qt for you

## Run from source

```bash
uv sync
```

```bash
uv run instant-frappuccino
```

## Build a native bundle

```bash
uv run python packaging/build.py
```

- **macOS** → `dist/Instant Frappuccino.app` and `dist/instant-frappuccino-<ver>-macos-<arch>.dmg`
- **Linux** → `dist/instant-frappuccino/` and `dist/instant-frappuccino-<ver>-linux-<arch>.tar.gz` (extract, run `./install.sh` to add it to your app menu)

PyInstaller doesn't cross-compile, so build on each OS. [`ci.yml`](.github/workflows/ci.yml)
tests on both and uploads both bundles. The macOS build targets Apple Silicon on macOS 13 or
later; the build fails if any bundled library needs a newer macOS.

The bundles are unsigned, so macOS blocks the first launch. On macOS 15 or 26, use
**System Settings → Privacy & Security → Open Anyway**; on macOS 13 or 14, right-click the app
and choose **Open**. Signing and notarizing with a Developer ID removes the prompt.

Linux needs Qt's xcb runtime libraries, which most desktops already have
(`sudo apt install libxcb-cursor0` on Debian/Ubuntu if the app won't start).

## Architecture

```
src/fmapp/
  core/            ← no Qt imports; unit-tested; shareable with a CLI or another UI
    models.py        AppRef (repo[:ref][#subdir] ↔ fm / fmd syntax), Bench, Release, Backup, SiteSpec
    probe.py         reads ~/frappe/sites/*/bench_config.toml, apps/.git, release_* dirs, backups
                     and `docker compose ls/ps`, with no scraping of fm's Rich tables. Stdlib-only,
                     Python 3.8+: runs in-process here and as `python3 -` on servers
    source.py        a host's benches and Docker state (local in-process, servers via SSH)
    hosts.py         this computer plus the SSH servers from Settings
    remote.py        wraps argv for a server: `ssh … host -- bash -lc '…'`, runs the probe
    benches.py       probe output → Bench models
    operations.py    intent → Job(steps) of fm/fmd argv; pure data, previewable, testable
    deployer.py      per-site fmd config: build, import and merge site.toml files
    engine.py        detects Docker Desktop / OrbStack / Colima / systemd / rootless and
                     builds their start/stop/restart commands, plus a readiness wait
    containers.py    FM containers (global and per bench), stats, port conflicts, disk usage
    marketplace.py   Frappe Cloud marketplace index, page details, repo and branch lookup
    info.py          parses `fm info` into rows, flagging secrets
    terminal.py      opens a command in the user's terminal app (.command file / Linux terminals)
    autostart.py     login item: LaunchAgent (macOS) / XDG autostart (Linux)
    catalog.py       "My Apps" library          settings.py   user preferences
    env.py           PATH/tool discovery for GUI launches on macOS and Linux
    paths.py         FM home, per-OS config/cache dirs, owner-only file writes
    textutil.py      ANSI cleanup, secret masking, app-name helpers
  ui/              ← PySide6
    app.py           single instance, --background mode, auto-start
    main_window.py   sidebar navigation and page stack
    context.py       shared stores (benches, tools, system, marketplace) and job submission
    jobs.py          QProcess job runner (streaming, masking, cancel, per-bench serialization)
    site_tools.py    Log in as Admin, terminal/console, run command, password, clear cache
    tray.py          menu-bar / tray quick actions
    theme.py         spacing scale, light/dark palettes, the stylesheet
    widgets.py       shared widgets and layout helpers
    pages/           sites, site_detail, system, marketplace, my_apps, activity, settings
    dialogs/         new_site, app_selector, build, logs, add apps / edit app / confirm
  diagnose.py      --diagnose / --diagnose-actions self-checks inside the packaged app
packaging/
  build.py         PyInstaller bundle, minimum-macOS check, DMG or Linux tarball
  dmg_art.py       installer window artwork and Finder layout (via dmgbuild)
```

**Portability.** Everything OS-specific lives in `core/env.py` (extra PATH entries for
Homebrew, `~/.local/bin`, Docker Desktop, OrbStack, snap…) and `core/paths.py` (config in
`~/Library/Application Support/instant-frappuccino` on macOS, `~/.config/instant-frappuccino` on Linux). The UI styles
everything through one stylesheet in `ui/theme.py`, re-applied when the OS switches between
light and dark.

**Which tool runs what.**

- *Frappe Manager sites:* `fm create --apps …` and `fm start/stop/restart/update/delete/logs`.
  Apps are added later with `bench get-app` + `install-app`, run through `fm shell`.
- *Frappe Deployer sites:* `fm create -e prod` (frappe only), then the app writes
  `<config>/deployer/<site>.toml` and runs `fmd deploy pull --config …`. Changing apps edits
  that config and deploys a new release. Rollback is `fmd release switch`.

**Servers.** Every job is the same fm/fmd argv as for this machine, run through
`ssh -o BatchMode=yes -o ControlMaster=auto … host -- bash -lc '<argv>'`, so tools resolve from
the server's own PATH. State is read by sending `core/probe.py` to the server's `python3`.
A Deployer site's config is kept locally per server and uploaded over stdin to
`~/.instant-frappuccino/deployer/<site>.toml` before `fmd deploy pull`. A restore reads the
MariaDB root password on the server, so the password never crosses SSH.

**Secrets.** The GitHub token lives in `settings.json` (mode 600) and reaches fm and fmd only
through the `GITHUB_TOKEN` env var. fmd configs reference it as `${GITHUB_TOKEN}`. When
`bench get-app` needs it inside a URL, the git remote is scrubbed right after cloning. Admin
passwords and tokens are masked in every preview and log.

**Marketplace caveat.** Frappe Cloud's public API only lists app names, so details are read
from each app's public page. Source repos aren't published at all: they come from a verified
map, from GitHub links on the page, or a `frappe/<name>` guess. The UI shows which one it
used and lets you edit it before installing.

## Develop

```bash
uv run pytest -q
```

```bash
uv run ruff check src tests packaging
```

## Troubleshooting

If a button seems to do nothing, run the packaged app from a terminal with a self-check. It
prints what the app finds (tools, PATH, sites) and what the actions do, with session ids redacted:

```bash
"/Applications/Instant Frappuccino.app/Contents/MacOS/Instant Frappuccino" --diagnose
```

Add `--diagnose-actions <site>` instead to really perform Log in as Admin and open a terminal
for that site.

If a server shows "Couldn't reach…", check that `ssh <destination>` works in a terminal without
a password prompt (the app uses `BatchMode`, so it can't answer one). A new server's host key
must be accepted once; the Sites page's **Connect in Terminal** button opens that session.

## Roadmap ideas

- Frappe Cloud import: fmd already supports `--fc-key/--fc-secret/--fc-site` to pull an FC
  site's apps and DB into a local release
- Ship mode: build locally, deploy to a remote server (`fmd deploy ship`)
- SSL / alias-domain management (`fm ssl`), ngrok tunnels
- Desktop notifications when long jobs finish
- AppImage / Flatpak for Linux; signed + notarized macOS build
