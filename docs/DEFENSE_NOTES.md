# Defense notes

Short notes per phase so I can explain every decision in the call. Format: what I built, why,
what the alternative was, what breaks at scale.

## Phase 1: skeleton and deploy setup

- **Settings split into base / dev / test / prod.** `base.py` holds what is shared; `prod.py`
  refuses to boot if `SECRET_KEY` is missing or a placeholder, if `ALLOWED_HOSTS` is empty, or if
  any bucket variable is missing (`config/settings/base.py` `s3_storage_options`). That is the
  answer to "graceful handling of missing config": fail at boot with a clear message, never fall
  back to local disk or SQLite. Tested in `config/tests/test_settings.py`.
- **Prod never reads `.env`.** Only dev/test load a local `.env`; prod relies on Railway variables.
  A stray `.env` in the build cannot silently configure production.
- **DB connections:** `CONN_MAX_AGE=60` reuses connections, `CONN_HEALTH_CHECKS=True` pings a
  reused connection before handing it to a request, so a connection Supabase dropped while idle
  is replaced instead of causing a 500. If the DB drops *mid*-request, the transaction rolls back
  and the user gets the clean 500 page; `/healthz` reports 503 so Railway sees the outage.
- **Railway + Supabase instead of Render + Neon:** one place for data (Postgres and S3-compatible
  storage both in Supabase), one place for compute. `railway.toml` runs `collectstatic` at build,
  `migrate` as a pre-deploy step (runs once, not per worker), gunicorn with 2 workers, and
  `/healthz` as the deploy health check so a broken build never takes traffic.
- **DB cache instead of locmem:** gunicorn runs 2 worker processes; a per-process cache would make
  the assistant rate limit 2x as loose. At 10x traffic I would switch to Redis.
