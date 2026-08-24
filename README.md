# ITAM — simple IT asset & subscription tracking

A small internal web app for tracking who has what and what it costs.
People come from **Entra ID**, keyed on **UPN**. Two kinds of cost:

| | What it is | Cost model |
|---|---|---|
| **Assets** | Laptops, monitors, phones, peripherals, perpetual software | one-off purchase cost |
| **Subscriptions** | SaaS licences (M365, GitHub, Slack, …) | cost per seat, per month |

Stack: FastAPI + SQLite + server-rendered HTML. No build step, no JavaScript
framework, no external services. One file for the database (`itam.db`).
Access is protected by **local username + password sign-in**.

## Deploy on Ubuntu

Three commands on a fresh server:

```bash
sudo apt-get update && sudo apt-get install -y git
```

```bash
git clone <your-repo-url> /opt/itam && cd /opt/itam
```

```bash
sudo ./setup.sh
```

`setup.sh` does the whole job: installs Docker if it is missing, asks for the
port, the admin username and password, the currency, whether you are behind
HTTPS, and optionally your Entra ID credentials. It writes `.env` (mode 600),
prepares the data directory, builds the image, verifies the container can
actually write to the database, starts everything, waits for the health check,
and prints the URL and the credentials.

Non-interactive, for a scripted rollout:

```bash
sudo ./setup.sh --yes --port 8080 --admin-user itadmin --admin-password 'a-long-passphrase'
```

Leave `--admin-password` off with `--yes` and it generates one and prints it.
Other flags: `--currency EUR`, `--https`, `--no-start`, `--reconfigure`, `--help`.

Re-running `setup.sh` later is safe — it offers to keep your existing settings
and just rebuild.

### Day-to-day

```bash
docker compose logs -f          # follow the logs
docker compose ps               # status and health
docker compose restart          # restart
docker compose down             # stop
docker compose up -d --build    # update after a git pull
./backup.sh                     # snapshot the database
```

On Ubuntu 22.04, and anywhere the Compose plugin is missing, substitute
`docker-compose` (with a hyphen) for `docker compose`. `setup.sh` detects which
one you have and tells you which to use.

The container runs with `restart: unless-stopped` and `setup.sh` enables the
Docker service, so the app comes back by itself after a reboot.

### Open the firewall

Only if you need to reach it from other machines, and only from where you
actually need:

```bash
sudo ufw allow from 10.0.0.0/8 to any port 8080 proto tcp
```

### Serve it over HTTPS

The app speaks plain HTTP; put a reverse proxy in front. Set
`ITAM_COOKIE_SECURE=1` in `.env` and `docker compose up -d` so session cookies
are HTTPS-only. This nginx config was verified against a running instance:

```nginx
server {
    listen 443 ssl;
    server_name itam.example.com;

    ssl_certificate     /etc/letsencrypt/live/itam.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/itam.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

`X-Forwarded-Proto` matters: without it the app builds redirects with the wrong
scheme after sign-in. Any proxy works as long as it sets these headers.

### Back up

`./backup.sh` writes a gzipped snapshot to `./backups/` and keeps the last 30.
It uses SQLite's own backup API, so it is safe to run while the app is live —
copying the file directly is not. Nightly at 02:30 (`crontab -e`):

```
30 2 * * * cd /opt/itam && ./backup.sh >> /var/log/itam-backup.log 2>&1
```

Restoring is a file copy:

```bash
docker compose down
gunzip -c backups/itam-YYYYMMDD-HHMMSS.db.gz > data/itam.db
docker compose up -d
```

Send `./backups/` somewhere off the box — the snapshot is no use if it dies with
the server. `apt-get install -y sqlite3` gives `backup.sh` the fastest path; it
falls back to the container's own Python if the host lacks it.

### If something goes wrong

| Symptom | Cause and fix |
|---|---|
| `unable to open database file` in the logs | The container runs as uid 10001 and cannot write to `./data`. Fix: `sudo chown -R 10001:10001 ./data && docker compose up -d` |
| Health check never turns healthy | `docker compose logs --tail=50` — the startup error is there |
| `port is already allocated` | Something else has that port. Change `ITAM_PORT` in `.env` and `docker compose up -d` |
| `permission denied` on the Docker socket | `sudo usermod -aG docker $USER`, then log out and back in |
| Forgot a password | Any other admin can reset it under **Accounts**. `sudo grep ITAM_ADMIN_PASSWORD .env` still shows the original bootstrap password if it was never changed. |
| Locked out of every admin account | Set a known hash directly, which keeps all your inventory data: `docker compose exec itam python -c "from app import auth; auth.set_password('admin','a-new-long-password')"`. If the account no longer exists, `docker compose exec itam python -c "from app import auth; auth.create_user('admin','a-new-long-password',is_admin=True)"`. |
| Sign-in says "too many attempts" | Deliberate: 8 failures locks that username for 15 minutes. `docker compose restart` clears it immediately. |

## Run it locally without Docker

For development on your own machine:

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
```

