import hashlib
import secrets


def generate_token() -> str:
    """A random, URL-safe token with 256 bits of entropy."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Tokens are stored only as SHA-256 hashes, so a database leak does not leak tokens."""
    return hashlib.sha256(token.encode()).hexdigest()
