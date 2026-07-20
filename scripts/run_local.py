from __future__ import annotations

import asyncio
import os
import threading
import webbrowser

import uvicorn

from gaia.api import service


def open_browser() -> None:
    if os.getenv("GAIA_OPEN_BROWSER", "1") == "1":
        webbrowser.open("http://127.0.0.1:8501")


async def initial_sync() -> None:
    try:
        await service.sync()
    except Exception:
        # The API remains usable and exposes source failures in Coverage.
        pass


if __name__ == "__main__":
    threading.Timer(1.0, open_browser).start()
    threading.Thread(target=lambda: asyncio.run(initial_sync()), daemon=True).start()
    uvicorn.run("gaia.api:app", host="127.0.0.1", port=8501, reload=False)
