"""JSON-RPC protocol helpers."""

from __future__ import annotations

import json
from typing import Any


class JsonRpcError(Exception):
    """Exception that carries a JSON-RPC error code and message."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        """Initialize JSON-RPC error.

        Args:
            code: JSON-RPC error code.
            message: Error message.
            data: Optional extra error data.
        """
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def parse_request(line: str) -> dict[str, Any]:
    """Parse a single JSON-RPC request line.

    Args:
        line: Raw JSON line.

    Returns:
        Parsed request dict.

    Raises:
        JsonRpcError: If the line is not valid JSON-RPC.
    """
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        raise JsonRpcError(PARSE_ERROR, f"Parse error: {exc}") from exc

    if not isinstance(payload, dict):
        raise JsonRpcError(INVALID_REQUEST, "Invalid Request: expected object")

    if payload.get("jsonrpc") != "2.0":
        raise JsonRpcError(INVALID_REQUEST, "Invalid Request: jsonrpc must be 2.0")

    if "method" not in payload:
        raise JsonRpcError(INVALID_REQUEST, "Invalid Request: missing method")

    return payload


def build_response(request_id: Any, result: Any) -> dict[str, Any]:
    """Build a JSON-RPC success response.

    Args:
        request_id: Request id from the client.
        result: Method result.

    Returns:
        JSON-RPC response dict.
    """
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def build_error(
    request_id: Any, code: int, message: str, data: Any = None
) -> dict[str, Any]:
    """Build a JSON-RPC error response.

    Args:
        request_id: Request id from the client (may be None).
        code: Error code.
        message: Error message.
        data: Optional extra error data.

    Returns:
        JSON-RPC error response dict.
    """
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def encode(message: dict[str, Any]) -> bytes:
    """Encode a JSON-RPC message to bytes with a trailing newline."""
    return json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n"