```bash
./run.sh
```

Serves on <http://127.0.0.1:8000> with auto-reload, and keeps the database next
to the code as `itam.db` instead of in the container volume.

On first run the app creates an `admin` account. If `ITAM_ADMIN_PASSWORD` is not
set it generates a password, prints it to the console, and forces a change at
first sign-in:

```
==================================================================
  ITAM first run - sign in with these credentials:
      admin / cB_SN7GHPbhhTt-G
  You will be asked to set a new password immediately.
==================================================================
```

First start also seeds demo people, assets and subscriptions so the app isn't
empty — clear them from **Admin → Remove demo users** once real users are
synced.

## Sign-in and accounts

Sign-in accounts are **local to this app** and completely separate from the
people synced out of Entra ID — those are inventory records, not logins. A
20-person Entra sync does not create 20 logins; you make a login only for the
people who actually administer the inventory.

Admins manage them under **Accounts**:

- **Add an account** — minimum 12-character password; the new account must
  choose its own password at first sign-in.
- **Roles** — *Admin* can manage accounts; *Standard* can do everything else
  (assets, subscriptions, assignments, Entra sync).
- **Reset someone's password** — signs out their sessions and forces a change.

Under the hood: passwords are salted **PBKDF2-HMAC-SHA256** (400,000 iterations,
stdlib only — no password is ever stored recoverably). Sessions live in the
database, so they expire and can be revoked server-side; the cookie holds only
an opaque random token and is `HttpOnly` + `SameSite=Lax`. Changing a password
revokes that account's other sessions. Repeated failed sign-ins lock an account
for 15 minutes.

Set `ITAM_COOKIE_SECURE=1` when you put this behind HTTPS.

## Connect Entra ID

1. In the Entra admin centre: **App registrations → New registration**
   (single tenant, no redirect URI needed — this is app-only).
2. **Certificates & secrets → New client secret**; copy the value.
3. **API permissions → Add → Microsoft Graph → Application permissions →
   `User.Read.All`**, then **Grant admin consent**.
4. Put the three values in `.env` — `setup.sh` prompts for them, or edit the
   file directly — and restart so the container picks them up:

```bash
docker compose up -d
```

5. Open **Admin → Sync users from Entra ID now**.

Credentials can be added at any time; until then the app runs on demo data.

Sync behaviour:

- Upserts on **UPN** (lowercased), so re-syncing never duplicates people and
  never disturbs existing assignments.
- **Never deletes.** Accounts disabled in Entra are flagged `disabled` here, so
  their assets and licences stay visible for reclaim — the dashboard calls out
  licences still assigned to disabled accounts.
- `ENTRA_USER_FILTER` optionally narrows the sync, e.g.
  `accountEnabled eq true`, or a department filter for a pilot rollout.

## What each screen does

- **Dashboard** — monthly SaaS run-rate and annualised figure, total hardware
  value, spare stock sitting idle, spend by subscription and by department, and
  a reclaim list of licences on disabled accounts.
- **People** — everyone with their asset count, asset value, licence count and
  monthly/annual licence cost. Searchable, filterable by department.
- **Person detail** — assign or return assets, grant or revoke licences, and a
  first-year total cost (assets + 12 months of licences).
- **Assets** — full inventory, filter by category and assigned/spare, add and
  edit assets, assign on creation.
- **Subscriptions** — per-seat cost, seat count, monthly and annual spend;
  manage seats per subscription.
- **Admin** — Entra sync, demo-data removal, CSV export.
- **Accounts** (admins only) — create, delete, and reset local sign-in accounts.
- **My account** — change your own password.

`/export/costs.csv` gives per-person costs for finance.

## Notes

- Money is stored as **integer cents**, never floats. Input accepts both
  `1,299.99` and `1.299,99` as well as space-grouped digits.
- Currency is a display label only (`ITAM_CURRENCY`, default `USD`); there is no
  FX conversion.
- Subscription cost is **per seat per month**, so a subscription's monthly total
  is `seats × per-seat cost`. Annual figures are `monthly × 12` — they do not
  model annual-prepay discounts or mid-month proration.
- Sign-in protects every page and every action; only the login page and the
  stylesheet are reachable without it. Cross-site form posts are blocked by the
  `SameSite=Lax` session cookie.
- It is still plain HTTP by default. If you serve it beyond your own machine,
  put it behind HTTPS (a reverse proxy is fine) and set `ITAM_COOKIE_SECURE=1`,
  so session cookies are never sent in the clear.
- `.env` holds a client secret and possibly an admin password. It is gitignored
  and `setup.sh` writes it mode 600 — keep it that way.
- The container runs as an unprivileged user (uid 10001), which is why `./data`
  must be owned by that uid. `setup.sh` handles it and verifies it before
  starting.
- One worker on purpose: SQLite serialises writes, and the sign-in lockout
  counter is per-process. Plenty for an internal inventory tool; this is not a
  design that wants horizontal scaling.
