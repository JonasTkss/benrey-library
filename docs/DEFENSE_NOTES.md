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

## Phase 2: data model and roles

- **Four models, FK + M2M + image:** `Book.authors` is M2M, `Loan` has FKs to `Book` and `Member`
  with `on_delete=PROTECT` (loan history must never vanish when someone deletes a book; withdraw
  it with `is_withdrawn` instead). `library/models.py`.
- **Constraints live in Postgres, not only in Python:** due date after loan date, `returned_at`
  set if and only if status is RETURNED, one active loan per book+member (a *partial* unique
  constraint, `WHERE status='active'`), case-insensitive unique email (`Lower("email")`).
  Even a bad shell script or bulk insert cannot break them. Tested in `test_models.py`.
- **Nothing derived is stored:** `copies_available`, `is_overdue`, `days_overdue`, `late_fee`
  are computed. A stored counter drifts the first time someone edits a row by hand. "Overdue" is
  not a status either; it is `ACTIVE and due_date < today`, so no nightly job is needed. The
  admin list uses `Book.objects.with_availability()`, one `COUNT ... FILTER` annotation instead
  of one query per row.
- **Indexes for the queries the admin actually runs:** `(status, due_date)` for overdue lists,
  `(member, status)` for "this member's open loans", `(book, status)` for availability.
- **Roles are a data migration (`0002_role_groups.py`), so every environment has them.** They are
  plain Django permissions, which the admin already enforces (buttons, actions, URLs, read-only
  forms). The one custom permission is `change_member_status` (suspend/reactivate), because
  "can edit a member" and "can suspend a member" are different jobs.
- **At 2x/10x books:** the availability `COUNT` per changelist page is bounded by the page size
  (100 rows), so it scales with the page, not the table. Admin search (`icontains` on title)
  becomes a sequential scan first; the fix is a `pg_trgm` GIN index.

## Phase 3: services (business logic)

- **All loan state changes go through `library/services.py`:** `checkout_book`, `return_loan`,
  `extend_loan`. The admin form, the inline and the bulk actions all call these, so each rule
  exists in one place. Admin classes contain no business rules.
- **Race on the last copy:** checkout runs in `transaction.atomic()` and locks the book row, then
  the member row, with `SELECT ... FOR UPDATE`. The second librarian waits, then sees 0 free
  copies. Same lock order everywhere means no deadlock. Real two-thread test against Postgres:
  `test_two_concurrent_checkouts_of_last_copy_only_one_wins`.
- **`borrowing_rule_violations()` returns *all* problems** (suspended + no copies + cap), so the
  librarian sees every reason at once instead of fixing one error at a time.
- **Bulk operations are per-row transactions on purpose** (`apply_to_each`): in a 500-loan
  "mark returned", one already-returned loan should be reported, not roll back the other 499.
- **Alternative I skipped:** a state-machine library (django-fsm). Two states and one transition
  do not need a dependency.
