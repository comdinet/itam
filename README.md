# ITAM — simple IT asset & subscription tracking

A small internal web app for tracking who has what and what it costs.
People come from **Entra ID**, keyed on **UPN**. Two kinds of cost:

| | What it is | Cost model |
|---|---|---|
| **Assets** | Laptops, monitors, phones, peripherals, perpetual software | one-off purchase cost |
| **Subscriptions** | SaaS licences (M365, GitHub, Slack, …) | cost per seat, per month |

Repository: <https://github.com/comdinet/itam>

Stack: FastAPI + SQLite + server-rendered HTML. No build step, no JavaScript
framework, no external services. One file for the database (`itam.db`).
Access is protected by **local username + password sign-in**, and the deployment
**serves HTTPS out of the box**.

A fresh install starts empty. People come from an Entra ID sync; hardware and
licences you enter yourself. There is no demo or sample data.

## Deploy on Ubuntu

Three commands on a fresh server:

```bash
sudo apt-get update && sudo apt-get install -y git
```

```bash
git clone https://github.com/comdinet/itam.git /opt/itam && cd /opt/itam
```

```bash
sudo ./setup.sh
```

`setup.sh` does the whole job: installs Docker if it is missing, asks for the
hostnames, the admin username and password, the currency, and optionally your
Entra ID credentials. It generates the self-signed certificate on the way. It writes `.env` (mode 600), prepares the data directory,
builds the image, verifies the container can actually write to the database,
starts the app behind an HTTPS terminator, waits for the health check, and
prints the URL and the credentials.

Non-interactive, for a scripted rollout:

```bash
sudo ./setup.sh --yes --hostname 'itam.example.com, itam' \
                --admin-user itadmin --admin-password 'a-long-passphrase'
```

Leave `--admin-password` off with `--yes` and it generates one and prints it.
Other flags: `--currency EUR`, `--no-start`, `--reconfigure`, `--help`.

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

Port 443 for the app, and port 80 because Caddy redirects HTTP to HTTPS.
Restrict the source range if you can:

```bash
sudo ufw allow from 10.0.0.0/8 to any port 443 proto tcp
sudo ufw allow from 10.0.0.0/8 to any port 80 proto tcp
```

### HTTPS

The stack serves HTTPS with a **self-signed certificate**. No ACME, no DNS
requirements, nothing to reach the internet for.

`./make-cert.sh` builds one certificate covering every name in
`ITAM_SITE_ADDRESS` plus `localhost` and this host's IP, valid for 10 years.
Caddy serves that single certificate for **every** request, whatever hostname
or IP is used, so no request can fail because a name was not configured.
`setup.sh` runs it for you.

To add a name later, edit `ITAM_SITE_ADDRESS` in `.env` — it is a value in a
file, not a shell command — then:

```bash
./make-cert.sh && docker compose restart caddy
```

Or pass the names directly:

```bash
./make-cert.sh itam.example.com itam 10.0.0.20 && docker compose restart caddy
```

Browsers show a warning the first time, because the certificate signs itself.
Click through it, or install `certs/itam.crt` as a trusted certificate on the
machines that use the app to stop the warning. `certs/` is gitignored — the
private key lives only on the server.

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
| Browser warns about the certificate | Expected — it is self-signed. Click through, or install `certs/itam.crt` on the client machines. |
| **"Secure Connection Failed" / `SSL_ERROR_INTERNAL_ERROR_ALERT`** | Caddy had no certificate to serve. Run `./make-cert.sh && docker compose restart caddy`. Confirm with `openssl s_client -connect HOST:443` — `alert number 80` means no certificate was loaded; check `docker compose logs caddy`. |
| `itam,: command not found` | An `.env` value was typed at the shell prompt. `ITAM_SITE_ADDRESS=...` belongs **inside** `.env`; editing that file is the only step. |
| Signed in, but immediately bounced back to the login page | The app is on plain HTTP while `ITAM_COOKIE_SECURE=1`, so the browser refuses to send the session cookie. Use HTTPS, or set it to 0. |
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

