# ITAM — simple IT asset & subscription tracking

A small internal web app for tracking who has what and what it costs.
People come from **Entra ID**, keyed on **UPN**. Two kinds of cost:

| | What it is | Cost model |
|---|---|---|
| **Assets** | Kit you own, by category: laptops, monitors, phones, peripherals, software | one-off cost in any currency — serial-tracked items get a row each, interchangeable ones are counted in bulk |
| **Subscriptions** | SaaS licences (M365, GitHub, Slack, …) | cost per seat, per month |

Repository: <https://github.com/comdinet/itam>

Amounts are recorded **in the currency they were paid in** — shekels, euros,
pounds, dollars — and consolidated figures are converted at a rate frozen when
the amount was entered.

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

### Two-factor authentication

Any account can add TOTP two-factor from **My account** — the standard
6-digit / 30-second codes, so Microsoft Authenticator, Google Authenticator,
1Password and the rest all work. Scan the QR code, or type the key in by hand.

Two-factor only switches on once a code from the app matches, so a mis-scanned
key cannot lock you out. Turning it on issues **8 single-use recovery codes**,
shown once and stored only as hashes.

- **Lost phone, has a recovery code** → use it on the sign-in screen.
- **Lost phone, no codes left** → an admin clears it under
  **Settings → Accounts → Reset 2FA**, which also signs that account out
  everywhere.

Set it up under **My account**, which is linked from the Settings tabs as well
as the header. Once it is on, **Test a code** there confirms your authenticator
is in sync — it checks the code *without consuming it*, so your next sign-in
still works with the same one.

Before turning it on for everyone, **Settings → General** shows a two-factor
readiness table: every sign-in account, whether it has two-factor, and what
would happen to it if you required it. It warns if your own account is not
covered yet — test it on your own phone first. Requiring it locks nobody out:
an account without it still signs in with its password but reaches only its own
account page until it is set up, and single-sign-on accounts are exempt.
- **Require it for everyone** → set `ITAM_REQUIRE_2FA=1`. Accounts without it
  can reach only their own account page until they set it up, and nobody can
  then turn it off.

Set `ITAM_TOTP_ISSUER` to change the name shown in the authenticator app; it
defaults to `ITAM`.

### Single sign-on with Entra ID (SAML 2.0)

Optional, and it sits alongside local accounts rather than replacing them.
Configure it and the sign-in page grows a **Sign in with Microsoft** button;
**Settings → SSO** shows the exact values to paste into Entra and what is
currently configured.

In Entra: **Enterprise applications → New application → Create your own →
Set up single sign-on → SAML**, then give it

| Entra field | Value |
|---|---|
| Identifier (Entity ID) | `https://<your host>/saml/metadata` |
| Reply URL (ACS) | `https://<your host>/saml/acs` |
| Sign on URL | `https://<your host>/saml/login` |

and copy back into `.env`:

```
ITAM_SAML_SP_BASE_URL=https://<your host>
ITAM_SAML_IDP_ENTITY_ID=<Microsoft Entra Identifier>
ITAM_SAML_IDP_SSO_URL=<Login URL>
ITAM_SAML_IDP_CERT=<the Base64 certificate>
```

`/saml/metadata` serves SP metadata if you would rather upload it.

By default an SSO sign-in only works for an account that **already exists** —
otherwise anyone in the tenant who finds the URL gets in. Set
`ITAM_SAML_AUTO_PROVISION=1` to create accounts on first sign-in, and
`ITAM_SAML_ADMIN_GROUP` to grant admin from a group claim.

SSO accounts satisfy `ITAM_REQUIRE_2FA` on their own, since Entra has already
applied whatever MFA policy you have there.

**Keep a local admin account with a password and two-factor.** If Entra is
unreachable or the app registration is changed, that is how you get back in.

### How the sign-in is protected

Passwords are salted **PBKDF2-HMAC-SHA256** (400,000 iterations, stdlib only —
no password is ever stored recoverably). Sessions live in the database, so they
expire and can be revoked server-side; the cookie holds only an opaque random
token and is `HttpOnly` + `SameSite=Lax`. Changing a password revokes that
account's other sessions. Repeated failed sign-ins lock an account for 15
minutes.

TOTP specifics worth knowing:

- Codes are accepted one window either side of now, for clock drift. Anything
  further out is refused.
- A code that has been used **cannot be replayed**, even inside its own 30
  seconds. Signing in twice in the same window gives "that code was already
  used — wait for the next one", and that message does **not** count toward the
  lockout, because it is not a failed guess.
