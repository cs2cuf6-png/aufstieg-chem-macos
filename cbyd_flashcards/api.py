"""Chemistry By Design API client, local catalog cache, and async asset downloader."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import httpx

BASE_URL = "https://chemistrybydesign.oia.arizona.edu/"
CACHE_DIR = Path.home() / ".cache" / "cbyd_flashcards"
PREVIEW_CACHE_DIR = CACHE_DIR / "previews"
CATALOG_CACHE_FILE = CACHE_DIR / "catalog.json"
CATALOG_TTL_SECONDS = 7 * 24 * 3600  # 7 days


@dataclass(slots=True)
class CatalogItem:
    """Single synthesis entry from listJSON.php."""

    full: str
    path: str
    year: str
    drug: str
    name: str
    author: str

    @classmethod
    def from_dict(cls, data: dict) -> CatalogItem:
        return cls(
            full=str(data.get("full", "")).strip(),
            path=str(data.get("path", "")).strip(),
            year=str(data.get("year", "")).strip(),
            drug=str(data.get("drug", "")).strip(),
            name=str(data.get("name", "")).strip(),
            author=str(data.get("author", "")).strip(),
        )

    def to_dict(self) -> dict:
        return {
            "full": self.full,
            "path": self.path,
            "year": self.year,
            "drug": self.drug,
            "name": self.name,
            "author": self.author,
        }


@dataclass(slots=True)
class StepData:
    """One reaction step (from molecule N to molecule N+2 via reagent N+1)."""

    step_index: int  # 1-based step number (1, 2, 3...)
    reagent_num: int  # CByD image number (2, 4, 6...)
    reagent_b64: str  # data:image/png;base64,...
    target_b64: str  # product molecule image (N+2)
    named_reaction_b64: str | None = None
    subsequence: SynthesisSequence | None = None


@dataclass(slots=True)
class SynthesisSequence:
    """Complete synthesis or subsequence with all embedded PNGs."""

    name: str
    path: str
    doi: str
    start_b64: str  # 1.png
    steps: list[StepData] = field(default_factory=list)
    citation_b64: str | None = None


@dataclass(slots=True)
class SynthesisMeta:
    """Lightweight metadata from infoJSON.php."""

    name: str
    path: str
    max_num: int
    total_steps: int
    reactions: list[int]
    subsequences: list[int]
    has_citation: bool
    doi: str


def encode_cbyd_path(raw_path: str) -> str:
    """Normalize and percent-encode a CByD relative directory path."""
    decoded = urllib.parse.unquote(raw_path).strip("/")
    return urllib.parse.quote(decoded, safe="/")


def clean_doi(raw_doi: str) -> str:
    """Decode percent-encoded DOI string."""
    return urllib.parse.unquote(raw_doi or "").strip()


def parse_int_list(raw: str) -> list[int]:
    """Parse comma-separated integer list, ignoring '..' or empty values."""
    result: list[int] = []
    for part in str(raw or "").split(","):
        part = part.strip()
        if part.isdigit():
            result.append(int(part))
    return result


def extract_query_from_url(text: str) -> str:
    """Extract synthesis name or path from a CByD URL if given, else return text."""
    text = text.strip()
    if not text.startswith(("http://", "https://")):
        return text

    parsed = urllib.parse.urlparse(text)
    if parsed.fragment:
        frag = urllib.parse.unquote(parsed.fragment)
        parts = [p for p in frag.split("/") if p and p != "quiz"]
        # Strip trailing numeric page indices if present
        while len(parts) > 1 and parts[-1].isdigit():
            parts.pop()
        if parts:
            return parts[0]

    qs = urllib.parse.parse_qs(parsed.query)
    if "dir" in qs and qs["dir"]:
        return urllib.parse.unquote(qs["dir"][0])

    return text


def load_cached_catalog(max_age: int = CATALOG_TTL_SECONDS) -> list[CatalogItem] | None:
    """Load catalog from local JSON cache if fresh."""
    if not CATALOG_CACHE_FILE.exists():
        return None
    try:
        payload = json.loads(CATALOG_CACHE_FILE.read_text(encoding="utf-8"))
        ts = float(payload.get("fetched_at", 0))
        if max_age > 0 and (time.time() - ts) > max_age:
            return None
        return [CatalogItem.from_dict(x) for x in payload.get("items", [])]
    except Exception:
        return None


def save_cached_catalog(items: list[CatalogItem]) -> None:
    """Persist catalog to local JSON cache."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at": time.time(),
        "items": [x.to_dict() for x in items],
    }
    CATALOG_CACHE_FILE.write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


