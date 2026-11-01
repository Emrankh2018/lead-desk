"""ASGI entry point for servers such as uvicorn in the container: `uvicorn leadgen.web.asgi:app`."""
from leadgen.web.app import create_app

app = create_app()