- The password step alone never issues a session for a 2FA account. It hands
  out a separate short-lived token that expires in 5 minutes and is good for
  nothing but the second step.

SAML specifics, all of which are covered by tests that craft real signed
assertions and try to get past them:

- Assertions must be signed, and the signature is checked against the IdP
  certificate you configured. Unsigned, signed-by-another-key, and
  tampered-after-signing are all refused.
- Audience, destination and issuer must match; `NotBefore` / `NotOnOrAfter`
  are enforced.
- A response must answer an `AuthnRequest` this app issued. Unsolicited
  assertions are refused unless `ITAM_SAML_ALLOW_IDP_INITIATED=1`, and each
  request id is good for exactly one response.
- Assertion ids are recorded for 24 hours, so a captured response cannot be
  replayed.
- The audience is built from `ITAM_SAML_SP_BASE_URL`, never from the `Host`
  header, so a forwarded header cannot change what an assertion is validated
  against.

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
   | `Organization.Read.All` | licence SKUs the tenant owns |
   | `Group.Read.All` | groups and membership |
   | `DeviceManagementManagedDevices.Read.All` | Intune devices |
   | `DeviceManagementScripts.Read.All` | macOS custom attributes |

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
  value, spare kit sitting idle, spend by subscription and by department, and
  a reclaim list of licences on disabled accounts.
- **People** — everyone with their asset count, asset value, licence count and
  monthly/annual licence cost. Searchable, filterable by department.
- **Person detail** — assign or return assets, grant or revoke licences, and a
  first-year total cost (assets + 12 months of licences).
- **Assets** — one tab per category. Each tab lists what you own in it, both
  the serial-tracked items and the ones counted in bulk, with an **Add** for
  each kind that files it under the category you are on. Search by name or
  serial and filter by assigned/spare.
- **Subscriptions** — per-seat cost, seat count, monthly and annual spend;
  manage seats per subscription.
- **Settings** — subsections:
  - **General** — counts, plus the application settings, **editable here**.
    Below them, the handful that must stay in `.env`, with the reason.
  - **Entra ID** — configuration status and the user sync.
  - **Groups** — Entra groups and their membership; the basis for rules.
  - **Devices** — Intune devices, their macOS custom attributes, and the link
    to assets.
  - **Rules** — entitlement rules and who is short against them.
  - **Accounts** (admins only) — create, delete, and reset local sign-in accounts.
  - **API** (admins only) — API keys, field mapping, and the recent call log.
- **My account** — change your own password.

`/export/costs.csv` gives per-person costs for finance.


## Licences

**Settings → Licences** reads what the tenant owns from Entra
(`/subscribedSkus`) and who holds each SKU, and shows purchased vs assigned vs
unused per licence.

Nothing is matched against a hardcoded product GUID. Published GUID lists
disagree with one another, so the tenant's own `/subscribedSkus` is the source
of truth for which SKUs exist, and friendly names are keyed on the stable
string ID:

| String ID | Shown as |
|---|---|
| `SPB` | Microsoft 365 Business Premium |
| `SPB_NOTEAMS` | Microsoft 365 Business Premium (no Teams) |

Any SKU not in that map is listed under its raw string ID rather than hidden,
so nothing goes missing.

**Create subscription** turns a licence into a tracked subscription and grants
its current holders a seat, so the counts line up with Entra immediately. The
cost starts at zero: Entra knows who holds a licence, not what you pay for it,
so set the per-seat price on the Subscriptions page. The licence row then links
to the subscription and flags it while no cost is set.

Two numbers are worth watching:

- **Unused** — purchased minus assigned, straight from Entra. Seats you pay for
  and nobody has.
- **On disabled accounts** — licences held by people whose Entra account is
  disabled. Reclaimable immediately, and called out at the top of the page.

Licence assignment uses the same `ENTRA_USER_FILTER` as the user sync, so the
two cannot disagree about who is in scope. Where Entra's tenant-wide "assigned"
count exceeds "held by synced people", the difference is licensed accounts your
filter excludes — usually guests and service accounts.

## Scheduled syncs

`./sync.sh` runs the syncs in dependency order — users first, then groups,
licences, devices, and custom attributes:

```bash
./sync.sh              # everything
./sync.sh licences     # just one
```

Nightly at 03:00 (`crontab -e`):

```
0 3 * * * cd /opt/itam && ./sync.sh >> /var/log/itam-sync.log 2>&1
```

Every job is safe to re-run: syncs upsert and never delete inventory. Each line
of output says what changed, and the exit status is non-zero if any job failed,
so cron reports real failures instead of swallowing them.

