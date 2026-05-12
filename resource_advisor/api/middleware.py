import time
import uuid
import logging
import traceback
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger("resource_advisor")

class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

class TimingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start_time = time.time()
        response = await call_next(request)
        process_time = time.time() - start_time
        response.headers["X-Response-Time-Ms"] = str(round(process_time * 1000, 2))
        return response

def add_middleware(app: FastAPI):
    app.add_middleware(TimingMiddleware)
    app.add_middleware(RequestIDMiddleware)
    
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        request_id = getattr(request.state, "request_id", "unknown")
        
        # We can expand this with specific exception types for RESOURCE_NOT_FOUND etc.
        error_code = "ENGINE_ERROR"
        if hasattr(exc, "error_code"):
            error_code = exc.error_code # type: ignore
            
        status_code = 500
        if hasattr(exc, "status_code"):
            status_code = exc.status_code # type: ignore
        
        logger.error(f"Error {error_code} on request {request_id}: {exc}\n{traceback.format_exc()}")
        
        return JSONResponse(
            status_code=status_code,
            content={
                "error": {
                    "code": error_code,
                    "message": str(exc),
                    "request_id": request_id,
                }
            }
        )
