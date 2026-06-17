"""SQLite database for storing mitmproxy flows."""

import base64
import contextlib
import json
import sqlite3
from collections.abc import Generator
from pathlib import Path
from typing import Any

import jsbeautifier
from lxml import etree

# Content types to exclude from response body storage
EXCLUDED_CONTENT_TYPES = {
    "font/",
    "video/",
    "audio/",
    "image/",
}

MSG_ONLY_SELECT_ALLOWED = "Only SELECT queries are allowed"

MAX_FILENAME_PART_LENGTH = 64

CONTENT_TYPE_FORMATTERS: dict[str, str] = {
    "application/json": "json",
    "application/javascript": "js",
    "text/javascript": "js",
    "text/html": "html",
}

DATA_DIR: Path = Path.cwd() / ".odda"

# Map MIME types to file extensions
_MIME_TO_EXT: dict[str, str] = {
    "application/json": ".json",
    "application/javascript": ".js",
    "text/javascript": ".js",
    "application/x-javascript": ".js",
    "text/html": ".html",
    "application/xhtml+xml": ".xhtml",
    "text/css": ".css",
    "text/xml": ".xml",
    "application/xml": ".xml",
    "application/atom+xml": ".xml",
    "application/rss+xml": ".xml",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "text/tab-separated-values": ".tsv",
    "application/x-yaml": ".yaml",
    "text/yaml": ".yaml",
    "application/yaml": ".yaml",
    "image/svg+xml": ".svg",
    "application/pdf": ".pdf",
    "application/zip": ".zip",
    "application/gzip": ".gz",
    "application/x-gzip": ".gz",
    "application/x-tar": ".tar",
    "application/octet-stream": ".bin",
    "application/x-protobuf": ".pb",
    "application/grpc": ".grpc",
    "application/grpc+proto": ".grpc",
    "application/grpc+json": ".grpc",
    "application/x-www-form-urlencoded": ".txt",
    "multipart/form-data": ".txt",
    "application/msgpack": ".msgpack",
    "application/x-msgpack": ".msgpack",
    "application/bson": ".bson",
    "application/wasm": ".wasm",
    "application/x-font-ttf": ".ttf",
    "application/x-font-otf": ".otf",
    "application/font-woff": ".woff",
    "application/font-woff2": ".woff2",
    "font/ttf": ".ttf",
    "font/otf": ".otf",
    "font/woff": ".woff",
    "font/woff2": ".woff2",
    "text/markdown": ".md",
    "text/calendar": ".ics",
    "text/vcard": ".vcf",
    "application/ld+json": ".jsonld",
    "application/schema+json": ".json",
    "application/manifest+json": ".json",
    "application/x-ndjson": ".ndjson",
    "application/vnd.api+json": ".json",
}


def set_data_dir(path: Path | str) -> None:
    """Set the directory used for the database and response bodies.

    Args:
        path: Directory path. Created if it does not exist.
    """
    global DATA_DIR
    DATA_DIR = Path(path)
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _beautify_body(body: bytes, content_type: str | None) -> bytes:
    """Beautify body bytes based on content type.

    Args:
        body: Raw body bytes.
        content_type: MIME type from Content-Type header.

    Returns:
        Beautified body bytes, or original body if unknown format or
        beautification fails.
    """
    if not content_type or not body:
        return body

    mime = content_type.split(";")[0].strip().lower()
    fmt = CONTENT_TYPE_FORMATTERS.get(mime)

    if fmt is None:
        return body

    try:
        if fmt == "json":
            return json.dumps(json.loads(body), indent=2, ensure_ascii=False).encode(
                "utf-8"
            )
        if fmt == "js":
            return jsbeautifier.beautify(body.decode("utf-8")).encode("utf-8")
        if fmt == "html":
            return etree.tostring(
                etree.HTML(body), pretty_print=True, encoding="unicode"
            ).encode("utf-8")
    except Exception:
        return body

    return body


def should_store_body(content_type: str | None) -> bool:
    """Check if body should be stored based on content type.

    Args:
        content_type: MIME type of the content.

    Returns:
        True if body should be stored, False otherwise.
    """
    if not content_type:
        return True
    return not any(content_type.startswith(prefix) for prefix in EXCLUDED_CONTENT_TYPES)