## Currencies

Money is stored in the currency it was paid in and never converted on the way
in. A laptop bought in Tel Aviv is ₪11,900 permanently — that is what the
invoice says. Conversion happens only when figures are added together, and the
amount, its currency, and the converted figure are all shown.

**The rate is frozen when the amount is entered.** Last year's totals do not
move because the shekel did, which is what book value means. Changing a rate
today affects only what is entered from today — there is a test asserting
exactly that.

### Setting rates

**Settings → Currencies**. Two ways:

- **Check rates at Bank of Israel** fetches today's published rates and shows
  them **for approval**. Nothing is stored until you tick and confirm, so a
  published rate never moves your books on its own. BoI quotes everything
  against the shekel, so one call yields all of them, expressed against your
  reporting currency.
- **Set a rate by hand** for anything BoI does not publish, or when you want a
  specific figure.

Every approval is recorded — rate, date, source, and who approved it — so a
converted total can be explained months later.

A currency in use cannot be deleted or switched off, and the reporting currency
(`ITAM_CURRENCY`, default `USD`) is always present at exactly 1.

### Entering amounts

Every form that takes money has a **currency picker with no default**. The
choice is always explicit: guessing one is how a shekel purchase silently
becomes dollars. An amount submitted without a currency is refused.

Editing a record keeps its frozen rate while the currency is unchanged, so
fixing a typo in a name never revalues the purchase.

### Reading totals

- **Dashboard** shows the consolidated figure *and* a per-currency breakdown
  underneath, so the number is checkable. The amounts on the left are exact;
  the converted column is only as good as the rates.
- **People** marks anyone holding items in more than one currency.
- **A person's page** shows each line in what was paid, with the converted
  figure beside it.
- **Pricing groups** are priced in one currency, and applying one carries that
  currency onto the assets it prices. A group covering both Israeli and UK
  laptops needs splitting in two.

Rates are stored as USD-per-unit × 1,000,000, and every conversion is integer
arithmetic with explicit half-up rounding — floats would drift, and Python's
`round()` rounds halves to even, which is not what money does.

## Assets: by category, tracked one by one or counted in bulk

**Assets** has one page per category — Laptop, Monitor, Peripheral, Software and
whatever else your kit introduces — reached from the row of tabs at the top.
Each page lists everything in that category and carries its own two **Add**
buttons, which file the new item under the category you are looking at.

Within a category there are two ways to record something, because there are two
kinds of thing:

**Tracked individually.** A laptop has a serial and belongs to one person, so it
gets a row of its own with that serial, its cost and its holder.

**Counted in bulk.** A mouse does not. Fifty identical mice as fifty rows is
noise, and the useful questions are *how many do we own, how many are out, what
did they cost*. So one record carries a **unit price** and **how many units are
owned**. Handing one out counts against the total; cost follows the units, so
someone holding two of a 25.00 item carries 50.00.

The same shape fits licences bought in bulk. Four JetBrains seats are one
purchase at 779.00 each, not four assets:

```
JetBrains All Products Pack   779.00/unit   owned 4   out 2   spare 2   3,116.00
```

Hand a seat to someone and the count goes up; the cost lands on them
automatically. Handing out more than you own is refused, and the quantity owned
cannot drop below what is already out — take some back first.

Which to use:

- **Tracked individually** — it has a serial and one owner.
- **Counted in bulk** — units are interchangeable, bought as a batch, one-off cost.
- **Subscriptions** — a recurring monthly charge per person.

Both kinds are assets to everything downstream: entitlement rules grant either,
and both feed the People list, each person's page, their first-year total, the
dashboard and the finance CSV (`pooled_units`, `pooled_value`, `onetime_total`).

## Pricing by specification

**Settings → Pricing** prices a fleet by spec instead of one machine at a time.
A group is a set of criteria with a price; applying it writes that price onto
every asset matching them.

The simple case — everything of one model:

| | |
|---|---|
| Name | Latitude 5450 |
| Price | 599.00 |
| Criterion | Model **is exactly** `Latitude 5450` |

Model alone is often not enough, though: two MacBook Airs of the same model
differ by memory and disk, and on macOS those arrive as Intune **custom
attributes**. So criteria can match those too, and all criteria must match:

| | |
|---|---|
| Name | MacBook Air 13 M4 16/512 |
| Price | 1299.00 |
| Criterion | Model **contains** `MacBook Air` |
| Criterion | Attribute `CPU and RAM` **contains** `16 GB` |
| Criterion | Attribute `Disk` **is exactly** `512 GB` |