async def fetch_catalog(force_refresh: bool = False) -> tuple[list[CatalogItem], bool]:
    """Return (items, from_cache). Fetches from CByD if cache is missing/stale/forced."""
    if not force_refresh:
        cached = load_cached_catalog()
        if cached is not None:
            return cached, True

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        resp = await client.get(f"{BASE_URL}listJSON.php")
        resp.raise_for_status()
        raw_items = resp.json()

    items = [CatalogItem.from_dict(x) for x in raw_items]
    save_cached_catalog(items)
    return items, False


def search_catalog(
    items: list[CatalogItem],
    query: str,
    mode: str = "all",
) -> list[CatalogItem]:
    """Filter and rank catalog items by mode ('all', 'name', 'author', 'year', 'drugs', 'url')."""
    q = extract_query_from_url(query).strip().lower()

    if mode == "drugs":
        pool = [it for it in items if it.drug]
    else:
        pool = items

    if not q:
        return pool

    def matches(it: CatalogItem) -> tuple[int, str] | None:
        if mode == "name":
            hay = it.name.lower()
            full_hay = it.full.lower()
            if hay == q or full_hay == q:
                return (0, it.full)
            if hay.startswith(q):
                return (1, it.full)
            if q in hay:
                return (2, it.full)
            return None
        if mode == "author":
            hay = it.author.lower()
            if hay == q:
                return (0, it.full)
            if hay.startswith(q):
                return (1, it.full)
            if q in hay:
                return (2, it.full)
            return None
        if mode == "year":
            if it.year == q:
                return (0, it.full)
            if q in it.year:
                return (1, it.full)
            return None
        if mode == "drugs":
            hay = f"{it.drug} {it.name} {it.full}".lower()
            if it.drug.lower() == q or it.name.lower() == q:
                return (0, it.full)
            if q in hay:
                return (1, it.full)
            return None
        # 'all' or 'url'
        full_l = it.full.lower()
        name_l = it.name.lower()
        path_l = urllib.parse.unquote(it.path).lower()
        if full_l == q or name_l == q or path_l == q:
            return (0, it.full)
        if full_l.startswith(q) or name_l.startswith(q):
            return (1, it.full)
        combined = f"{it.full} {it.author} {it.year} {it.drug}".lower()
        if q in combined:
            return (2, it.full)
        return None

    scored: list[tuple[tuple[int, str], CatalogItem]] = []
    for it in pool:
        score = matches(it)
        if score is not None:
            scored.append((score, it))

    scored.sort(key=lambda pair: pair[0])
    return [pair[1] for pair in scored]


