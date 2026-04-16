from fastapi import Header, HTTPException
from src.config import Config


def verify_api_key(x_api_key: str = Header(None)) -> str:
    if x_api_key is None or x_api_key != Config.API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return x_api_key
