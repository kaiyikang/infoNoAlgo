"""Shared by all sources: SQLite cache and OpenRouter translation."""

import os
import sqlite3

import httpx

MODEL = os.environ[
    "TRANSLATE_MODEL"
]  # OpenRouter model id, e.g. from openrouter.ai/models
DB = os.getenv("DB_PATH", "/data/cache.db")
PROMPT = "把下面的英文译成简体中文。保留段落划分(空行分隔),只输出译文,不要解释。\n\n"


def conn():
    return sqlite3.connect(DB)


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