That prices the 16/512 machines and leaves an 8/256 of the same model alone.

Criteria can match model, manufacturer, operating system, category, or any
custom attribute, with **is exactly**, **contains**, or **starts with** — all
case-insensitive. Model falls back to the asset name for assets with no linked
Intune device.

Notes on behaviour:

- A group with **no criteria matches nothing**, deliberately. An empty group
  silently repricing the whole estate would be worse than doing nothing.
- Applying is repeatable: it only touches assets not already at the price, and
  the page shows how many would change before you commit.
- Deleting a group leaves the prices it set alone. It is a pricing tool, not
  an owner of the data.
- Creating an asset from an Intune device **prices it automatically** if a
  group covers its specification, so a new machine of a known spec never lands
  at zero.

## Groups, devices and rules

### Groups

**Settings → Groups** syncs Entra groups and their membership. Run the user
sync first: only people already in ITAM can be linked, and the group list shows
how many members it could not match (nested groups, service principals, or
someone who joined since the last user sync).

Membership comes from Entra's `transitiveMembers`, so people in **nested
groups** are included — a group of groups resolves to the actual people.

Membership is replaced on every sync, so someone removed from a group in Entra
stops counting here too.

If a group shows fewer people than Entra does, open it: the **Not matched to an
ITAM user** section lists those UPNs by name and says why. Almost always it is
`ENTRA_USER_FILTER` excluding them — guests, disabled accounts, or anyone with
a null `userType` if you filter on `userType eq 'Member'`.

### Devices

**Settings → Devices** pulls managed devices from Intune. A device is what
Intune reports; an asset is what you paid for. They are matched on **serial
number** — where a serial matches, the device links to that asset. A link made
by hand survives later syncs even if the serial never matches.

The **Linked or not** filter separates devices already in ITAM from those that
are not — *"Show only missing from ITAM"* jumps straight to the gap.

**Create asset** does one device. **Create assets for N device(s) not in ITAM**
does the whole filtered set at once, so onboarding a delivery is one click
rather than fifty. It is scoped to the filters showing, so you can bulk-create
just the Macs, or just one model. Each asset is named after the model, linked to
its device, and **priced from a matching pricing group** where one covers the
spec. Re-running it finds nothing, since the devices are then linked.

**macOS custom attributes** are shell scripts in Intune whose output Intune
stores per device. Needs `DeviceManagementScripts.Read.All` — Microsoft moved
this endpoint off `DeviceManagementConfiguration.*` in July 2025, so older
guides name the wrong permission. *Sync macOS custom attributes* reads those results and merges
them onto the device, so an attribute reporting CPU and RAM shows up on the
device row:

```
HEDY-MBP    CPU and RAM: Apple M4 Pro / 36 GB
```

Whatever the script prints is stored under Intune's `customAttributeName`, so
anything you already collect this way carries over without configuration.

If you only want some of them, set **`INTUNE_ATTRIBUTE_FILTER`** under
**Settings → Entra ID** — a comma-separated list of attribute names, with `*`
wildcards allowed and case ignored:

```
INTUNE_ATTRIBUTE_FILTER=CPU and RAM, Warranty
INTUNE_ATTRIBUTE_FILTER=CPU*
```

Blank means all of them. Filtering happens on the script list, *before* the
per-script device-state calls, so an excluded attribute costs no requests at
all — each one is a separate paged call over your whole device estate.

Two things worth knowing:

- Values for attributes the filter no longer covers are **removed** on the next
  sync, so narrowing the filter actually cleans up rather than leaving orphans.
- The sync message lists every attribute name Intune reports, filtered or not,
  so you can see what is available and copy the names you want. The Devices
  page shows the filter in effect and which attributes are currently held. This
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

### What a rule grants

The form reveals itself a step at a time. Choose whether the rule grants an
**asset** or a **licence**, and only the fields for that appear:

- **Asset** → choose a **category**, then the **item** within it. The item list
  is the distinct names you already own in that category, so a rule can grant
  *two Dell U2723QE* rather than *two of any monitor*. Leave it on
  *any in this category* for the broader version.
- **Licence** → choose the subscription. Quantity disappears, since a licence
  is one per person.

A rule naming an item only ever hands out that item: with three Dells and two
LGs spare, a Dell rule leaves the LGs alone.

### Who a rule covers

A rule applies to one group, then narrows or widens it:

- **Except members of** — excluded even if another condition would include them.
- **Also include members of** — a second group covered by the same rule.

