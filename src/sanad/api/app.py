"""Explicitly configured health application; no external startup dependencies."""

from fastapi import FastAPI


def create_app(revision: str = "dev") -> FastAPI:
    """Build an independent application without reading runtime configuration."""
    app = FastAPI()

    @app.get("/health")
    async def health() -> dict[str, bool | str]:
        return {"ok": True, "service": "sanad", "revision": revision}

    return app
