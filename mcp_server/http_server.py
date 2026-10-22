import contextlib
import os
import secrets

import uvicorn
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Mount

from server import mcp  # same tools, same prompt, same .env

API_KEY = os.environ["MCP_API_KEY"]


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Reject any request that does not carry the right x-api-key header."""

    async def dispatch(self, request, call_next):
        supplied = request.headers.get("x-api-key", "")
        if not secrets.compare_digest(supplied, API_KEY):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


mcp_app = mcp.streamable_http_app(stateless_http=True, json_response=True)


@contextlib.asynccontextmanager
async def lifespan(app):
    async with mcp.session_manager.run():
        yield


app = Starlette(routes=[Mount("/", app=mcp_app)], lifespan=lifespan)
app.add_middleware(ApiKeyMiddleware)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)