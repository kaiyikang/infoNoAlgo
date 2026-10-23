"""Feed proxy: each module in sources/ fetches one upstream and serves its own RSS route.

A background thread fills a SQLite cache; the HTTP endpoints only read the cache,
so Miniflux never waits on translation.
"""

import os
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI

from sources import blog, x

SOURCES = [blog, x]  # each module provides refresh() and router; add new ones here
REFRESH_SECONDS = int(os.getenv("REFRESH_SECONDS", "1800"))


def loop():
    while True:
        for s in SOURCES:
            try:
                s.refresh()
            except Exception as ex:  # one broken source must not stop the others
                print(f"{s.__name__} error:", ex, flush=True)
        time.sleep(REFRESH_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=loop, daemon=True).start()
    yield


app = FastAPI(lifespan=lifespan)
for s in SOURCES:
    app.include_router(s.router)


@app.get("/health")
def health():
    return {"ok": True}
