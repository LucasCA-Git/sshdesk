"""FastAPI application factory.

Run with: uvicorn sshdesk_server.main:create_app --factory --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from sshdesk_server import __version__
from sshdesk_server.config import Settings
from sshdesk_server.db import Database
from sshdesk_server.routes import auth, hosts, invites, teams
from sshdesk_server.security import RateLimiter

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(title="SSHDesk Server", version=__version__, docs_url="/docs", redoc_url=None)
    app.state.settings = settings
    app.state.db = Database(settings.database_url)
    app.state.db.create_all()
    app.state.login_limiter = RateLimiter(10, 60)
    app.state.register_limiter = RateLimiter(10, 3600)
    app.state.invite_limiter = RateLimiter(10, 600)

    for router in (auth.router, teams.router, invites.router, hosts.router):
        app.include_router(router, prefix="/api/v1")

    @app.get("/api/v1/info", tags=["meta"])
    def info() -> dict:
        return {
            "name": "SSHDesk Server",
            "version": __version__,
            "registration": settings.allow_registration,
            "email_invites": settings.smtp_enabled,
        }

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        return {"status": "ok"}

    return app

