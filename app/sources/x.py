"""Selected X accounts via twitterapi.io -> RSS at /x.xml. English tweets get a Chinese translation."""

import html
import os
import time
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Response
from feedgen.feed import FeedGenerator

from common import conn, translate

router = APIRouter()
API = "https://api.twitterapi.io/twitter/tweet/advanced_search"
QUERY = os.getenv("X_QUERY", "")  # e.g. "from:a OR from:b"; ponytail: from a private List later
MAX_PAGES = 5  # cost guard: up to 20 tweets per page, billed per tweet


with conn() as c:
    c.execute(
        "create table if not exists tweets "
        "(guid text primary key, title text, author text, html text, ts real)"
    )


def fetch(since: int) -> list[dict]:
    """All tweets matching QUERY since a unix time, newest first."""
    tweets, cursor = [], ""
    for _ in range(MAX_PAGES):
        r = httpx.get(
            API,
            headers={"X-API-Key": os.environ["X_API_KEY"]},
            params={
                "query": f"({QUERY}) since_time:{since}",
                "queryType": "Latest",
                "cursor": cursor,
            },
            timeout=30,
        )
        r.raise_for_status()
        d = r.json()
        tweets += d["tweets"]
        if not d["has_next_page"]:
            break
        cursor = d["next_cursor"]
    return tweets


def row(t: dict, tr=translate) -> tuple:
    a = t["author"]
    text = html.escape(t["text"]).replace("\n", "<br>")
    body = f"<p>{text}</p>"
    if t.get("lang") == "en":
        zh = html.escape(tr(t["text"])).replace("\n", "<br>")
        body = f"{body}<p>{zh}</p>"
    ts = datetime.strptime(t["createdAt"], "%a %b %d %H:%M:%S %z %Y").timestamp()
    return (t["url"], t["text"][:80], f'{a["name"]} (@{a["userName"]})', body, ts)


def refresh():
    if not QUERY:
        return  # source off until X_QUERY is set
    with conn() as c:
        last = c.execute("select max(ts) from tweets").fetchone()[0]
    since = int(last or time.time() - 86400)  # first run: last 24h
    new = 0
    # oldest first: if translation fails midway, max(ts) still marks where to resume
    for t in reversed(fetch(since)):
        with conn() as c:
            if c.execute("select 1 from tweets where guid=?", (t["url"],)).fetchone():
                continue
            c.execute("insert into tweets values (?,?,?,?,?)", row(t))
        new += 1
    print(f"x: {new} new tweets", flush=True)


@router.get("/x.xml")
def x():
    with conn() as c:
        rows = c.execute(
            "select guid, title, author, html, ts from tweets order by ts desc limit 50"
        ).fetchall()
    fg = FeedGenerator()
    fg.load_extension("dc")
    fg.id("x")
    fg.title("X (selected)")
    fg.link(href="https://x.com")
    fg.description("Selected X accounts, English translated")
    for guid, title, author, body, ts in rows:
        fe = fg.add_entry()
        fe.id(guid)
        fe.title(title)
        fe.link(href=guid)
        fe.dc.dc_creator(author)  # RSS <author> must be an email
        fe.content(body, type="html")
        fe.published(datetime.fromtimestamp(ts, timezone.utc))
    return Response(fg.rss_str(pretty=True), media_type="application/rss+xml; charset=utf-8")


if __name__ == "__main__":
    t = {
        "url": "https://x.com/a/status/1",
        "text": "hello\nworld",
        "lang": "en",
        "createdAt": "Tue Dec 10 07:00:30 +0000 2024",
        "author": {"name": "A", "userName": "a"},
    }
    guid, title, author, body, ts = row(t, tr=lambda s: "你好\n世界")
    assert author == "A (@a)" and ts == 1733814030
    assert body == "<p>hello<br>world</p><p>你好<br>世界</p>", body
    assert "你好" not in row({**t, "lang": "ja"}, tr=None)[3]
    print("ok")