async def fetch_sequence_meta(
    client: httpx.AsyncClient, raw_path: str
) -> SynthesisMeta:
    """Fetch infoJSON.php metadata for a synthesis or subsequence directory."""
    enc_path = encode_cbyd_path(raw_path)
    resp = await client.get(f"{BASE_URL}infoJSON.php", params={"dir": urllib.parse.unquote(enc_path)})
    resp.raise_for_status()
    data = resp.json()

    max_num = int(data.get("max", 1))
    total_steps = max(0, (max_num - 1) // 2)
    return SynthesisMeta(
        name=str(data.get("name", "")).strip(),
        path=str(data.get("path", raw_path)).strip(),
        max_num=max_num,
        total_steps=total_steps,
        reactions=parse_int_list(data.get("reactions", "")),
        subsequences=parse_int_list(data.get("subsequences", "")),
        has_citation=str(data.get("citation", "")).lower() == "y",
        doi=clean_doi(str(data.get("doi", ""))),
    )


async def _fetch_png_b64(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    rel_path: str,
    on_done: Callable[[], None] | None = None,
) -> str | None:
    """Download a PNG and encode as data URI, returning None on 404."""
    url = f"{BASE_URL}{encode_cbyd_path(rel_path)}"
    async with sem:
        for attempt in range(3):
            try:
                resp = await client.get(url)
                if resp.status_code == 404:
                    if on_done:
                        on_done()
                    return None
                resp.raise_for_status()
                if on_done:
                    on_done()
                b64 = base64.b64encode(resp.content).decode("ascii")
                return f"data:image/png;base64,{b64}"
            except (httpx.TransportError, httpx.TimeoutException):
                if attempt == 2:
                    raise
                await asyncio.sleep(0.4 * (attempt + 1))
    return None


async def _download_sequence_recursive(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    raw_path: str,
    display_name: str,
    progress_cb: Callable[[int, int, str], None] | None = None,
    counter: dict[str, int] | None = None,
) -> SynthesisSequence:
    """Download a synthesis sequence and all nested subsequences concurrently."""
    if counter is None:
        counter = {"done": 0, "total": 0}

    meta = await fetch_sequence_meta(client, raw_path)
    base_dir = urllib.parse.unquote(meta.path).strip("/")

    # Count images in this sequence
    img_count = 1 + meta.total_steps * 2 + len(meta.reactions) + (1 if meta.has_citation else 0)
    counter["total"] += img_count

    def tick() -> None:
        counter["done"] += 1
        if progress_cb:
            progress_cb(counter["done"], counter["total"], display_name)

    # Schedule PNG downloads
    start_task = asyncio.create_task(_fetch_png_b64(client, sem, f"{base_dir}/1.png", tick))
    citation_task = (
        asyncio.create_task(_fetch_png_b64(client, sem, f"{base_dir}/citation.png", tick))
        if meta.has_citation
        else None
    )

    step_tasks: list[tuple[int, int, asyncio.Task, asyncio.Task, asyncio.Task | None, asyncio.Task | None]] = []
    reaction_set = set(meta.reactions)
    subseq_set = set(meta.subsequences)

    for idx in range(1, meta.total_steps + 1):
        reagent_num = idx * 2
        target_num = reagent_num + 1
        r_task = asyncio.create_task(_fetch_png_b64(client, sem, f"{base_dir}/{reagent_num}.png", tick))
        t_task = asyncio.create_task(_fetch_png_b64(client, sem, f"{base_dir}/{target_num}.png", tick))
        nr_task = (
            asyncio.create_task(_fetch_png_b64(client, sem, f"{base_dir}/{reagent_num}b.png", tick))
            if reagent_num in reaction_set
            else None
        )
        sub_task = (
            asyncio.create_task(
                _download_sequence_recursive(
                    client,
                    sem,
                    f"{base_dir}/{reagent_num}",
                    f"{display_name} (подсинтез шага {idx})",
                    progress_cb,
                    counter,
                )
            )
            if reagent_num in subseq_set
            else None
        )
        step_tasks.append((idx, reagent_num, r_task, t_task, nr_task, sub_task))

    start_b64 = await start_task or ""
    citation_b64 = await citation_task if citation_task else None

    steps: list[StepData] = []
    for idx, reagent_num, r_task, t_task, nr_task, sub_task in step_tasks:
        r_b64 = await r_task or ""
        t_b64 = await t_task or ""
        nr_b64 = await nr_task if nr_task else None
        sub_seq = await sub_task if sub_task else None
        steps.append(
            StepData(
                step_index=idx,
                reagent_num=reagent_num,
                reagent_b64=r_b64,
                target_b64=t_b64,
                named_reaction_b64=nr_b64,
                subsequence=sub_seq,
            )
        )

    return SynthesisSequence(
        name=display_name or meta.name,
        path=meta.path,
        doi=meta.doi,
        start_b64=start_b64,
        steps=steps,
        citation_b64=citation_b64,
    )


async def fetch_synthesis_bundle(
    item: CatalogItem,
    progress_cb: Callable[[int, int, str], None] | None = None,
) -> SynthesisSequence:
    """Download complete synthesis bundle (images + subsequences) for a catalog item."""
    sem = asyncio.Semaphore(6)
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        return await _download_sequence_recursive(
            client=client,
            sem=sem,
            raw_path=item.path,
            display_name=item.full,
            progress_cb=progress_cb,
        )


def _preview_cache_file(item_path: str) -> Path:
    digest = hashlib.sha1(item_path.encode("utf-8")).hexdigest()
    return PREVIEW_CACHE_DIR / f"{digest}.png"


def get_cached_preview_bytes(item_path: str) -> bytes | None:
    """Return cached target molecule PNG bytes if already downloaded."""
    p = _preview_cache_file(item_path)
    if p.is_file():
        return p.read_bytes()
    return None


async def fetch_target_preview_bytes(item: CatalogItem) -> bytes | None:
    """Fetch only the final target molecule PNG for quick preview, with local disk cache."""
    cached = get_cached_preview_bytes(item.path)
    if cached is not None:
        return cached

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        meta = await fetch_sequence_meta(client, item.path)
        base_dir = urllib.parse.unquote(meta.path).strip("/")
        target_num = meta.total_steps * 2 + 1 if meta.total_steps > 0 else 1
        url = f"{BASE_URL}{encode_cbyd_path(f'{base_dir}/{target_num}.png')}"
        resp = await client.get(url)
        if resp.status_code == 404 and target_num != 1:
            url = f"{BASE_URL}{encode_cbyd_path(f'{base_dir}/1.png')}"
            resp = await client.get(url)
        if resp.status_code != 200:
            return None
        data = resp.content
        PREVIEW_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _preview_cache_file(item.path).write_bytes(data)
        return data


def make_safe_filename(item: CatalogItem) -> str:
    """Generate clean filename like Name_Author_Year.html."""
    parts = [p for p in [item.name, item.author, item.year] if p]
    raw = "_".join(parts) if parts else item.full
    cleaned = re.sub(r'[<>:"/\\|?*\s]+', "_", raw).strip("_.")
    return f"{cleaned}.html"
