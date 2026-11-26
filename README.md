# ErrorLens for Python

Error tracking, performance tracing, logs and cron monitoring for Python apps, reporting to
[ErrorLens](https://errorlens.io) — the hosted service or your own server.

- **No dependencies** — only the Python standard library.
- **Private by default** — no IP addresses, cookies or request bodies unless you opt in, and passwords,
  tokens and keys are scrubbed before anything leaves your app.
- **Never in your way** — sending happens on a background thread with a bounded queue; your code never
  waits for the network.
- **Works with your stack** — Django, Flask, FastAPI/Starlette, Celery, httpx, requests, SQLAlchemy and
  `logging` are instrumented automatically.
- **Standard tracing** — W3C `traceparent`, so traces connect with OpenTelemetry services too.

## Install

```bash
pip install errorlens
```

Python 3.9 or newer.

## Set up

```python
import errorlens

errorlens.init(
    dsn="https://<key>@errorlens.io/<project-id>",  # from your project's page
    release="my-app@1.4.2",
    environment="production",
)
```

Or set `ERRORLENS_DSN`, `ERRORLENS_RELEASE` and `ERRORLENS_ENVIRONMENT` and call `errorlens.init()`.
Without a DSN the SDK does nothing, so it's safe in tests and local development.

Unhandled exceptions — in the main thread, threads, web requests and Celery tasks — are reported
automatically.

## Report errors and messages

```python
try:
    charge(order)
except PaymentError:
    errorlens.capture_exception()

errorlens.capture_message("Payment provider is slow", level="warning")
```

## Add context

```python
errorlens.set_user({"id": "42"})
errorlens.set_tag("region", "eu-west")
errorlens.set_context("order", {"id": 9817, "items": 3})
errorlens.add_breadcrumb(category="payment", message="Charging card")

with errorlens.new_scope() as scope:      # only for this block
    scope.set_tag("job", "nightly-sync")
    errorlens.capture_exception(err)
```

Inside web requests and tasks, context is isolated per request automatically.

## Performance

```python
errorlens.init(dsn="…", traces_sample_rate=0.2)   # record 20% of requests
```

Requests in Django, Flask and FastAPI, their SQL queries and outgoing HTTP calls are timed
automatically. Time your own work too:

```python
with errorlens.start_transaction("nightly-export", op="task"):
    with errorlens.start_span("db.query", "load orders"):
        orders = load()
```

## Logs

```python
errorlens.init(dsn="…", enable_logs=True)

import logging
logging.getLogger("checkout").warning("Inventory low for %s", sku)     # standard logging

errorlens.logger.info("Order {order_id} paid", order_id=42, amount=9.5)  # structured
```

## Cron monitors

```python
@errorlens.monitor("nightly-backup", schedule="0 2 * * *")
def backup():
    ...
```

ErrorLens alerts you when the job fails, runs too long, or doesn't run at all.

## Options

| Option | Default | |
|---|---|---|
| `dsn` | `$ERRORLENS_DSN` | Where to send data |
| `release`, `environment` | env vars / `production` | Shown on every event |
| `sample_rate` | `1.0` | Share of errors to send |
| `traces_sample_rate` / `traces_sampler` | off | Performance sampling |
| `enable_logs` | `False` | Send logs |
| `send_default_pii` | `False` | Include IP addresses, cookies, request bodies and user details |
| `scrub_fields` / `safe_fields` | | Extra field names to remove / never remove |
| `before_send`, `before_send_transaction`, `before_breadcrumb`, `before_send_log` | | Edit or drop data |
| `ignore_errors` | | Exception types or names never to report |
| `include_local_variables` | `True` | Local variables in your own frames |
| `in_app_include` / `in_app_exclude` / `project_root` | | Which frames are your code |
| `max_breadcrumbs` | `100` | |
| `trace_propagation_targets` | all | URL patterns that receive `traceparent` |
| `disabled_integrations` | | e.g. `["sqlalchemy"]` |

Full documentation: https://errorlens.io/docs/python/

## License

MIT
