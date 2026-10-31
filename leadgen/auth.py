"""Single-user password authentication (scrypt hashes, stdlib only) with a small login throttle."""
import base64
import hashlib
import hmac
import secrets
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from leadgen.models import User

_N, _R, _P = 2**14, 8, 1
MAX_FAILURES = 5
LOCKOUT_SECONDS = 60


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P)
    return f"scrypt${_N}${_R}${_P}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt_b64, digest_b64 = stored.split("$")
        digest = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), n=int(n), r=int(r), p=int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, base64.b64decode(digest_b64))


def set_password(session: Session, username: str, password: str) -> User:
    if len(password) < 10:
        raise ValueError("password must be at least 10 characters")
    user = session.scalars(select(User).where(User.username == username)).first()
    if user:
        user.password_hash = hash_password(password)
    else:
        user = User(username=username, password_hash=hash_password(password))
        session.add(user)
    session.commit()
    return user


class LoginThrottle:
    """In-memory failure counter per client address. Enough for a single-user local app."""

    def __init__(self):
        self._failures: dict[str, tuple[int, float]] = {}

    def locked(self, key: str) -> bool:
        count, since = self._failures.get(key, (0, 0.0))
        if count >= MAX_FAILURES and time.monotonic() - since < LOCKOUT_SECONDS:
            return True
        if count >= MAX_FAILURES:
            self._failures.pop(key, None)
        return False

    def fail(self, key: str) -> None:
        count, _ = self._failures.get(key, (0, 0.0))
        self._failures[key] = (count + 1, time.monotonic())

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)


def authenticate(session: Session, username: str, password: str) -> User | None:
    user = session.scalars(select(User).where(User.username == username)).first()
    if not user:
        verify_password(password, hash_password("timing-equaliser"))
        return None
    return user if verify_password(password, user.password_hash) else None
