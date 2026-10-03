from fastapi import Request
from fastapi.responses import JSONResponse


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str, latest=None, headers=None):
        self.status, self.code, self.message = status, code, message
        self.latest, self.headers = latest, headers or {}


async def api_error(request: Request, exc: APIError):
    error = {"code": exc.code, "message": exc.message, "fields": {}}
    if exc.latest is not None:
        error["latest"] = exc.latest
    return JSONResponse(
        {"error": error, "request_id": request.state.request_id},
        status_code=exc.status,
        headers=exc.headers,
    )
