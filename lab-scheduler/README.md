# Lab Experiment Scheduler

A lightweight self-hosted app for scheduling shared lab equipment/experiments,
detecting date-range conflicts automatically, and letting an admin resolve
them. Built with **FastAPI + SQLite + Jinja2/Bootstrap** — no JS framework,
no separate frontend build step, one container.

## Directory structure

```
lab-scheduler/
├── app/
│   ├── main.py            # FastAPI app & routes
│   ├── models.py          # SQLAlchemy Booking model
│   ├── schemas.py         # Pydantic validation
│   ├── database.py        # DB engine/session setup
│   ├── conflict.py         # Conflict detection engine
│   ├── auth.py             # Admin HTTP Basic Auth
│   ├── notifications.py    # Email/webhook notification stubs
│   ├── static/style.css
│   └── templates/
│       ├── base.html
│       ├── index.html        # Booking submission form
│       ├── confirmation.html
│       ├── dashboard.html    # Public approved schedule
│       └── admin.html        # Admin conflict resolution panel
├── data/                   # SQLite DB lives here (volume-mounted)
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── .dockerignore
```

## How it works

1. **Booking form (`/`)** — any lab member enters their name, equipment name,
   experiment details, date range, and a priority (1=critical, 5=flexible).
2. **Conflict engine** — on submit, the booking is saved as `pending`, then
   the app queries for any other `pending`/`approved` booking on the *same
   equipment* whose date range overlaps (`start <= other.end AND end >=
   other.start`). If found, it's flagged and a notification stub fires.
3. **Notifications** — `app/notifications.py` logs every conflict to stdout
   and, if configured, also posts to a webhook URL and/or sends an email via
   SMTP. Both are no-ops until you fill in the relevant environment
   variables — nothing is sent by default.
4. **Admin panel (`/admin`, HTTP Basic Auth)** — shows all pending requests
   with conflicting rows highlighted in yellow, listing exactly which other
   booking(s) they clash with (including priority) so the admin can decide.
   One click Approves or Denies each request. Approved bookings can be
   revoked; denied ones can be re-approved.
5. **Dashboard (`/dashboard`)** — public, read-only list of all *approved*
   bookings only, so lab members always see the finalized schedule.

## Running locally (without Docker, for development)

```bash
cd lab-scheduler
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
mkdir -p data
uvicorn app.main:app --reload --port 8000
```

Then visit `http://localhost:8000`. The SQLite database
(`data/lab_scheduler.db`) and its tables are created automatically on first
run — there's no separate init step needed.

Default admin login is `admin` / `changeme` (from the environment variable
defaults in `app/auth.py`) — **set your own via env vars before deploying**.

## Deploying on TrueNAS with Tailscale Funnel (Isolated & Public HTTPS)

This stack includes a dedicated, unprivileged **Tailscale Funnel sidecar container** (`ts-funnel`) paired with a hardened **Lab Scheduler container** (`lab-scheduler`):
- **Public Browser Access**: Lab members open `https://lab-scheduler.<your-tailnet>.ts.net` from **any regular web browser** (no Tailscale client or VPN needed on lab computers).
- **Local Network & Host Isolation**:
  - `ts-funnel` runs in **userspace networking mode** (`TS_USERSPACE=true`) — no host networking (`network_mode: host`), no `/dev/net/tun`, and no `NET_ADMIN` privileges.
  - `lab-scheduler` runs with `read_only: true` root filesystem, `cap_drop: [ALL]`, `no-new-privileges`, and shares only the sidecar's loopback (`127.0.0.1:8000`).
  - Zero router ports are opened, and zero ports need to be exposed on your TrueNAS LAN IP.

