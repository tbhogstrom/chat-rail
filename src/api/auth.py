from fastapi import Request, HTTPException
from src.config import Config
from src.api.session import valid_token


def verify_api_key(request: Request) -> None:
    """Authorize a request via the x-api-key header OR a valid session cookie."""
    key = request.headers.get("x-api-key")
    if key is not None and key == Config.API_KEY:
        return
    if valid_token(request.cookies.get("sfw_session"), Config.APP_PASSWORD):
        return
    raise HTTPException(status_code=401, detail="Invalid API key")
