"""Gunicorn production configuration.

Values are environment-overridable. Worker count defaults to the common
(2 * CPU) + 1 heuristic; tune per instance size.
"""

import multiprocessing
import os

bind = os.environ.get("GUNICORN_BIND", "0.0.0.0:8000")
workers = int(os.environ.get("GUNICORN_WORKERS", (multiprocessing.cpu_count() * 2) + 1))
threads = int(os.environ.get("GUNICORN_THREADS", 2))
timeout = int(os.environ.get("GUNICORN_TIMEOUT", 30))
graceful_timeout = int(os.environ.get("GUNICORN_GRACEFUL_TIMEOUT", 30))
keepalive = int(os.environ.get("GUNICORN_KEEPALIVE", 5))
max_requests = int(os.environ.get("GUNICORN_MAX_REQUESTS", 1000))
max_requests_jitter = int(os.environ.get("GUNICORN_MAX_REQUESTS_JITTER", 100))

# Log to stdout/stderr so the platform captures and rotates logs.
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")