### Step 1: One-Time Tailscale Admin Console Setup
1. **Enable HTTPS Certificates**:
   - In the [Tailscale Admin Console → DNS](https://login.tailscale.com/admin/dns), make sure **MagicDNS** is enabled and click **Enable HTTPS** under *HTTPS Certificates*.
2. **Allow Funnel in Access Controls (ACLs)** & Isolate from Tailnet Devices:
   - Go to **Access Controls** in the Tailscale Admin Console.
   - Ensure the `funnel` nodeAttr is enabled (Tailscale adds this by default when enabling Funnel; optionally restrict it to `tag:funnel-only` so this container cannot initiate connections to any other device on your tailnet):
     ```json
     "tagOwners": {
       "tag:funnel-only": ["autogroup:admin"]
     },
     "nodeAttrs": [
       {
         "target": ["autogroup:member", "tag:funnel-only"],
         "attr": ["funnel"]
       }
     ]
     ```
3. **Generate an Auth Key**:
   - Go to [Settings → Keys](https://login.tailscale.com/admin/settings/keys) → **Generate auth key**.
   - Recommended: Enable **Reusable** (or single-use) and optionally attach tag `tag:funnel-only` (a tagged node has zero outbound access to your other Tailscale devices unless explicitly granted in ACLs).
   - Copy the `tskey-auth-...` key.

### Step 2: Run as an Isolated Container Stack (or VM) on TrueNAS
1. Copy the `lab-scheduler/` folder onto your TrueNAS dataset (e.g., `/mnt/<pool>/apps/lab-scheduler/`) — or inside a lightweight Ubuntu VM in **TrueNAS → Virtualization** if you also want full KVM hypervisor separation from the TrueNAS kernel.
2. Create `.env` from `.env.example` (or edit `docker-compose.yml` directly):
   ```bash
   cp .env.example .env
   ```
   Set:
   - `TS_AUTHKEY=tskey-auth-...` (your Tailscale Auth Key)
   - `ADMIN_PASSWORD=...` (a strong password for `/admin`)
3. Start the stack:
   ```bash
   docker compose up -d --build
   ```
4. Check the public Funnel URL in the logs:
   ```bash
   docker exec lab-scheduler-ts tailscale funnel status
   ```
   Your app is now live at **`https://lab-scheduler.<your-tailnet-name>.ts.net`**!

1. Copy the whole `lab-scheduler/` folder onto your TrueNAS box, e.g. into
   `/mnt/<pool>/apps/lab-scheduler/`.
2. Edit `docker-compose.yml`:
   - Change `ADMIN_PASSWORD` to something real.
   - Optionally set `WEBHOOK_URL` and/or the `SMTP_*` / `NOTIFY_EMAIL_TO`
     variables to enable live notifications.
   - Optionally point the `volumes:` line at a proper TrueNAS dataset
     instead of the relative `./data` folder, e.g.:
     ```yaml
     volumes:
       - /mnt/mypool/apps/lab-scheduler/data:/app/data
     ```
   - Change the host port on the left of `"8000:8000"` if 8000 is already
     used on your NAS.
3. From the TrueNAS shell (or via SSH), in that directory run:
   ```bash
   docker compose up -d --build
   ```
   TrueNAS SCALE's **Apps → Discover Apps → Custom App** launcher can also
   point at this same `docker-compose.yml`/Dockerfile if you prefer to
   manage it through the GUI (Launch Docker Image, set the image build
   context to this folder, map port 8000, and mount `/app/data` to a
   dataset).
4. Visit `http://<truenas-ip>:8000` to submit bookings, and
   `http://<truenas-ip>:8000/admin` (prompts for the Basic Auth login) to
   manage conflicts.

### Updating

```bash
docker compose down
docker compose up -d --build
```

The SQLite file in the mounted `data/` volume persists across rebuilds, so
no bookings are lost.

### Backing up

The entire database is the single file `data/lab_scheduler.db`. Back up that
one file (e.g. via a TrueNAS periodic snapshot task on the dataset it lives
in) to protect your scheduling history.

## Configuration reference

| Variable          | Purpose                                              | Default   |
|--------------------|-------------------------------------------------------|-----------|
| `ADMIN_USERNAME`   | Admin panel login username                            | `admin`   |
| `ADMIN_PASSWORD`   | Admin panel login password                            | `changeme`|
| `WEBHOOK_URL`      | POSTs `{"text": "..."}` here on every detected conflict | *(unset)* |
| `SMTP_HOST`        | SMTP server for email notifications                   | *(unset)* |
| `SMTP_PORT`        | SMTP port                                              | `587`     |
| `SMTP_USER`        | SMTP auth username                                     | *(unset)* |
| `SMTP_PASSWORD`    | SMTP auth password                                     | *(unset)* |
| `SMTP_FROM`        | From address for notification emails                  | `SMTP_USER` |
| `NOTIFY_EMAIL_TO`  | Comma-separated recipient list                         | *(unset)* |
| `DATABASE_URL`     | SQLAlchemy DB URL                                      | `sqlite:////app/data/lab_scheduler.db` |

## Extending

- **Calendar grid view**: the dashboard currently renders a sorted list/table
  (deliberately simple and dependency-free). If you'd like a visual month
  grid instead, FullCalendar.js can be dropped into `dashboard.html` via CDN
  and populated from a small `/api/bookings` JSON endpoint — happy to add
  that if useful.
- **Per-equipment filtering** on the dashboard is a natural next step if the
  list gets long.
- **Auto-deny on approve**: currently the admin manually denies the losing
  side of a conflict; this could be automated (approving one auto-denies all
  its conflicting pending bookings) if you'd rather not click twice.
