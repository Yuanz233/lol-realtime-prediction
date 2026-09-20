"""Shared verified TLS context with a portable CA bundle when available."""
import ssl

try:
    import certifi
except ImportError:  # Keep offline/unit-test use possible before dependencies are installed.
    certifi = None


TLS_CONTEXT = ssl.create_default_context(cafile=certifi.where() if certifi else None)
