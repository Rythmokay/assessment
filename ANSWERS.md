# Written answers

## Section 1

### Interpretation

The brief says no code changed in the view, so I built a scenario where the view is untouched but code
it depends on changed in the same deploy:

- `Order.total` went from a stored column to a `@property` that sums `self.items.all()`, to stop the
  stored total drifting from the line items.
- `OrderSerializer` started nesting `items` (with `product_name` via `source="product.name"`) for a new
  dashboard card.

The view still does `Order.objects.filter(customer=request.user)`. That queryset was fine when it only
had to render columns of `orders_order`, and it now silently drives two lazy relations per order.

Code:
- `orders/views.py`: `OrderSummaryBeforeFixView` (the regression, kept at `/api/orders/summary/before-fix/`
  so the comparison can be reproduced) and `OrderSummaryView` (fixed).
- `orders/models.py`, `orders/serializers.py`: the changed model property and serializer.
- `orders/tests.py`: query-count regression tests.

### Incident investigation log

1. **Confirm and scope the symptom.** Check APM / access logs for the endpoint: p50 vs p99 latency
   since the deploy, and whether it's all users or some. The brief already says it's users with more
   than 200 orders. That's the most important clue: if latency scales with *how much data a user has*,
   the problem is per-row work inside the request, not something global (DB down, network, pool
   exhaustion), which would hit everyone. The 30s figure is most likely the gunicorn/load-balancer
   timeout cutting off the request, so the real duration is unknown and possibly longer.

2. **Find what actually changed.** "No change to the view" doesn't mean no change on the code path. I'd
   diff the deploy for everything the view touches: serializers, models (properties, `Meta.ordering`,
   custom managers), middleware, settings (`CONN_MAX_AGE`, cache backend, `DEBUG`), and migrations.
   Here the diff shows `Order.total` becoming a property and `OrderSerializer` gaining nested items.
   Both are on the path; neither is in `views.py`.

3. **Reproduce with production-shaped data.** Seed a user with 250 orders x 4 items
   (`manage.py seed_orders`) and call the endpoint with django-silk enabled. Silk shows 1,753 queries
   for one request. Before the deploy, the same request needed a handful.

4. **Read the shape of the queries, not just the count.** The silk SQL tab shows two statements
   repeated with different parameters: `SELECT ... FROM orders_orderitem WHERE order_id = ?` (750 times)
   and `SELECT ... FROM orders_product WHERE id = ?` (1,000 times). Many tiny identical queries that
   each run in microseconds is the signature of an N+1, and silk's traceback for each query points at
   `Order.total` and the serializer's `product.name` source.

5. **Rule out the alternatives explicitly.**
   - Missing index: would show as a *few* slow queries with large scan times. Here every query is fast;
     there are just too many. The FK columns are indexed automatically by Django and the order query uses
     the `(customer, -created_at)` index.
   - Serializer overhead (CPU): would show high Python time with a low query count. Silk shows the
     opposite.
   - Cache invalidation: would affect all users after a deploy, not scale with order count.

6. **Fix, prove it, guard it.** Add `prefetch_related` with a `select_related` inner queryset, confirm
   in silk (4 queries), add tests that assert the query count is constant regardless of order count, so
   the next serializer change that adds a relation fails CI instead of production.

### Root cause: N+1 queries from lazy ORM relations

Per request the old code runs `1 + orders x (3 + items_per_order)` queries:

- 1 for the orders.
- Per order, `order.items.all()` three times: once in `build_summary` for `total_spent`, once for the
  serializer's `total` field, once for the nested `items` field. A related manager returns a *new*
  `QuerySet` on every `.all()`, and without a prefetch cache each one goes to the database. Nothing is
  shared between the three.
- Per item, `item.product.name` lazily loads the `Product` FK.

For 250 orders x 4 items that's `1 + 250 x 7 = 1,751`, plus 2 for the session and user = 1,753, which is
what silk recorded.

Why it went from 80ms to 30s+: locally on SQLite (in-process, no network) the request went from about 30ms to
about 900ms. In production every query is a network round trip to the database plus parse/plan time,
typically 1-5ms, and much more when the DB is busy serving the same pattern for many dashboard users at
once. 1,750 x ~15ms under load is ~26s. And because the count grows with orders, it crossed the 30s
timeout at around the 200-order mark.

### The fix and why it works

```python
orders = (
    Order.objects.filter(customer=request.user)
    .order_by("-created_at")
    .prefetch_related(
        Prefetch("items", queryset=OrderItem.objects.select_related("product"))
    )
)
```