def get_db_path() -> Path:
    """Get the database file path.

    Returns:
        Path to the SQLite database file, under ``.odda/``.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / "flows.db"


def get_bodies_dir() -> Path:
    """Get the directory where response body files are stored.

    Returns:
        Path to ``.odda/bodies/``.
    """
    bodies_dir = DATA_DIR / "bodies"
    bodies_dir.mkdir(parents=True, exist_ok=True)
    return bodies_dir


def _mime_to_ext(content_type: str | None) -> str:
    """Map a MIME type to a file extension.

    Args:
        content_type: MIME type string (e.g. ``application/json``).

    Returns:
        File extension including leading dot, or empty string for unknown types.
    """
    if not content_type:
        return ""
    mime = content_type.split(";")[0].strip().lower()
    return _MIME_TO_EXT.get(mime, "")


def _sanitize_filename_part(s: str) -> str:
    """Sanitize a string for use in a filename.

    Replaces any character that is not alphanumeric, dot, hyphen, or
    underscore with an underscore. Collapses consecutive underscores.
    Truncates to a maximum length.

    Args:
        s: Raw string to sanitize.

    Returns:
        Safe filename fragment.
    """
    result = []
    for ch in s:
        if ch.isalnum() or ch in (".", "-", "_"):
            result.append(ch)
        else:
            result.append("_")

    sanitized = "".join(result)

    # Collapse consecutive underscores
    while "__" in sanitized:
        sanitized = sanitized.replace("__", "_")

    # Remove leading/trailing underscores
    sanitized = sanitized.strip("_")

    # Truncate to max length
    if len(sanitized) > MAX_FILENAME_PART_LENGTH:
        sanitized = sanitized[:MAX_FILENAME_PART_LENGTH]

    return sanitized or "unknown"


def _build_body_filename(
    flow_id: int, method: str, host: str, content_type: str | None
) -> str:
    """Build a filename for a response body file.

    Args:
        flow_id: Database row ID of the flow.
        method: HTTP method (e.g. GET).
        host: Target hostname.
        content_type: Response Content-Type header.

    Returns:
        Filename like ``00042_get_example_com.json``.
    """
    safe_host = _sanitize_filename_part(host) or "unknown"
    ext = _mime_to_ext(content_type)
    return f"{flow_id:05d}_{method.lower()}_{safe_host}{ext}"


def init_database(db_path: Path) -> sqlite3.Connection:
    """Initialize the database with schema.

    Args:
        db_path: Path to the database file.

    Returns:
        SQLite connection object.
    """
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA auto_vacuum=FULL")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS flows_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            method TEXT NOT NULL,
            scheme TEXT,
            host TEXT NOT NULL,
            port INTEGER,
            path TEXT,
            url TEXT NOT NULL,
            status_code INTEGER,
            reason TEXT,
            request_headers TEXT,
            response_headers TEXT,
            request_body BLOB,
            response_body_path TEXT,
            request_start REAL,
            response_start REAL,
            response_end REAL,
            total_duration_ms REAL
        )
        """
    )

    # Create a sorted view so agents can query `flows` and get newest first.
    conn.execute(
        """
        CREATE VIEW IF NOT EXISTS flows AS
        SELECT * FROM flows_data ORDER BY id DESC
        """
    )

    # Create indexes
    conn.execute("CREATE INDEX IF NOT EXISTS idx_host ON flows_data(host)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_method ON flows_data(method)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON flows_data(status_code)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_url ON flows_data(url)")

    conn.commit()

    # Ensure the bodies directory exists
    get_bodies_dir()

    return conn


