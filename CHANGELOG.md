# Changelog

## 1.0.1

- Each exception is reported once, even when a framework hook, `logger.exception()` and your own code all see it (Flask errors were reported twice).
- WSGI requests (Flask, Django, any WSGI app) finish their transaction once the response has been sent, also with servers and test clients that never call `close()`.

## 1.0.0

- First release: errors with local variables, breadcrumbs, scopes, tracing with W3C `traceparent`, structured logs, cron monitors and secret scrubbing. Django, Flask, FastAPI/Starlette, Celery, SQLAlchemy, httpx, `logging` and the standard library's HTTP client are detected automatically. No dependencies.
