"""Feed proxy (test): Paul Graham essays -> full-text Chinese RSS.

A background thread fills a SQLite cache; the HTTP endpoint only reads the cache,
so Miniflux never waits on translation.
"""

import calendar
import html
import os
import re
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import feedparser
import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Response
from feedgen.feed import FeedGenerator

UPSTREAM = os.getenv("UPSTREAM_FEED", "https://paulgrahamessays.val.run/")
MODEL = os.environ[
    "TRANSLATE_MODEL"
]  # OpenRouter model id, e.g. from openrouter.ai/models
DB = os.getenv("DB_PATH", "/data/cache.db")
MAX_NEW_PER_RUN = int(os.getenv("MAX_NEW_PER_RUN", "3"))  # cost guard
REFRESH_SECONDS = int(os.getenv("REFRESH_SECONDS", "1800"))
PROMPT = "把下面的英文译成简体中文。保留段落划分(空行分隔),只输出译文,不要解释。\n\n"


def conn():
    return sqlite3.connect(DB)


with conn() as c:
    c.execute(
        "create table if not exists items "
        "(guid text primary key, title text, html text, ts real)"
    )


def extract(url: str) -> list[str]:
    """Fetch an essay page and return its paragraphs. Selector may need tuning."""
    r = httpx.get(url, timeout=30, follow_redirects=True)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    for t in soup(["script", "style"]):
        t.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n\n")
    text = soup.get_text()
    paras = [" ".join(p.split()) for p in re.split(r"\n{2,}", text)]
    return [p for p in paras if p]


def chunks(paras: list[str], limit: int = 3000):
    buf, n = [], 0
    for p in paras:
        if buf and n + len(p) > limit:
            yield "\n\n".join(buf)
            buf, n = [], 0
        buf.append(p)
        n += len(p)
    if buf:
        yield "\n\n".join(buf)


def translate(text: str) -> str:
    r = httpx.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"},
        json={
            "model": MODEL,
            "max_tokens": 4000,
            "reasoning": {"enabled": False},  # translation needs no thinking; saves time and tokens
            "messages": [{"role": "user", "content": PROMPT + text}],
        },
        timeout=180,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def to_html(url: str, zh: str) -> str:
    body = "".join(f"<p>{html.escape(p)}</p>" for p in zh.split("\n\n") if p.strip())
    return f'<p><a href="{html.escape(url)}">原文 / Original</a></p>{body}'


def refresh():
    feed = feedparser.parse(UPSTREAM)
    done = 0
    for e in feed.entries[:10]:
        guid = e.link
        with conn() as c:
            if c.execute("select 1 from items where guid=?", (guid,)).fetchone():
                continue
        if done >= MAX_NEW_PER_RUN:
            break
        try:
            parts = list(chunks(extract(guid)))
            out = []
            for i, ch in enumerate(parts, 1):
                print(f"translating {guid} chunk {i}/{len(parts)}", flush=True)
                out.append(translate(ch))
            zh = "\n\n".join(out)
            title = f"{translate(e.title)} | {e.title}"
            ts = (
                calendar.timegm(e.published_parsed)
                if e.get("published_parsed")
                else time.time()
            )
            with conn() as c:
                c.execute(
                    "insert into items values (?,?,?,?)",
                    (guid, title, to_html(guid, zh), ts),
                )
            done += 1
            print("translated:", guid, flush=True)
        except Exception as ex:  # keep the loop alive
            print("refresh failed:", guid, ex, flush=True)
            if isinstance(ex, httpx.HTTPStatusError) and ex.request.url.host == "openrouter.ai":
                print(ex.response.text, flush=True)
                break  # model/quota problem hits every item: stop, retry next run


def loop():
    while True:
        try:
            refresh()
        except Exception as ex:
            print("loop error:", ex, flush=True)
        time.sleep(REFRESH_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=loop, daemon=True).start()
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/pg.xml")
def pg():
    with conn() as c:
        rows = c.execute(
            "select guid, title, html, ts from items order by ts desc limit 30"
        ).fetchall()
    fg = FeedGenerator()
    fg.id("pg-zh")
    fg.title("Paul Graham (中文)")
    fg.link(href="https://paulgraham.com/articles.html")
    fg.description("Translated essays, full text")
    for guid, title, body, ts in rows:
        fe = fg.add_entry()
        fe.id(guid)
        fe.title(title)
        fe.link(href=guid)
        fe.content(body, type="html")
        fe.published(datetime.fromtimestamp(ts, timezone.utc))
    return Response(fg.rss_str(pretty=True), media_type="application/rss+xml; charset=utf-8")


@app.get("/health")
def health():
    return {"ok": True}
