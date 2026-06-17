"""Shared utility functions."""

import socket


def is_port_available(host: str, port: int) -> bool:
    """Check if a port is available for binding.

    Args:
        host: Host address to bind to.
        port: Port number to check.

    Returns:
        True if port is available, False otherwise.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
        except OSError:
            return False
        else:
            return True


def find_available_port(host: str, port: int) -> int:
    """Find an available port, starting from the given port.

    Args:
        host: Host address to check.
        port: Port number to start searching from.

    Returns:
        An available port number.

    Raises:
        RuntimeError: If no ports are available up to 65535.
    """
    while not is_port_available(host, port):
        port += 1
        if port > 65535:  # noqa: PLR2004
            msg = "No available ports found in range"
            raise RuntimeError(msg)
    return port