Serves on <http://127.0.0.1:8000> with auto-reload, keeps the database next to
the code as `itam.db` instead of in the container volume, and turns Secure
cookies off — that path is plain HTTP, so leaving them on would break sign-in
from anything but localhost.

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
3. **API permissions → Add → Microsoft Graph → Application permissions**, then
   **Grant admin consent**. One registration covers everything:

   | Permission | Enables |
   |---|---|
   | `User.Read.All` | people |
   | `Group.Read.All` | groups and membership |
   | `DeviceManagementManagedDevices.Read.All` | Intune devices |
   | `DeviceManagementConfiguration.Read.All` | macOS custom attributes |

   Only `User.Read.All` is required. Add the others when you want groups,
   devices, or attributes.
4. Put the three values in `.env` — `setup.sh` prompts for them, or edit the
   file directly — and restart so the container picks them up:

```bash
docker compose up -d
```

5. Open **Settings → Entra ID users from Entra ID now**.

Credentials can be added at any time; until then the app simply has no people.

Sync behaviour:

- Upserts on **UPN** (lowercased), so re-syncing never duplicates people and
  never disturbs existing assignments.
- **Never deletes.** Accounts disabled in Entra are flagged `disabled` here, so
  their assets and licences stay visible for reclaim — the dashboard calls out
  licences still assigned to disabled accounts.
- `ENTRA_USER_FILTER` optionally narrows the sync. For real staff only —
  no disabled accounts, no guests:

  ```
  ENTRA_USER_FILTER=accountEnabled eq true and userType eq 'Member'
  ```

  That line goes **inside `.env`**. Values may contain spaces and quotes exactly
  as written; do not wrap the whole value in quotes. Then
  `docker compose up -d`.

  Whenever a filter is set, the sync sends Graph's advanced query headers
  (`ConsistencyLevel: eventual` and `$count=true`), because several directory
  filters — `userType`, `ne`, `not`, `startsWith` — are rejected without them.
  `ENTRA_GROUP_FILTER` works the same way. `INTUNE_DEVICE_FILTER` does not:
  Intune's `managedDevices` has no advanced query support, so keep those
  filters simple, e.g. `operatingSystem eq 'macOS'`.

  The filters currently in effect are shown on **Settings → Entra ID**.

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
- **Settings** — subsections:
  - **General** — counts, plus every current setting and the environment
    variable behind it. Read-only; change these in `.env` and restart.
  - **Entra ID** — configuration status and the user sync.
  - **Groups** — Entra groups and their membership; the basis for rules.
  - **Devices** — Intune devices, their macOS custom attributes, and the link
    to assets.
  - **Rules** — entitlement rules and who is short against them.
  - **Accounts** (admins only) — create, delete, and reset local sign-in accounts.
  - **API** (admins only) — API keys, field mapping, and the recent call log.
- **My account** — change your own password.

`/export/costs.csv` gives per-person costs for finance.


## Groups, devices and rules

### Groups

**Settings → Groups** syncs Entra groups and their membership. Run the user
sync first: only people already in ITAM can be linked, and the group list shows
how many members it could not match (nested groups, service principals, or
someone who joined since the last user sync).

Membership is replaced on every sync, so someone removed from a group in Entra
stops counting here too.

### Devices

**Settings → Devices** pulls managed devices from Intune. A device is what
Intune reports; an asset is what you paid for. They are matched on **serial
number** — where a serial matches, the device links to that asset. A link made
by hand survives later syncs even if the serial never matches.

For a device with no asset, **Create asset** makes one from the device details
and links them, leaving you to fill in the cost.

**macOS custom attributes** are shell scripts in Intune whose output Intune
stores per device. *Sync macOS custom attributes* reads those results and merges
them onto the device, so an attribute reporting CPU and RAM shows up on the
device row:

```
HEDY-MBP    CPU and RAM: Apple M4 Pro / 36 GB
```

Whatever the script prints is stored under the script's display name, so
anything you already collect this way carries over without configuration. This
uses the Graph **beta** endpoint, because custom attribute shell scripts have no
v1.0 equivalent.

### Rules

**Settings → Rules** holds entitlement rules: what a group of people should
have. "Everyone in Design gets 2 monitors" is a group, an asset category, and a
quantity.

Rules can target a synced Entra group, or the built-in **Everyone (all users)**
target, which needs no group sync at all — useful for "everyone gets a laptop"
and for getting going before `Group.Read.All` is consented.