class DatabaseAddon:
    """Mitmproxy addon that stores flows in SQLite database."""

    def __init__(self) -> None:
        """Initialize the addon with database connection."""
        self.db_path = get_db_path()
        self.conn = init_database(self.db_path)
        # flow.id -> database row id
        self._pending_flows: dict[int, int] = {}

    def request(self, flow) -> None:
        """Handle request - store request data immediately in database.

        Args:
            flow: mitmproxy flow object.
        """
        request = flow.request
        request_start = request.timestamp_start

        # Store request headers as JSON
        request_headers = dict(request.headers.items())

        # Always store request body if present
        request_body = request.content or None

        # Insert request into database immediately
        cursor = self.conn.execute(
            """
            INSERT INTO flows_data (
                method, scheme, host, port, path, url,
                status_code, reason,
                request_headers, response_headers,
                request_body, response_body_path,
                request_start, response_start, response_end, total_duration_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request.method,
                request.scheme,
                request.host,
                request.port,
                request.path,
                request.url,
                None,  # status_code - will be updated on response
                None,  # reason - will be updated on response
                json.dumps(request_headers),
                None,  # response_headers - will be updated on response
                request_body,
                None,  # response_body_path - will be updated on response
                request_start,
                None,  # response_start - will be updated on response
                None,  # response_end - will be updated on response
                None,  # total_duration_ms - will be updated on response
            ),
        )
        self.conn.commit()

        # Store the database row id to update later
        self._pending_flows[flow.id] = cursor.lastrowid

    def response(self, flow) -> None:
        """Handle response - update the existing row with response data.

        Args:
            flow: mitmproxy flow object.
        """
        if flow.id not in self._pending_flows:
            return

        row_id = self._pending_flows[flow.id]
        response = flow.response

        # Use mitmproxy's built-in timestamps
        request_start = flow.request.timestamp_start
        response_start = response.timestamp_start
        response_end = response.timestamp_end

        # Calculate duration
        if response_end:
            total_duration_ms = (response_end - request_start) * 1000
        else:
            total_duration_ms = None

        # Store response headers as JSON
        response_headers = dict(response.headers.items())

        # Get content type and decide whether to store body
        content_type = response.headers.get("Content-Type", "")
        response_body_path = None
        if should_store_body(content_type) and response.content:
            with contextlib.suppress(Exception):
                body_bytes = _beautify_body(response.content, content_type)
                filename = _build_body_filename(
                    row_id, flow.request.method, flow.request.host, content_type
                )
                filepath = get_bodies_dir() / filename
                filepath.write_bytes(body_bytes)
                response_body_path = str(filepath)

        # Update the existing row with response data
        self.conn.execute(
            """
            UPDATE flows_data SET
                status_code = ?,
                reason = ?,
                response_headers = ?,
                response_body_path = ?,
                response_start = ?,
                response_end = ?,
                total_duration_ms = ?
            WHERE id = ?
            """,
            (
                response.status_code,
                response.reason,
                json.dumps(response_headers),
                response_body_path,
                response_start,
                response_end,
                total_duration_ms,
                row_id,
            ),
        )
        self.conn.commit()

        # Clean up memory
        del self._pending_flows[flow.id]

    def done(self) -> None:
        """Clean up when addon is done."""
        if hasattr(self, "conn"):
            self.conn.close()


class FlowStore:
    """Interface for querying stored flows."""

    def __init__(self, db_path: Path | None = None) -> None:
        """Initialize with database path.

        Args:
            db_path: Path to database. Uses default if not provided.
        """
        self.db_path = db_path or get_db_path()

    @contextlib.contextmanager
    def _get_readonly_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager for read-only database connection.

        Yields:
            SQLite connection in read-only mode.

        Example:
            with self._get_readonly_connection() as conn:
                cursor = conn.execute("SELECT * FROM flows_data")
        """
        # URI mode for read-only access
        uri = f"file:{self.db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            yield conn
        finally:
            conn.close()

    def get_latest(self, n: int = 10) -> list[dict[str, Any]]:
        """Get latest N flows (summary only).

        Args:
            n: Number of flows to return.

        Returns:
            List of flow summaries.
        """
        with self._get_readonly_connection() as conn:
            cursor = conn.execute(
                """
                SELECT id, method, host, path, status_code, total_duration_ms
                FROM flows
                LIMIT ?
                """,
                (n,),
            )

            return [
                {
                    "id": row[0],
                    "method": row[1],
                    "host": row[2],
                    "path": row[3],
                    "status_code": row[4],
                    "total_duration_ms": row[5],
                }
                for row in cursor.fetchall()
            ]

    def _row_to_dict(self, row: tuple, columns: list[str]) -> dict[str, Any]:
        """Convert a database row to a dictionary with processed values.

        Args:
            row: Raw database row tuple.
            columns: Column names matching the row.

        Returns:
            Dictionary with parsed headers, decoded request bodies, and
            response body file path.
        """
        result: dict[str, Any] = {}
        for i, col in enumerate(columns):
            value = row[i]
            if col in ("request_headers", "response_headers") and value:
                with contextlib.suppress(json.JSONDecodeError):
                    value = json.loads(value)
            elif col == "request_body" and value:
                try:
                    value = value.decode("utf-8")
                except UnicodeDecodeError:
                    value = base64.b64encode(value).decode("ascii")
            result[col] = value
        return result

    def get_by_id(self, flow_id: int) -> dict[str, Any] | None:
        """Get full flow details by ID.

        Args:
            flow_id: Flow ID.

        Returns:
            Full flow details or None if not found.
        """
        with self._get_readonly_connection() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM flows WHERE id = ?
                """,
                (flow_id,),
            )

            row = cursor.fetchone()
            if not row:
                return None

            columns = [description[0] for description in cursor.description]
            return self._row_to_dict(row, columns)

    def execute_query(self, sql: str) -> list[dict[str, Any]]:
        """Execute a SELECT query (read-only).

        Args:
            sql: SQL SELECT statement.

        Returns:
            List of result rows as dictionaries.

        Raises:
            ValueError: If query is not a SELECT statement.
        """
        # Basic check that it's a SELECT
        trimmed = sql.strip().upper()
        if not trimmed.startswith("SELECT"):
            raise ValueError(MSG_ONLY_SELECT_ALLOWED)

        with self._get_readonly_connection() as conn:
            cursor = conn.execute(sql)

            columns = [description[0] for description in cursor.description]
            return [self._row_to_dict(row, columns) for row in cursor.fetchall()]
