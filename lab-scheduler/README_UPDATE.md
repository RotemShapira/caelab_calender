# Update: Delete Denied Bookings (Manual + Automatic after 30 Days)

## What changed
1. **New file** `app/cleanup.py` — deletes denied bookings whose
   `updated_at` (the time they were denied) is older than
   `DENIED_RETENTION_DAYS` (default 30, configurable via env var).
   Runs automatically once at startup, then once every 24h, for the
   life of the running container — no cron/systemd needed.
2. **`app/main.py`**:
   - New route `POST /admin/bookings/{id}/delete` — permanently deletes
     a booking. Restricted to bookings already in `denied` status as a
     safety guard, so it can't be used to accidentally wipe a live
     pending/approved request (you must deny it first).
   - New startup hook that kicks off the daily background purge loop.
3. **`app/templates/admin.html`** — added a "Delete" button next to each
   row in the "Recently Denied" table (with a JS confirm prompt), plus a
   note that denied bookings are auto-purged after 30 days.
4. **`docker-compose.yml` / `.env.example`** — documented the new
   `DENIED_RETENTION_DAYS` env var.

No database schema changes — this update does NOT touch the SQLite
schema in any way that needs a migration script. It only ever deletes
rows that are already in `denied` status, so there's no risk to active
scheduling data.

## Deploying this update

1. Copy the changed/new files into your project (paths relative to the
   `lab-scheduler/` folder that has `docker-compose.yml`):
   ```
   app/cleanup.py            (new)
   app/main.py                (overwrite)
   app/templates/admin.html   (overwrite)
   ```
   Or apply the diffs directly from that folder:
   ```bash
   cd /mnt/Rotem/Rotem_Data/LAB_Schedual/lab-scheduler
   patch -p1 < main.py.diff
   patch -p1 < admin.html.diff
   ```

2. (Optional) Change the retention window in `docker-compose.yml`:
   ```yaml
   environment:
     DENIED_RETENTION_DAYS: "30"
   ```

3. Rebuild and restart:
   ```bash
   sudo docker compose down
   sudo docker compose up -d --build
   ```

4. Verify:
   - Go to `/admin` — the "Recently Denied" table now has a "Delete"
     button (with a confirmation prompt) next to "Re-approve".
   - Deny a booking, click Delete, confirm — it disappears immediately.
   - Trying to delete a still-pending or approved booking directly (e.g.
     via curl) returns a 400 error telling you to deny it first — this
     is intentional and protects your live data.
   - Check `sudo docker logs lab-scheduler` after 24h (or after a
     restart) for a line like `Purged N denied booking(s) older than 30
     days.` if any were old enough — nothing logs if there's nothing to
     purge.

## Note on this diff's baseline
This diff was generated against the exact `main.py` / `admin.html` you
uploaded earlier (before the Google Calendar holiday-overlay update). If
you've already applied that update, just copy these files in on top of
it as normal — the two updates touch different, non-overlapping parts
of `main.py`, so there's no conflict either way.
