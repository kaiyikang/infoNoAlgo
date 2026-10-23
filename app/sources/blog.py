"""Blog RSS feeds -> full-text Chinese RSS at /blog/{slug}.xml (plus /pg.xml, the original URL)."""

import calendar
import html
import os
import re
import time
from datetime import datetime, timezone

import feedparser
import httpx
from bs4 import BeautifulSoup
from fastapi import APIRouter, HTTPException, Response
from feedgen.feed import FeedGenerator

from common import conn, translate

router = APIRouter()
MAX_NEW_PER_RUN = int(os.getenv("MAX_NEW_PER_RUN", "3"))  # cost guard, per feed
FEEDS = {
    # slug: (upstream RSS, title, where the full text is)
    # "feed": the RSS item carries the full HTML; "page": only a summary, fetch the page
    "pg": ("https://paulgrahamessays.val.run/", "Paul Graham", "page"),
    "newport": ("https://calnewport.com/feed/", "Cal Newport", "feed"),
}


with conn() as c:
    c.execute(
        "create table if not exists items "
        "(guid text primary key, title text, html text, ts real)"
    )
    # rows from before multi-feed support are all Paul Graham
    if "source" not in [r[1] for r in c.execute("pragma table_info(items)")]:
        c.execute("alter table items add column source text not null default 'pg'")


def extract(url: str) -> list[str]:
    """Fetch an essay page and return its paragraphs. Tuned for PG's <br>-separated HTML."""
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


def html_paras(body: str) -> list[str]:
    """Paragraphs of a feed item's HTML content."""
    blocks = BeautifulSoup(body, "html.parser").find_all(["p", "h2", "h3", "h4", "li"])
    paras = [" ".join(b.get_text(" ").split()) for b in blocks]
    # drop WordPress's "The post X appeared first on Y." footer
    return [p for p in paras if p and not (p.startswith("The post ") and "appeared first on" in p)]


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


def pair(src: str, zh: str) -> list[tuple[str, str]]:
    """Match a chunk's paragraphs with their translations, one to one."""
    s = src.split("\n\n")
    z = [p for p in zh.split("\n\n") if p.strip()]
    if len(s) != len(z):  # model merged or split paragraphs: keep the chunk as one block
        return [(src, zh)]
    return list(zip(s, z))


def to_html(url: str, pairs: list[tuple[str, str]]) -> str:
    """Bilingual body: each original paragraph followed by its translation."""
    def ps(text):
        return "".join(f"<p>{html.escape(p)}</p>" for p in text.split("\n\n") if p.strip())
    body = "".join(ps(en) + ps(zh) for en, zh in pairs)
    return f'<p><a href="{html.escape(url)}">原文 / Original</a></p>{body}'


def refresh_one(slug: str, upstream: str, mode: str):
    feed = feedparser.parse(upstream)
    done = 0
    for e in feed.entries[:10]:
        guid = e.link
        with conn() as c:
            if c.execute("select 1 from items where guid=?", (guid,)).fetchone():
                continue
        if done >= MAX_NEW_PER_RUN:
            break
        try:
            paras = html_paras(e.content[0].value) if mode == "feed" else extract(guid)
            parts = list(chunks(paras))
            out = []
            for i, ch in enumerate(parts, 1):
                print(f"translating {guid} chunk {i}/{len(parts)}", flush=True)
                out += pair(ch, translate(ch))
            title = f"{e.title} | {translate(e.title)}"
            ts = (
                calendar.timegm(e.published_parsed)
                if e.get("published_parsed")
                else time.time()
            )
            with conn() as c:
                c.execute(
                    "insert into items (guid, title, html, ts, source) values (?,?,?,?,?)",
                    (guid, title, to_html(guid, out), ts, slug),
                )
            done += 1
            print("translated:", guid, flush=True)
        except Exception as ex:  # keep the loop alive
            print("refresh failed:", guid, ex, flush=True)
            if isinstance(ex, httpx.HTTPStatusError) and ex.request.url.host == "openrouter.ai":
                print(ex.response.text, flush=True)
                raise  # model/quota problem hits every item and feed: stop, retry next run


def refresh():
    for slug, (upstream, _, mode) in FEEDS.items():
        refresh_one(slug, upstream, mode)


@router.get("/pg.xml")  # original URL, kept so existing subscriptions keep working
@router.get("/blog/{slug}.xml")
def blog(slug: str = "pg"):
    if slug not in FEEDS:
        raise HTTPException(404)
    upstream, name, _ = FEEDS[slug]
    with conn() as c:
        rows = c.execute(
            "select guid, title, html, ts from items where source=? order by ts desc limit 30",
            (slug,),
        ).fetchall()
    fg = FeedGenerator()
    fg.id(f"{slug}-zh")
    fg.title(f"{name} (中文)")
    fg.link(href=upstream)
    fg.description("Translated essays, full text")
    for guid, title, body, ts in rows:
        fe = fg.add_entry()
        fe.id(guid)
        fe.title(title)
        fe.link(href=guid)
        fe.content(body, type="html")
        fe.published(datetime.fromtimestamp(ts, timezone.utc))
    return Response(fg.rss_str(pretty=True), media_type="application/rss+xml; charset=utf-8")


if __name__ == "__main__":
    got = html_paras("<p>One <a href='#'>link</a> here.</p><h2>Head</h2><ul><li>item</li></ul><p> </p>"
        "<p>The post Head appeared first on Blog .</p>")
    assert got == ["One link here.", "Head", "item"], got
    assert pair("a\n\nb", "甲\n\n乙\n\n") == [("a", "甲"), ("b", "乙")]
    assert pair("a\n\nb", "甲乙") == [("a\n\nb", "甲乙")]
    assert to_html("u", [("a\n\nb", "甲乙")]).endswith("<p>a</p><p>b</p><p>甲乙</p>")
    print("ok")