So *"everyone in Israel gets 2 monitors, except CSE"* is the Israel group with
CSE excluded. Exclusion always wins over inclusion.

### A rule serves each person once

Applying a rule records who it served. Someone already served is **finished
with** — if they hand a monitor back later, the rule does not quietly issue
another. It is a one-time entitlement, not a level the app keeps restoring.

Three cases, deliberately different:

- **Served in full** → recorded, never revisited.
- **Served in part** (ran out mid-way) → *not* recorded, so a later apply
  completes them once more arrives. Finishing an entitlement is not the same
  as topping someone up.
- **Already had enough** by other means → recorded without consuming anything,
  so the rule does not come back to them later.

A new joiner in the group is short and gets served; everyone already served is
left alone. If a rule was applied by mistake, **Allow again** on a person — or
on the whole rule — lets it serve them once more.

**Apply** closes the gaps it can:

- **Assets** are only ever taken from existing spares — a spare serial-tracked
  item first, then a spare unit from the bulk count. ITAM will not invent
  hardware: an asset record for kit nobody owns is worse than no record. If
  there are fewer spares than the rule needs, it assigns what exists and names
  who was left short. What a person already holds counts either way, so two
  mice out of the pool satisfy a rule asking for two mice.
- **Licences** are granted outright, since a seat is just a record. This adds
  to the monthly run-rate, so the number of seats it will grant is shown before
  you click.

Members are served in name order, so with too little spare for everyone the
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


## Settings: `.env` or the UI

Most settings are editable under **Settings**, by an admin, and take effect
immediately — no restart. Each row shows where the value in use comes from:

| Source | Meaning |
|---|---|
| `set here` | stored in this database, overriding everything else |
| `.env` | no override; the environment value is in use |
| `default` | neither is set; the built-in default applies |

**Reset** removes the stored override so the `.env` value applies again. So
`.env` keeps working exactly as before — it is now the default layer rather
than the only one, and an existing deployment needs no changes.

Editable in the UI: currency, session length, require-2FA, authenticator issuer
name, the Entra tenant/client/secret and the three OData filters, and the whole
SAML configuration.

These stay in `.env`, because a database row could not work:

| Variable | Why |
|---|---|
| `ITAM_DB` | Opening the database comes before reading any setting |
| `ITAM_SITE_ADDRESS` | Certificate names, and what Caddy serves |
| `ITAM_HTTP_PORT`, `ITAM_HTTPS_PORT`, `ITAM_PORT` | Published by Compose, outside the app |
| `ITAM_COOKIE_SECURE` | Transport-level; setting it wrong locks everyone out |
| `ITAM_ADMIN_USER`, `ITAM_ADMIN_PASSWORD` | Used once, to create the first account |

The Entra client secret is **write-only** in the UI: it shows as set or not
set, is never rendered back to the page, and submitting the field blank leaves
the stored value alone. It does mean the secret is in `itam.db` — which already
holds password hashes, TOTP secrets and session tokens, so treat that file and
your backups as secrets either way.

## Tests

```bash
./tests/run_all.sh
```

Sixteen suites covering money parsing, currencies and frozen rates, the Entra
user/group/licence syncs, Intune devices and macOS custom attributes, OData
filters, pricing groups, pooled assets, the entitlement rules (including the
ones that grant from the pool), the schema migration, and two-factor
authentication - plus a static check that no template reads a name its route
does not pass. Each uses a throwaway database and a mocked Graph, so none of
them touch a real tenant or your data.

The SAML suite is separate because it needs `xmlsec` to sign assertions, which
lives in the container:

```bash
docker compose cp tests/saml_attacks.py itam:/tmp/ && \
docker compose exec -w /srv/itam itam python /tmp/saml_attacks.py
```

It stands up a miniature identity provider and tries 21 ways to get past the
SAML endpoint. Worth running after any change near sign-in.

## Notes

- Upgrading from a version that had a separate **Stock** section: nothing to
  do. The first start renames the tables to match the vocabulary, carrying
  every unit, allocation and frozen rate across; `/stock` is gone, and its
  contents are on the category pages under **Assets**. The finance CSV columns
  `stock_units` / `stock_value` are now `pooled_units` / `pooled_value`.
- Money is stored as **integer cents**, never floats. Input accepts both
  `1,299.99` and `1.299,99` as well as space-grouped digits.
- Every amount is recorded in the currency it was paid in, at a rate frozen at
  that moment. Consolidated figures are converted to the reporting currency
  (`ITAM_CURRENCY`, default `USD`) — see **Currencies** above.
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
