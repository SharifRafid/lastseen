# lastseen

A private location history for your Apple devices.

![lastseen showing a day of locations on a map, with devices and a timeline](https://raw.githubusercontent.com/sharifrafid/lastseen/main/docs/screenshot.webp)

> Works with Apple's iCloud Find My. Not affiliated with, endorsed by, or sponsored by Apple Inc.
> "Find My" and "iCloud" are trademarks of Apple Inc.

## What it does

Find My only shows where a device is *right now*. lastseen asks iCloud where your
devices are every few minutes and keeps every answer, so when something goes
missing you can see where it was last seen, and where it has been.

- Records your own devices and your Family Sharing members' devices.
- Shows them on a map with a timeline of where each device stayed, a time slider,
  and GPX/CSV export.
- Runs on your own computer or server. Your history never leaves it.
- Setup happens in your browser: no config files and no terminal sign-in.

## Quick start

**macOS or Linux.** Open Terminal and paste:

```sh
curl -LsSf https://raw.githubusercontent.com/sharifrafid/lastseen/main/install.sh | sh
```

**Windows.** Open PowerShell and paste:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/sharifrafid/lastseen/main/install.ps1 | iex"
```

The installer sets up [uv](https://docs.astral.sh/uv/) (a small Python tool
manager) if needed, installs lastseen for your user account (no admin rights),
and starts it. Your browser opens at <http://127.0.0.1:8765>. Follow the setup there:

1. **Welcome.** Optionally choose a password for the lastseen page.
2. **Sign in to Apple.** Enter your Apple Account email and password. They go
   only to Apple.
3. **Verify.** Type the 6-digit code that appears on your iPhone, iPad or Mac
   (or use your security key).
4. **How often.** Pick how often to check (every 15 minutes is recommended).
   lastseen takes the first snapshot immediately, so you can see which
   devices it can locate.
5. **Keep it running.** Leave "Start automatically" on so lastseen keeps
   recording after a restart.

To open lastseen later, run `lastseen` or go to <http://127.0.0.1:8765>.

**Manual install.** If you already use uv or pipx:

```sh
uv tool install https://github.com/sharifrafid/lastseen/archive/refs/heads/main.zip
lastseen
```

```sh
pipx install https://github.com/sharifrafid/lastseen/archive/refs/heads/main.zip
lastseen
```

Python 3.10 or newer is required (uv downloads it for you).

## Server / Docker

To run lastseen on a home server, NAS or VPS:

```sh
git clone https://github.com/sharifrafid/lastseen && cd lastseen
docker compose up -d
docker compose logs lastseen
```

The log prints a link like `http://localhost:8765/?setup=...`. Open it (replace
`localhost` with the server's address), choose a password and finish setup.
lastseen always requires a password when it is reachable from other machines,
and the setup link proves you are the one setting it up. The compose file's
`restart: unless-stopped` keeps lastseen running (with plain `docker run`, add
`--restart unless-stopped`), and your data lives in the `lastseen-data` volume.

If you bind-mount a folder instead of using the named volume, make it writable by
user id 1000 (`sudo chown 1000 ./data`).

**Use HTTPS or a VPN.** Your Apple password is sent through the page during
setup, so don't use plain `http://` over the internet. You can either:

- Put it behind a reverse proxy. With [Caddy](https://caddyserver.com), a
  `Caddyfile` like this gets you a certificate automatically:

  ```
  lastseen.example.com {
      reverse_proxy 127.0.0.1:8765
  }
  ```

  Also change the port line in `docker-compose.yml` to `"127.0.0.1:8765:8765"`
  so the only way in is through the proxy.
- Or reach the server over a private network such as
  [Tailscale](https://tailscale.com) or WireGuard.

**Without Docker** on a headless Linux box:

- `lastseen --host 0.0.0.0` listens on the network and prints the setup link.
- Or keep lastseen local-only and forward the port over SSH from your own computer:
  `ssh -L 8765:localhost:8765 your-server`, run `lastseen` there, and open
  <http://127.0.0.1:8765> on your computer. If other people have accounts on the
  server, set an access password in Settings: anyone logged in to the server can
  open `127.0.0.1:8765` too.
- `lastseen service install --host 0.0.0.0` keeps it running in the background
  (as a systemd user service).

## Limitations

- **Family devices** only have a location when that family member shares their
  location with you (Settings > [their name] > Family Sharing > Location
  Sharing) and the device is online. Otherwise Apple returns no location, and
  lastseen shows "Not sharing location" or "Offline".
- **No AirTags, items or People-tab friends.** iCloud on the web doesn't provide them.
- **Advanced Data Protection:** turn on *Settings > [your name] > iCloud > Access
  iCloud Data on the Web*, or Apple blocks Find My on the web.
- **Security keys:** if your Apple Account uses hardware security keys, the key
  must be plugged into the machine running lastseen. This isn't possible
  in Docker.
- **Apple signs you out** about every two months. lastseen then shows a
  "Sign in again" banner. It never retries in a loop, so your account isn't locked
  and you don't get a flood of codes.
- **Battery:** each check asks your devices to locate themselves, which uses a
  little battery. Checking less often uses less.

## Privacy & security

- Your history stays in a folder on the machine running lastseen. There is no
  telemetry and no account with us.
- Your Apple password is sent only to Apple. It is kept in your system keychain
  (macOS Keychain, Windows Credential Manager, or the Linux Secret Service). If
  no keychain is available, as in Docker or on a headless server, it is kept in
  `credentials.json` in the data folder, readable only by your user.
- By default lastseen listens only on this computer (`127.0.0.1`). It refuses to
  run on the network without a password, and it rejects requests from other websites.
- The page loads the map library ([Leaflet](https://leafletjs.com)) from
  cdnjs and map tiles from OpenStreetMap. Your locations aren't sent to either.

## Where your data lives

| System  | Folder |
|---------|--------|
| macOS   | `~/Library/Application Support/lastseen` |
| Windows | `%LOCALAPPDATA%\lastseen` |
| Linux   | `~/.local/share/lastseen` (or `$XDG_DATA_HOME/lastseen`) |
| Docker  | the `lastseen-data` volume (`/data` in the container) |

Set `LASTSEEN_DATA_DIR` or pass `--data-dir` to use another folder. The data folder
holds `history.db` (your history), `config.json` (settings), `session/`
(the Apple sign-in) and `lastseen.log`. To back up, stop lastseen and copy the folder.

## Configuration

Most settings live in the page (gear icon): how often to check, how long to keep
history, start automatically, the access password, and your Apple Account.

Environment variables:

| Variable | Default | What it does |
|----------|---------|--------------|
| `LASTSEEN_DATA_DIR` | see above | Where history and settings are stored |
| `LASTSEEN_HOST` | `127.0.0.1` | Address to listen on; `0.0.0.0` makes it reachable from your network (a password is then required) |
| `LASTSEEN_PORT` | `8765` | Port for the page |
| `LASTSEEN_ALLOWED_HOSTS` | (none) | Extra host names, comma-separated, accepted when lastseen listens on `127.0.0.1` behind a reverse proxy, e.g. `lastseen.example.com` (same as `--allowed-hosts`) |
| `LASTSEEN_CREDENTIAL_STORE` | auto | `file` to keep the Apple password in the data folder instead of the system keychain |
| `LASTSEEN_NO_BROWSER` | (unset) | `1` stops lastseen from opening a browser at start |
| `LASTSEEN_IN_DOCKER` | (unset) | Set by the Docker image. It turns off autostart and security keys |

Requests that arrive through one of the `LASTSEEN_ALLOWED_HOSTS` names are treated
like requests from the network: lastseen asks for a password, and the first one
needs the setup link from the log. "Start automatically" remembers these names.

Commands:

```text
lastseen                      start lastseen and open it in your browser
lastseen --host 0.0.0.0       listen on your network (requires a password)
lastseen --port 8766          use another port
lastseen status               show the data folder, account and recent checks
lastseen service install      start automatically at login (same as the switch in Settings)
lastseen service uninstall    stop starting automatically
lastseen stop                 stop lastseen (including the background service, until next login)
lastseen reset-password       remove the page's access password and print a new setup link
lastseen --version
```

Every command also accepts `--data-dir`, `--host`, `--port`, `--allowed-hosts`,
`--no-browser` and `-v` (more detailed logs).

## Updating, uninstalling, deleting data

- **Update:** run the installer again. It stops the running lastseen, installs
  the new version and starts it again. By hand: `lastseen stop`,
  `uv tool upgrade lastseen`, then `lastseen` (or `lastseen service install` if
  it starts automatically). With Docker: `git pull && docker compose up -d --build`.
- **Uninstall:** `lastseen service uninstall`, `lastseen stop`, then
  `uv tool uninstall lastseen` (or `pipx uninstall lastseen`). With Docker:
  `docker compose down`.
- **Delete your data:** use Settings > Delete all history, or Settings > Sign out
  & forget password (this keeps history). To remove everything, delete the data
  folder after uninstalling. With Docker: `docker compose down -v`.

## Troubleshooting

- **"Port 8765 is used by another app".** Start lastseen with
  `lastseen --port 8766`, or set `LASTSEEN_PORT`.
- **"Can't use data folder".** lastseen can't write to its data folder. With a
  Docker bind mount, make the folder writable by user id 1000
  (`sudo chown 1000 ./data`), or pick another folder with `--data-dir`.
- **"Sign in again" banner.** Apple ended the session, which is normal about every
  two months. Click *Sign in again* and enter the new code. Recording resumes right away.
- **A family member's devices have no location.** They need to share their
  location with you in Family Sharing, and the device must be on and online. See
  [Limitations](https://github.com/sharifrafid/lastseen#limitations).
- **Forgot the page password.** Run `lastseen reset-password` on the machine
  running lastseen (it can keep running). In Docker, run
  `docker compose exec lastseen lastseen reset-password`. Then reload the page,
  or open the setup link it prints if you use lastseen from another device.
- **Logs** are in `lastseen.log` in the data folder (`lastseen status` shows where
  that is). Start with `-v` for more detail.
- **Linux: lastseen stops when you log out.** Run `loginctl enable-linger $USER`
  once so your user services keep running.
- **Windows:** "Start automatically" adds a hidden launcher to your Startup
  folder and starts lastseen in the background right away.

## Development

```sh
git clone https://github.com/sharifrafid/lastseen && cd lastseen
uv sync
uv run pytest
uv run lastseen --data-dir ./data -v
```

The tests use fake iCloud objects and never contact Apple.

```text
src/lastseen/
  cli.py          command line, startup, single-instance lock
  config.py       data folder, settings, password storage, secrets
  icloud.py       pyicloud wrapper, sign-in flow, error messages
  collector.py    background checks and scheduling
  db.py           SQLite schema and queries
  web.py          HTTP server, JSON API, security checks, exports
  service.py      autostart (launchd, systemd --user, Windows Startup folder)
  static/index.html   the whole web UI (vanilla JS, no build step)
```

The only runtime dependency is [pyicloud](https://github.com/timlaing/pyicloud).
Everything else is the Python standard library.

## License

[MIT](https://github.com/sharifrafid/lastseen/blob/main/LICENSE) © 2026 Sharif Rafid
