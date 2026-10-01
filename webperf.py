"""Response-size and caching helpers for the Flask app.

- Static files get a content fingerprint in their URL (?v=<hash>), so
  browsers can cache them for a year and still pick up every change the
  moment a file's content does.
- Text responses (pages, JSON, CSS, JS, SVG) are gzip-compressed when the
  browser accepts it. Compressed static files are kept in memory, keyed by
  path and modification time, so each one is compressed once per deploy.
"""

import gzip
import hashlib
import os
import threading

STATIC_MAX_AGE = 365 * 24 * 3600
MIN_COMPRESS_BYTES = 1024
COMPRESSIBLE = ("text/", "application/json", "application/javascript", "image/svg+xml")

_versions = {}
_gzipped_static = {}
_lock = threading.Lock()


def _static_version(static_folder, filename):
    path = os.path.join(static_folder, filename)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    key = (path, mtime)
    with _lock:
        if key in _versions:
            return _versions[key]
    with open(path, "rb") as f:
        version = hashlib.md5(f.read()).hexdigest()[:10]
    with _lock:
        _versions[key] = version
    return version


def _compressible(response):
    return (response.status_code == 200
            and response.mimetype and response.mimetype.startswith(COMPRESSIBLE)
            and "Content-Encoding" not in response.headers)


def init_app(app):
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = STATIC_MAX_AGE

    @app.url_defaults
    def add_static_version(endpoint, values):
        if endpoint == "static" and "filename" in values and "v" not in values:
            version = _static_version(app.static_folder, values["filename"])
            if version:
                values["v"] = version

    @app.after_request
    def compress(response):
        from flask import request
        if "gzip" not in (request.headers.get("Accept-Encoding") or "").lower() or not _compressible(response):
            return response
        static_key = None
        if response.direct_passthrough:
            # send_file streams static files; read them once and keep the
            # compressed copy for this path/version
            if request.endpoint != "static":
                return response
            path = os.path.join(app.static_folder, request.view_args.get("filename", ""))
            try:
                static_key = (path, os.path.getmtime(path))
            except OSError:
                return response
            with _lock:
                cached = _gzipped_static.get(static_key)
            if cached is None:
                response.direct_passthrough = False
                data = response.get_data()
                cached = gzip.compress(data, 6) if len(data) >= MIN_COMPRESS_BYTES else b""
                with _lock:
                    _gzipped_static[static_key] = cached
            if not cached:
                return response
            original = response.response
            response.direct_passthrough = False
            response.set_data(cached)
            if hasattr(original, "close"):
                original.close()
        else:
            data = response.get_data()
            if len(data) < MIN_COMPRESS_BYTES:
                return response
            response.set_data(gzip.compress(data, 6))
        response.headers["Content-Encoding"] = "gzip"
        response.headers["Content-Length"] = str(len(response.get_data()))
        response.vary.add("Accept-Encoding")
        return response
