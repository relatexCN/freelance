from typing import Optional
from fastapi.responses import JSONResponse

def make_error_response(message: str, status_code: int = 500, type: str = "upstream_error", code: Optional[str] = None):
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": message,
                "type": type,
                "code": code or "internal_error"
            }
        }
    )
