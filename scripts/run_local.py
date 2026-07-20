from __future__ import annotations

import os
import threading
import webbrowser

import uvicorn


def open_browser() -> None:
    if os.getenv("GAIA_OPEN_BROWSER", "1") == "1":
        webbrowser.open("http://127.0.0.1:8501")


if __name__ == "__main__":
    threading.Timer(1.0, open_browser).start()
    uvicorn.run("gaia.api:app", host="127.0.0.1", port=8501, reload=False)