- **`prefetch_related("items")`**: when the orders queryset is evaluated, Django runs the main query,
  collects the primary keys of all fetched orders, and runs *one* more query:
  `SELECT ... FROM orders_orderitem WHERE order_id IN (1, 2, ..., 250)`. It groups those rows by
  `order_id` in Python and stores each group in the order instance's `_prefetched_objects_cache["items"]`.
  From then on, `order.items.all()` returns that cached list without touching the database, so all three
  uses (both `total` calls and the nested serializer) are free. Note that `.filter()` or `.order_by()`
  on the related manager would bypass this cache and query again, so code on the hot path must use
  `.all()`.
- **`Prefetch(..., queryset=OrderItem.objects.select_related("product"))`**: the inner queryset turns
  that single items query into `orders_orderitem INNER JOIN orders_product`. Django builds the `Product`
  from the joined columns and stores it in each item's field cache, so `item.product.name` is an
  attribute read, not a query.
- **Why not `select_related("items")`?** `select_related` only follows single-valued relations (FK,
  one-to-one) because it works by joining. Joining a one-to-many would multiply the order rows, so
  Django forbids it. The reverse FK needs `prefetch_related`'s separate `IN` query.
- **At the database level** it's now two queries regardless of order count: one index range scan on
  `(customer_id, created_at)` for orders, and one lookup on the `order_id` FK index for items, joined to
  products by primary key. The work moved from 1,750 round trips to two round trips that return the
  same rows.

Result (from `docs/profiling/silk_report.md`, seeded user with 250 orders x 4 items):

| | SQL queries | Time in SQL | Total time |
|---|---:|---:|---:|
| Before fix | 1,753 | 127 ms | 906 ms |
| After fix | 4 | 2 ms | 32 ms |

(The 4 includes the session and user lookups done by the auth middleware.)

What the fix doesn't address: it still loads every order and item into memory. For a user with 20,000
orders that's a lot of objects and a very long `IN` list. The proper next step is to paginate the order
list and compute `total_spent` in the database with
`OrderItem.objects.filter(order__customer=user).aggregate(Sum(F("quantity") * F("unit_price")))`. I kept
the response shape unchanged because the mobile app depends on it.

### Profiler evidence

django-silk is installed (`silk.middleware.SilkyMiddleware`, UI at `/silk/`). `python manage.py
profile_summary` sends one request to each endpoint through the full middleware stack and writes silk's
recorded numbers and the top repeated statements to
[docs/profiling/silk_report.md](docs/profiling/silk_report.md). The tests in `orders/tests.py` assert the
same thing with `CaptureQueriesContext`: the fixed view runs 2 queries for 5 orders and for 60 orders;
the old one runs `1 + 20 x 6` for 20 orders.

## Section 2

Design and trade-offs are in [DESIGN.md](DESIGN.md). Test: `notifications/tests/test_queue.py` submits
500 jobs to a real worker (rate limit scaled to 50/second so the test takes ~20s instead of ~3 minutes)
and asserts every job is delivered exactly once or dead-lettered, that no 1-second window saw more than
50 provider calls (with a 50ms tolerance, explained in DESIGN.md under "Headroom"), that `fail-once` jobs were retried after a backoff and then succeeded, and that
`fail-always` jobs hit the dead-letter list after exactly `EMAIL_MAX_ATTEMPTS` attempts.
`test_rate_limiter.py` hits the limiter from 16 threads and checks exactly `limit` grants.

### What happens to in-flight tasks if the worker is SIGKILLed?

**With Celery defaults, they're lost.** `task_acks_late` is `False` by default, so the worker acks a
message as soon as it starts executing it. SIGKILL can't be caught, so the task dies mid-run, the message
is already gone from Redis, and nobody ever retries it.

**How this implementation handles it:**

1. **`task_acks_late=True`** (globally, and `acks_late=True` on the task). The ack happens only after the
   task function returns or the retry message has been published. With the Redis transport, a fetched
   but unacked message is moved into the `unacked` hash, with its delivery time in the `unacked_index`
   sorted set. If the worker is killed, that entry just stays there.

2. **`visibility_timeout=900`** in `broker_transport_options`. kombu's Redis transport periodically runs
   `restore_visible`, which moves messages whose `unacked_index` score is older than the visibility
   timeout back onto the queue. So after a SIGKILL of the whole worker, every message it held (the one
   running, plus any it had reserved with a future ETA) is redelivered to another worker within 15
   minutes. On a clean restart of the same worker, the same mechanism picks them up.

   The value is a trade-off. Lower means faster recovery after a crash, but it must stay above the
   longest countdown a task can wait for (300s backoff cap), or kombu redelivers ETA tasks that are still
   legitimately waiting and they get sent twice.

