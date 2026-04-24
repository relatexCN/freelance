from typing import Optional, AsyncIterator
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

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

class SSEStreamingResponse(StreamingResponse):
    """Custom SSE streaming response with proper headers and no buffering."""
    
    def __init__(self, content: AsyncIterator[str], **kwargs):
        headers = kwargs.pop("headers", {})
        headers.update({
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
            "Transfer-Encoding": "chunked",
            "Content-Type": "text/event-stream",
        })
        super().__init__(content, media_type="text/event-stream", headers=headers, **kwargs)
