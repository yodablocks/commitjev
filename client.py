"""Jev transport: the official SDK, plus a cache and a thread pool.

Caching pays here in a way it does not everywhere. A commit is immutable, and
so are the rules it is judged against, so a second run over the same range is
free and instant. That is what makes the thresholds in `rules.py` cheap to
tune: change PASS, re-run, see the report move, without spending anything.

The key covers the model, the state, and the questions, so editing a rule's
wording correctly misses the cache and re-asks.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, TypeVar

from typesafe_sdk import RetryPolicy, TypeSafeClient

DEFAULT_MODEL = "jev-latest"
USD_PER_INPUT_TOKEN = 0.042 / 1_000_000   # docs.typesafe.ai/models, read 2026-09-19
KEY_VARS = ("TYPESAFE_API_KEY", "TYPESAFE_AI_API_KEY")
HERE = Path(__file__).resolve().parent

T = TypeVar("T")
R = TypeVar("R")


class NoAPIKey(RuntimeError):
    pass


def _load_dotenv() -> None:
    for candidate in (HERE / ".env", HERE.parent / ".env", HERE.parent.parent / ".env"):
        if not candidate.is_file():
            continue
        for line in candidate.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip().removeprefix("export ").strip()
            os.environ.setdefault(key, value.strip().strip("'\""))


def api_key() -> str:
    if not any(os.environ.get(var) for var in KEY_VARS):
        _load_dotenv()
    for var in KEY_VARS:
        if os.environ.get(var):
            return os.environ[var]
    raise NoAPIKey(
        "No TypeSafe API key. Export TYPESAFE_API_KEY, or put it in "
        f"{HERE / '.env'}. Keys come from https://console.typesafe.ai/"
    )


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    requests: int = 0
    cache_hits: int = 0

    @property
    def cost_usd(self) -> float:
        return self.input_tokens * USD_PER_INPUT_TOKEN


class Cache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._local = threading.local()
        with self._conn() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS answers ("
                "key TEXT PRIMARY KEY, model TEXT, payload TEXT, "
                "input_tokens INTEGER, created_at REAL)"
            )

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            self._local.conn = sqlite3.connect(self.path, timeout=30)
            self._local.conn.execute("PRAGMA journal_mode=WAL")
        return self._local.conn

    @staticmethod
    def key(model: str, state: Any, questions: dict) -> str:
        blob = json.dumps(
            {"model": model, "state": state, "questions": questions},
            sort_keys=True, ensure_ascii=False,
        )
        return hashlib.sha256(blob.encode()).hexdigest()

    def get(self, key: str) -> dict | None:
        row = self._conn().execute(
            "SELECT payload FROM answers WHERE key = ?", (key,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, model: str, answers: dict, input_tokens: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO answers VALUES (?, ?, ?, ?, ?)",
                (key, model, json.dumps(answers, ensure_ascii=False),
                 input_tokens, time.time()),
            )


class Jev:
    def __init__(
        self,
        cache_path: Path,
        model: str = DEFAULT_MODEL,
        use_cache: bool = True,
        timeout: float = 90.0,
    ):
        self.model = model
        self.use_cache = use_cache
        self.cache = Cache(cache_path)
        self.usage = Usage()
        self._lock = threading.Lock()
        self._client = TypeSafeClient(
            api_key=api_key(),
            model=model,
            timeout=timeout,
            retry=RetryPolicy(max_retries=5, backoff_max=20.0, timeout=timeout),
        )

    def ask(self, state: Any, questions: dict) -> dict:
        """One request, every question. Returns the raw answers map."""
        key = Cache.key(self.model, state, questions)
        if self.use_cache:
            hit = self.cache.get(key)
            if hit is not None:
                with self._lock:
                    self.usage.cache_hits += 1
                return hit

        response = self._client.system_one(state, questions)
        answers = _plain_answers(response)
        with self._lock:
            self.usage.input_tokens += response.usage.input_tokens
            self.usage.output_tokens += response.usage.output_tokens
            self.usage.requests += 1
        self.cache.put(key, self.model, answers, response.usage.input_tokens)
        return answers

    def close(self) -> None:
        self._client.close()


def _plain_answers(response) -> dict:
    """The answers as plain JSON, which is what the cache and rules.py want.

    The wire format is the honest record, so take it when the SDK still has the
    response around, and rebuild it from the typed objects when it does not.
    """
    raw = response.raw_http_response
    if raw is not None:
        return raw.json()["answers"]
    plain = {}
    for qid, answer in response.answers.items():
        if hasattr(answer, "noul"):
            plain[qid] = {"type": "noul", "noul": answer.noul}
        elif hasattr(answer, "choice"):
            plain[qid] = {
                "type": "choice", "choice": answer.choice,
                "confidence": answer.confidence,
                "probabilities": dict(answer.probabilities),
            }
        else:
            plain[qid] = {
                "type": "score", "score": answer.score,
                "confidence": answer.confidence,
                "probabilities": dict(answer.probabilities),
            }
    return plain


def in_parallel(
    items: Iterable[T],
    work: Callable[[T], R],
    workers: int = 8,
    on_done: Callable[[], None] | None = None,
) -> list[R | Exception]:
    """Map over items, keeping input order, keeping failures as values."""
    items = list(items)

    def guarded(item: T) -> R | Exception:
        try:
            return work(item)
        except Exception as exc:       # one bad commit must not sink the run
            return exc
        finally:
            if on_done:
                on_done()

    with ThreadPoolExecutor(max_workers=min(workers, max(len(items), 1))) as pool:
        return list(pool.map(guarded, items))