A rule reports rather than acts. The Rules page shows, per rule, how many
members are compliant, how many are short, how many items that adds up to, and
how many spares you have to cover it. The rule's own page lists every member
with what they have against what they should.

**Apply** closes the gaps it can:

- **Assets** are only ever taken from existing spares. ITAM will not invent
  hardware — an asset record for kit nobody owns is worse than no record. If
  there are fewer spares than the rule needs, it assigns what exists and names
  who was left short.
- **Licences** are granted outright, since a seat is just a record. This adds
  to the monthly run-rate, so the number of seats it will grant is shown before
  you click.

Members are served in name order, so with stock too short for everyone the
earlier names are filled first and the rest are reported. Nothing is ever
un-assigned: someone holding more than a rule asks for is flagged as
over-provisioned and left alone, for you to reclaim by hand.

Rules can be paused, which keeps them without evaluating them. Deleting a group
deletes its rules.

## API for webhooks

Other systems can create assets in ITAM over HTTP — for example a Frappe
webhook that fires when hardware is approved for someone. This is separate from
the browser sign-in: callers authenticate with a bearer token.

Set it up under **Settings → API**.

### 1. Create a key

Give it a name and choose what it may do:

| Permission | Meaning |
|---|---|
| Create assets | may add assets at all |
| Assign to people | may set the holder. Without it, assets land in spares |

The token is shown **once**. Only a SHA-256 of it is stored, so a lost token
means deleting the key and issuing a new one. Disabling a key revokes it
immediately.

Check a token before wiring anything up:

```bash
curl -H "Authorization: Bearer itam_..." https://itam.example.com/api/v1/ping
```

### 2. Map the fields

Your system's field names almost certainly differ from ITAM's. The mapping
table translates them — left is the incoming field, right is the ITAM field.
ITAM's own field names are mapped to themselves out of the box; add your
system's on top.

The eight fields an asset can take: `name`, `category`, `cost`, `serial`,
`purchased_on`, `notes`, `assigned_upn`, `external_id`.

- `name` is required.
- `assigned_upn` must match a synced user's UPN. It is lowercased, so
  `Grace.Hopper@Company.com` matches fine.
- `category` is free text. A category ITAM has not seen before is accepted and
  starts appearing in the dropdowns.
- `cost` accepts `129.00`, `129,00` and `1 299,50` alike.
- `external_id` is what makes retries safe — see below.

Anything **not** mapped is ignored and listed back in the response as
`ignored_fields`, so a wrong webhook config shows up instead of silently
dropping data.

### 3. Point the webhook at it

```
POST https://itam.example.com/api/v1/assets
Authorization: Bearer itam_...
Content-Type: application/json

{
  "item_group": "Overhead headphones",
  "item_name": "ULT900",
  "employee_email": "grace.hopper@yourcompany.com",
  "serial_no": "SN-ULT900-77",
  "rate": 129.00,
  "doc_name": "HR-AST-2026-00042"
}
```

With `item_group→category`, `item_name→name`, `employee_email→assigned_upn`,
`serial_no→serial`, `rate→cost` and `doc_name→external_id`, that returns:

```json
{
  "status": "created",
  "asset_id": 1,
  "name": "ULT900",
  "category": "Overhead headphones",
  "assigned_upn": "grace.hopper@yourcompany.com",
  "cost": "129.00",
  "currency": "USD",
  "ignored_fields": ["approved_by", "workflow_state"]
}
```

### Retries are safe

Send your source document's identifier as `external_id`. A repeat of the same
webhook returns `200` with `"status": "already_exists"` and the original asset
id, instead of creating a second row. Without an `external_id`, every delivery
creates a new asset — so map it if your sender retries at all.

### Responses

| Code | Meaning |
|---|---|
| `201` | asset created |
| `200` | this `external_id` already existed; nothing changed |
| `400` | body was not a JSON object, or no `name` after mapping |
| `401` | token missing, wrong, or disabled |
| `403` | the key lacks that permission |
| `422` | `assigned_upn` matches no known user — sync Entra ID first |
| `500` | unexpected failure; the reason is in the call log |

Every call is recorded under **Settings → API**, most recent first, with the
status and outcome — the first place to look when a webhook "didn't work". The
last 200 calls are kept.


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