3. **`task_reject_on_worker_lost=True`**. If only a pool child process is killed (e.g. the OOM killer
   SIGKILLs one prefork child), the main worker process survives and sees `WorkerLostError`. By default
   with `acks_late` it would mark the task failed *and ack it*, losing it. With this setting it rejects
   with requeue, so the message goes straight back to the queue without waiting for the visibility
   timeout. (With the threads pool, a SIGKILL takes the whole process, so it's always case 2.)

4. **`worker_prefetch_multiplier=1`** keeps the number of messages a single crashed worker can strand
   small.

5. **Duplicates.** All of the above makes delivery at-least-once, so a task can run twice: if the worker
   is killed after the provider accepted the email but before the ack, the redelivered task would send
   it again. The task checks `email:sent:<job_id>` before sending and sets it right after a successful
   send, which covers the common case. The remaining window (killed between the provider's response and
   the `SET`) is covered only if the provider honours the `idempotency_key` we pass, which is the job
   id. Without that, a rare duplicate email is the price of never losing one, and for transactional email
   that's the right side to err on.

What it doesn't cover: if Redis itself loses data (crash with only RDB snapshots), messages are gone
regardless of acks. That needs AOF persistence or a database outbox (see DESIGN.md).

## Section 3

I answered A and B.

### A: Django admin with 500,000+ rows

**1. `COUNT(*)` for pagination.** `ChangeList` calls `paginator.count` on every page load, which runs
`SELECT COUNT(*)` over the filtered queryset. On PostgreSQL that's a full scan, because MVCC means there's
no stored row count. With a filter or search active, the admin runs a *second* count for the unfiltered
total ("3 of 500,000"). Fix: set `show_full_result_count = False` on the `ModelAdmin` to drop the second
count, and set `paginator` to a custom `Paginator` subclass whose `count` returns the planner estimate
from `pg_class.reltuples` when no filters are applied. Trade-off: the page count is approximate, so
"last page" may be slightly off.

**2. N+1 queries in `list_display`.** Every FK in `list_display`, or a `__str__` that touches a related
object, triggers one query per row. 100 rows per page x 3 FKs is 300 queries. Fix:
`list_select_related = ("customer", "product")` so `ChangeList` joins them. For computed columns over
reverse relations (e.g. number of items), override `get_queryset()` to `.annotate(item_count=Count("items"))`
and expose it with `@admin.display(ordering="item_count")`. Trade-off: the annotation makes the main query
heavier, and `list_select_related` only helps forward FKs.

**3. Search, filtering and ordering that can't use an index.** `search_fields = ["name"]` generates
`UPPER(name) LIKE UPPER('%term%')` for every field, which no B-tree index can serve, so every search is a
sequential scan. `list_filter` on a FK uses `RelatedFieldListFilter`, which loads every related object to
render the sidebar. And `ordering` on an unindexed column forces a sort of the full table before `LIMIT`.
Fixes: use prefixes in `search_fields` (`"=email"` for exact, `"^name"` for `istartswith`) so indexes
apply, or add a `GinIndex` with `gin_trgm_ops` if substring search is really needed. Replace FK filters
with `RelatedOnlyFieldListFilter`. Make `ordering` match a column in `Meta.indexes`. On the change form,
use `raw_id_fields` or `autocomplete_fields` so FK dropdowns don't render 500,000 `<option>`s.
Trade-off: prefix search finds less than substring search, and trigram indexes slow writes and use disk.

### B: Offset vs cursor pagination

**Offset (`LIMIT 25 OFFSET 5000`, DRF `LimitOffsetPagination` / `PageNumberPagination`).** The database
can't jump to row 5,000; it has to produce and discard the first 5,000 rows in sort order, even when it
walks an index. Cost grows linearly with page depth, so deep pages get slower, and the usual
`COUNT(*)` for the total adds a scan per request. On mutation it breaks infinite scroll: if 3 new records
are inserted at the top while the user is on page 2, page 3 starts 3 rows earlier and the user sees
duplicates; deletions shift the other way and rows are silently skipped. What you get in return: total
counts, "jump to page 40", and trivially simple implementation with any ordering.

**Cursor (DRF `CursorPagination`).** The client sends an opaque cursor that encodes the last position,
and the query becomes `WHERE (created_at, id) < (:last_created_at, :last_id) ORDER BY created_at DESC, id
DESC LIMIT 25`. With a composite index on those columns this is an index seek: page 400 costs the same as
page 1. It's stable under inserts and deletes because the position is a value, not a row number, so new
rows at the top don't shift what comes next. Costs: no total count and no jumping to an arbitrary page;
the ordering must be on a unique, immutable column (or have a tie-breaker), and
every sort option the API offers needs its own index.

**When to choose which.** For the mobile app's infinite scroll, cursor, without much debate: users only
ever go "next", the feed changes while they scroll, and deep scrolls shouldn't get slower. For an internal
or admin table where people need page numbers, a total, and sortable columns, and the data is in the
thousands rather than millions, offset is simpler and the downsides don't show up. For this 10,000-record
endpoint, offset would work today, but the consumer is the one that decides: if it's a feed, I'd use
cursor from the start, because switching later is a breaking API change.
