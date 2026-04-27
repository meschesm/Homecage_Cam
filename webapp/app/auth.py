from datetime import datetime, timedelta, timezone

from fastapi import Cookie, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from jose import JWTError, jwt
from passlib.context import CryptContext

from .config import settings

_crypt = CryptContext(schemes=["bcrypt"], deprecated="auto")
_COOKIE = "hcv3_token"


# ── Token helpers ──────────────────────────────────────────────────────────────

def create_token(username: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.token_expire_minutes)
    return jwt.encode({"sub": username, "exp": expire}, settings.secret_key, settings.algorithm)


def _decode(token: str) -> str | None:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
        return payload.get("sub")
    except JWTError:
        return None


# ── Password ───────────────────────────────────────────────────────────────────

def verify_password(plain: str, hashed: str) -> bool:
    return _crypt.verify(plain, hashed)


def hash_password(plain: str) -> str:
    return _crypt.hash(plain)


# ── Dependencies ───────────────────────────────────────────────────────────────

def require_auth(request: Request, hcv3_token: str | None = Cookie(default=None)) -> str:
    """
    Dependency for page routes — redirects to /login on failure.
    Returns the authenticated username.
    """
    if hcv3_token:
        user = _decode(hcv3_token)
        if user:
            return user
    # Preserve the intended destination so login can redirect back
    return_to = str(request.url)
    raise HTTPException(
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
        headers={"Location": f"/login?next={return_to}"},
    )


def require_auth_api(hcv3_token: str | None = Cookie(default=None)) -> str:
    """Dependency for API routes — returns 401 on failure."""
    if hcv3_token:
        user = _decode(hcv3_token)
        if user:
            return user
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")


# ── Login / logout ─────────────────────────────────────────────────────────────

def login_response(username: str, next_url: str = "/") -> RedirectResponse:
    token = create_token(username)
    resp = RedirectResponse(url=next_url, status_code=status.HTTP_303_SEE_OTHER)
    resp.set_cookie(
        _COOKIE, token,
        httponly=True,
        samesite="lax",
        max_age=settings.token_expire_minutes * 60,
    )
    return resp


def logout_response() -> RedirectResponse:
    resp = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    resp.delete_cookie(_COOKIE)
    return resp
