"""Signed stateless session token for the shared-password gate."""
import hashlib
import hmac

_MSG = b"sfw-authed"


def make_token(password: str) -> str:
    """Hex HMAC-SHA256 of a fixed message keyed by the shared password."""
    return hmac.new(password.encode(), _MSG, hashlib.sha256).hexdigest()


def valid_token(token: str | None, password: str) -> bool:
    """True iff `token` matches `make_token(password)` (constant-time).

    False for missing tokens or an empty password (gate treated as off).
    """
    if not token or not password:
        return False
    return hmac.compare_digest(token, make_token(password))
