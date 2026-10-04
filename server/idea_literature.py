"""Public-source reading and a versioned, shared Idea group source library.

Discovery supplies URLs from native web receipts. This module fetches actual
bytes; generated snippets never become quotation evidence. It has no provider,
credentials, browser profile, or experiment execution capability.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import inspect
import ipaddress
import json
from pathlib import Path
import re
import socket
import sqlite3
import sys
import time
from urllib.parse import urljoin, urlsplit, urlunsplit
from uuid import uuid4

import httpx
from idea_source_archive import RawArchiveError, RawSourceArchive

PARSER_VERSION = "idea-public-text-v3"
MAX_DOWNLOAD = 10 * 1024 * 1024
MAX_TEXT = 400_000
MAX_SOURCE_BYTES = 18_000
MAX_PACKET_BYTES = 100 * 1024
CACHE_SECONDS = 24 * 3600
LEASE_SECONDS = 120
GAP_MARKER = "\n[... omitted source text; not a continuous quotation ...]\n"


class ReadError(ValueError):
    """A fixed, content-free reason suitable for a visible coverage receipt."""
    def __init__(self, message, *, raw_document=None, content_hash=None):
        super().__init__(message)
        self.raw_document, self.content_hash = raw_document, content_hash


def public_url(value: str) -> str:
    try:
        if not isinstance(value, str) or len(value) > 4096 or re.search(r"[\x00-\x20\x7f\\]", value):
            raise ValueError()
        p = urlsplit(value)
        host = p.hostname
        if p.scheme not in ("http", "https") or not host or p.username or p.password:
            raise ValueError()
        if p.port not in (None, 80, 443) or "%" in host or host.endswith("."):
            raise ValueError()
        host = host.encode("idna").decode("ascii").lower()
        if host in ("localhost", "metadata.google.internal") or host.endswith((".local", ".localhost", ".internal")):
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not _global(address):
            raise ValueError()
        authority = f"[{host}]" if ":" in host else host
        if p.port and p.port != (443 if p.scheme == "https" else 80):
            authority += f":{p.port}"
        return urlunsplit((p.scheme, authority, p.path or "/", p.query, ""))
    except (ValueError, UnicodeError):
        raise ReadError("URL is not an allowed public HTTP(S) source") from None


def _global(address):
    mapped = getattr(address, "ipv4_mapped", None)
    six_to_four = getattr(address, "sixtofour", None)
    teredo = getattr(address, "teredo", None)
    if isinstance(address, ipaddress.IPv6Address) and address in ipaddress.ip_network("64:ff9b::/96"):
        return False
    return (address.is_global and not address.is_multicast
            and (mapped is None or _global(mapped))
            and (six_to_four is None or _global(six_to_four))
            and (teredo is None or all(_global(part) for part in teredo)))


async def _resolve(host, port):
    try:
        entries = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM), 5)
        addresses = list(dict.fromkeys(item[4][0] for item in entries))
        if not addresses or any(not _global(ipaddress.ip_address(ip)) for ip in addresses):
            raise ReadError("Source resolves to a non-public address")
        return addresses
    except (OSError, asyncio.TimeoutError):
        raise ReadError("Source DNS lookup failed") from None


async def _download(url: str) -> tuple[bytes, str, str]:
    """Pin every redirect to validated DNS results, retaining TLS hostname checks."""
    url = public_url(url)
    for _ in range(6):
        parsed = httpx.URL(url)
        addresses = await _resolve(parsed.host, parsed.port or (443 if parsed.scheme == "https" else 80))
        last_error = None
        # Prefer IPv4 where a host advertises IPv6 but the machine has no route.
        for address in sorted(addresses, key=lambda ip: ":" in ip)[:3]:
            try:
                async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                    timeout=httpx.Timeout(18, connect=5)) as client:
                    async with client.stream("GET", parsed.copy_with(host=address),
                        headers={"Host": parsed.netloc.decode("ascii"),
                                 "User-Agent": "AgentsDock-Idea-Literature/2.0 (public research reader)",
                                 "Accept": "text/html,application/pdf,text/plain;q=0.8",
                                 "Accept-Encoding": "identity"},
                        extensions={"sni_hostname": parsed.host}) as response:
                        if response.status_code in (301, 302, 303, 307, 308):
                            location = response.headers.get("location")
                            if not location:
                                raise ReadError("Source redirect has no location")
                            url = public_url(urljoin(url, location))
                            break
                        if response.status_code != 200:
                            raise ReadError(f"Source returned HTTP {response.status_code}")
                        if response.headers.get("content-encoding", "identity").strip().lower() not in ("", "identity"):
                            raise ReadError("Source ignored the bounded uncompressed-transfer request")
                        length = response.headers.get("content-length", "")
                        if length.isdigit() and int(length) > MAX_DOWNLOAD:
                            raise ReadError("Source exceeds the 10 MiB reading limit")
                        chunks, size = [], 0
                        async for chunk in response.aiter_raw():
                            size += len(chunk)
                            if size > MAX_DOWNLOAD:
                                raise ReadError("Source exceeds the 10 MiB reading limit")
                            chunks.append(chunk)
                        return b"".join(chunks), response.headers.get("content-type", ""), url
            except httpx.HTTPError:
                last_error = ReadError("Source connection failed or timed out")
                continue
        else:
            raise last_error or ReadError("Source connection failed")
    raise ReadError("Source has too many redirects")


class _PageText(HTMLParser):
    IGNORE = {"script", "style", "noscript", "svg", "canvas", "nav", "footer", "header", "form"}
    BREAKS = {"p", "div", "section", "article", "main", "h1", "h2", "h3", "h4", "li", "br", "tr", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.main, self.title, self.meta = [], [], [], {}
        self.skip, self.in_title, self.in_main = [], False, 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta":
            name = attrs.get("name", attrs.get("property", "")).lower()
            self.meta[name] = attrs.get("content", "")
        if self.skip:
            if tag in self.IGNORE:
                self.skip.append(tag)
            return
        if tag in self.IGNORE:
            self.skip.append(tag)
            return
        if tag in ("main", "article"):
            self.in_main += 1
        if tag == "title":
            self.in_title = True
        if tag in self.BREAKS:
            self._append("\n")

    def handle_endtag(self, tag):
        if self.skip:
            if tag == self.skip[-1]:
                self.skip.pop()
            return
        if tag == "title":
            self.in_title = False
        if tag in self.BREAKS:
            self._append("\n")
        if tag in ("main", "article"):
            self.in_main = max(0, self.in_main - 1)

    def _append(self, text):
        self.parts.append(text)
        if self.in_main:
            self.main.append(text)

    def handle_data(self, data):
        if self.skip:
            return
        if self.in_title:
            self.title.append(data)
        else:
            self._append(data)


def _clean(text):
    lines = [re.sub(r"[^\S\n]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _html(body, content_type, url):
    match = re.search(r"charset\s*=\s*[\"']?([\w-]+)", content_type, re.I)
    encoding = match.group(1) if match else "utf-8"
    try:
        decoded = body.decode(encoding, errors="replace")
    except LookupError:
        decoded = body.decode("utf-8", errors="replace")
    parser = _PageText()
    parser.feed(decoded)
    main = _clean("".join(parser.main))
    text = main if len(main) >= 300 else _clean("".join(parser.parts))
    title = parser.meta.get("citation_title") or _clean("".join(parser.title))
    # HTML may be a complete article, a landing page, or a partial rendering.
    # Never claim paper full text solely from a successful HTTP response.
    return {"title": title[:500], "text": text[:MAX_TEXT],
            "access": "partial",
            "parse_truncated": len(text) > MAX_TEXT,
            "limitations": ["HTML text only; figures, interactive elements, linked supplements and inaccessible sections were not read."],
            "pdf_link": parser.meta.get("citation_pdf_url") or None}


async def _join_owned(task):
    """Repeated cancellation must not abandon a spawning/closing child."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


async def _pdf(body):
    # PDF text extraction runs with an independent timeout/resource boundary.
    launch = asyncio.create_task(asyncio.create_subprocess_exec(sys.executable, str(Path(__file__).resolve()), "--parse-pdf",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL))
    try:
        process = await asyncio.shield(launch)
    except asyncio.CancelledError:
        process = await _join_owned(launch)
        if process.returncode is None:
            process.kill()
        await _join_owned(asyncio.create_task(process.wait()))
        raise
    try:
        output, _ = await asyncio.wait_for(process.communicate(body), 22)
        if process.returncode != 0 or len(output) > 3 * 1024 * 1024:
            raise ReadError("PDF text could not be extracted within the reading limit")
        result = json.loads(output)
        if not isinstance(result, dict) or not isinstance(result.get("text"), str):
            raise ValueError()
        return result
    except (ValueError, asyncio.TimeoutError):
        raise ReadError("PDF text could not be extracted within the reading limit") from None
    finally:
        if process.returncode is None:
            process.kill()
        await _join_owned(asyncio.create_task(process.wait()))


async def _parse(body, content_type, url):
    if body.startswith(b"%PDF-") or "application/pdf" in content_type:
        return await _pdf(body)
    if "html" in content_type or body.lstrip().lower().startswith((b"<!doctype html", b"<html")):
        return _html(body, content_type, url)
    if "text/plain" in content_type:
        text = body.decode("utf-8", errors="replace")
        return {"title": "", "text": text[:MAX_TEXT], "access": "partial", "parse_truncated": len(text) > MAX_TEXT,
                "limitations": ["Plain text resource; completeness and linked materials were not verified."]}
    raise ReadError("Source is not a supported HTML, PDF or plain-text resource")


class SourceLibrary:
    """Atomic source-version cache and expiring, fenced extraction leases.

    Cache entries contain parsed public text, not goal-dependent claims. A new
    source hash or parser version produces a different identity. Failed reads
    are not cached as negative evidence. Short URL freshness is explicit.
    """
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "sources.sqlite3"
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS versions (
                    key TEXT PRIMARY KEY, content_hash TEXT NOT NULL, parser TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS urls (
                    url TEXT NOT NULL, parser TEXT NOT NULL, key TEXT NOT NULL, fetched REAL NOT NULL,
                    receipt TEXT,
                    PRIMARY KEY(url,parser));
                CREATE TABLE IF NOT EXISTS leases (
                    url TEXT NOT NULL, parser TEXT NOT NULL, owner TEXT NOT NULL, expires REAL NOT NULL,
                    PRIMARY KEY(url,parser));
            """)
            if "receipt" not in {row[1] for row in db.execute("PRAGMA table_info(urls)")}:
                db.execute("ALTER TABLE urls ADD COLUMN receipt TEXT")

    @contextmanager
    def _db(self):
        connection = sqlite3.connect(self.path, timeout=3)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def cached(self, url):
        with self._db() as db:
            row = db.execute("SELECT u.receipt FROM urls u WHERE u.url=? AND u.parser=? AND u.fetched>? AND u.receipt IS NOT NULL",
                             (url, PARSER_VERSION, time.time() - CACHE_SECONDS)).fetchone()
        return json.loads(row[0]) if row else None

    def acquire(self, url, owner):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM leases WHERE url=? AND parser=? AND expires<?", (url, PARSER_VERSION, time.time()))
            return db.execute("INSERT OR IGNORE INTO leases VALUES (?,?,?,?)",
                              (url, PARSER_VERSION, owner, time.time() + LEASE_SECONDS)).rowcount == 1

    def version(self, key):
        with self._db() as db:
            row = db.execute("SELECT data FROM versions WHERE key=? AND parser=?", (key, PARSER_VERSION)).fetchone()
        return json.loads(row[0]) if row else None

    def search(self, brief, limit=6):
        """Bounded keyword recall plus a small catalog for native semantic review.

        These are reusable source versions, not cached goal-dependent answers.
        The native literature role assesses relevance before making a claim.
        """
        goal = " ".join(str(brief.get(key, "")) for key in ("goal", "hypothesis", "reading_focus"))
        terms = list(dict.fromkeys(re.findall(r"[A-Za-z][A-Za-z0-9-]{1,}", goal.lower())))[:30]
        for chunk in re.findall(r"[\u4e00-\u9fff]+", goal):
            terms.extend(chunk[i:i + 2] for i in range(min(len(chunk) - 1, 8)))
        ranked = []
        with self._db() as db:
            for row in db.execute("SELECT data FROM versions WHERE parser=? ORDER BY rowid DESC LIMIT 50", (PARSER_VERSION,)):
                source = json.loads(row[0])
                body = source["text"].lower()
                title = source["title"].lower()
                score = sum((3 if term in title else 0) + (1 if term in body else 0) for term in terms[:40])
                start = next((max(0, body.index(term) - 200) for term in terms if term in body), 0)
                ranked.append((score, {key: source[key] for key in ("id", "title", "url", "content_hash", "parser_version", "retrieved_at")} | {
                    "excerpt": source["text"][start:start + 1500],
                    "coverage": "Catalog excerpt for relevance assessment; retrieve the versioned source before quoting.",
                    "match": "keyword" if score else "recent_catalog_for_semantic_review"}))
        return [item for _, item in sorted(ranked, key=lambda pair: -pair[0])[:max(0, min(limit, 6))]]

    def release(self, url, owner):
        with self._db() as db:
            db.execute("DELETE FROM leases WHERE url=? AND parser=? AND owner=?", (url, PARSER_VERSION, owner))

    def publish(self, url, owner, result, extraction=None):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            lease = db.execute("SELECT owner,expires FROM leases WHERE url=? AND parser=?", (url, PARSER_VERSION)).fetchone()
            if not lease or lease[0] != owner or lease[1] < time.time():
                raise ReadError("Source reading lease expired; a stale result was not published")
            if extraction:
                task = db.execute("SELECT owner,expires FROM leases WHERE url=? AND parser=?", (extraction, PARSER_VERSION)).fetchone()
                if not task or task[0] != owner or task[1] < time.time():
                    raise ReadError("Source extraction lease expired; a stale result was not published")
            db.execute("INSERT OR IGNORE INTO versions VALUES (?,?,?,?)", (result["id"], result["content_hash"], PARSER_VERSION,
                       json.dumps(result, ensure_ascii=False)))
            db.execute("INSERT OR REPLACE INTO urls(url,parser,key,fetched,receipt) VALUES (?,?,?,?,?)",
                       (url, PARSER_VERSION, result["id"], time.time(), json.dumps(result, ensure_ascii=False)))
            db.execute("DELETE FROM leases WHERE url=? AND parser=? AND owner=?", (url, PARSER_VERSION, owner))

    async def read(self, url):
        url = public_url(url)
        owner = uuid4().hex
        for _ in range(240):
            cached = self.cached(url)
            if cached:
                return {**cached, "cache_hit": True,
                        "raw_document": cached.get("raw_document", {"status": "not_retained", "reason": "legacy_fetch_did_not_retain_original_bytes"})}
            if self.acquire(url, owner):
                break
            await asyncio.sleep(0.25)
        else:
            raise ReadError("Another literature task is reading this source; retry on continuation")
        raw_document, archive, digest, parse_finished = None, None, None, False
        async def finish_raw(status, *, parse_key=None, error=None):
            if archive is None:
                return
            task = asyncio.create_task(asyncio.to_thread(archive.finish_parse, raw_document["fetch_id"],
                                       status=status, parse_key=parse_key, error=error))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await _join_owned(task)
                raise
        try:
            body, mime, actual = await asyncio.wait_for(_download(url), 35)
            digest = hashlib.sha256(body).hexdigest()
            def retain_raw():
                current = RawSourceArchive(self.root)
                return current, current.retain(body, requested_url=url, final_url=actual, mime=mime, parse_version=PARSER_VERSION)
            retention = asyncio.create_task(asyncio.to_thread(retain_raw))
            try:
                archive, raw_document = await asyncio.shield(retention)
            except asyncio.CancelledError:
                try:
                    archive, raw_document = await _join_owned(retention)
                except (RawArchiveError, sqlite3.Error, OSError):
                    pass
                raise
            except (RawArchiveError, sqlite3.Error, OSError):
                raw_document = {"status": "not_retained", "reason": "original_byte_archive_unavailable"}
            # MIME/charset affect parsing of identical bytes. URL-specific
            # metadata is resolved afresh and stored in a separate fetch receipt.
            key = hashlib.sha256((digest + ":" + PARSER_VERSION + ":" + mime.lower()).encode()).hexdigest()
            identity, extraction = "paper_" + key[:24], "content:" + key
            acquired = False
            def receipt(parsed):
                access = parsed.get("parsed_access", parsed["access"])
                is_abstract = urlsplit(actual).hostname in ("arxiv.org", "export.arxiv.org") and "/abs/" in urlsplit(actual).path
                return {**parsed, "id": identity, "url": actual, "requested_url": url,
                        "parsed_access": access, "access": "abstract" if is_abstract else access,
                        "pdf_url": urljoin(actual, parsed["pdf_link"]) if parsed.get("pdf_link") else None,
                        "content_hash": digest, "parser_version": PARSER_VERSION,
                        "raw_document": raw_document,
                        "retrieved_at": datetime.now(timezone.utc).isoformat(),
                        "cache_freshness_seconds": CACHE_SECONDS, "cache_hit": False}
            try:
                for _ in range(160):
                    known = self.version(identity)
                    if known:
                        result = {**receipt(known), "parse_cache_hit": True}
                        await finish_raw("parsed", parse_key=identity)
                        parse_finished = True
                        self.publish(url, owner, result)
                        return result
                    if self.acquire(extraction, owner):
                        acquired = True
                        break
                    await asyncio.sleep(0.25)
                else:
                    raise ReadError("Another task is extracting this source version; retry on continuation")
                parsed = await _parse(body, mime, actual)
                if parsed.get("extracted_characters", len(parsed["text"].strip())) < 80:
                    raise ReadError("Source contains too little extractable text; scanned or blocked content may need another version")
                result = receipt(parsed)
                await finish_raw("parsed", parse_key=identity)
                parse_finished = True
                self.publish(url, owner, result, extraction)
                return result
            finally:
                if acquired:
                    self.release(extraction, owner)
        except asyncio.CancelledError:
            if archive is not None and not parse_finished:
                try:
                    await _join_owned(asyncio.create_task(finish_raw("cancelled", error="parse_cancelled")))
                except (RawArchiveError, sqlite3.Error, OSError):
                    pass
            raise
        except (ReadError, asyncio.TimeoutError, RawArchiveError, sqlite3.Error, OSError) as error:
            if archive is not None and not parse_finished:
                try:
                    await finish_raw("failed", error="parse_or_publication_failed")
                except (RawArchiveError, sqlite3.Error, OSError):
                    pass
            message = str(error) if isinstance(error, ReadError) else "Source reading or original-file receipt could not be completed"
            raise ReadError(message, raw_document=raw_document, content_hash=digest) from None
        finally:
            self.release(url, owner)


def _utf8_prefix(text, size):
    return text.encode("utf-8")[:size].decode("utf-8", errors="ignore")


def project_text(text, brief, max_bytes):
    """Cover the abstract/start plus relevant later passages without hiding gaps."""
    if len(text.encode("utf-8")) <= max_bytes:
        return text, [{"start": 0, "end": len(text)}]
    if max_bytes < 600:
        prefix = _utf8_prefix(text, max_bytes)
        return prefix, [{"start": 0, "end": len(prefix)}]
    prefix = _utf8_prefix(text, max_bytes // 3)
    spans = [(0, len(prefix))]
    goal = " ".join(str(brief.get(k, "")) for k in ("goal", "hypothesis", "constraints", "feedback", "reading_focus"))
    # English terms and CJK bigrams support useful projections of both languages.
    words = {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9-]{3,}", goal)}
    for chunk in re.findall(r"[\u4e00-\u9fff]+", goal):
        words.update(chunk[i:i + 2] for i in range(min(len(chunk) - 1, 80)))
    scored = []
    for start in range(len(prefix), len(text), 1200):
        passage = text[start:start + 2400].lower()
        score = sum(min(passage.count(term), 4) for term in words)
        # Conclusion/end coverage is useful even across goal/source languages.
        score += 2 if re.search(r"conclusion|limitation|discussion|conclud", passage) else 0
        scored.append((score, start, min(start + 2400, len(text))))
    remaining = max_bytes - len(prefix.encode("utf-8"))
    for _, start, end in sorted(scored, key=lambda item: (-item[0], item[1])):
        if any(start < b and end > a for a, b in spans):
            continue
        piece = _utf8_prefix(text[start:end], remaining - len(GAP_MARKER.encode("utf-8")))
        if len(piece) < 100:
            break
        spans.append((start, start + len(piece)))
        remaining -= len(piece.encode("utf-8")) + len(GAP_MARKER.encode("utf-8"))
        if remaining < 400:
            break
    spans.sort()
    return GAP_MARKER.join(text[a:b] for a, b in spans), [{"start": a, "end": b} for a, b in spans]


async def _event(callback, **event):
    if callback:
        value = callback({"agent": "literature", **event})
        if inspect.isawaitable(value):
            await value


def _arxiv_versions(url):
    parsed = urlsplit(url)
    match = re.fullmatch(r"/(?:abs|pdf|html)/(\d{4}\.\d{4,5}(?:v\d+)?)(?:\.pdf)?", parsed.path)
    if parsed.hostname in ("arxiv.org", "export.arxiv.org") and match:
        identity = match.group(1)
        return [f"https://arxiv.org/html/{identity}", f"https://arxiv.org/pdf/{identity}", f"https://arxiv.org/abs/{identity}"]
    return [url]


async def retrieve_papers(candidates, brief, on_event=None, *, library_root):
    library = SourceLibrary(library_root)
    limits = brief.get("_retrieval_limits", {})
    count = min(max(int(limits.get("max_sources", 6)), 0), 12)
    remaining = min(max(int(limits.get("max_packet_bytes", MAX_PACKET_BYTES)), 0), MAX_PACKET_BYTES)
    result = {"sources": [], "papers": [], "coverage_gaps": [], "usage": {"reads": 0, "cache_hits": 0}}
    seen = set()
    for candidate in candidates[:24]:
        if len(result["papers"]) >= count:
            break
        title = str(candidate.get("title", "Untitled source"))[:500]
        try:
            url = public_url(candidate.get("url", ""))
        except ReadError as error:
            result["coverage_gaps"].append(f"{title}: {error}")
            continue
        versions = _arxiv_versions(url)
        if versions[0] in seen:
            continue
        seen.add(versions[0])
        parsed, failures, retained_failures = None, [], []
        for version in versions:
            await _event(on_event, type="source_read_started", summary=f"Reading {title}", url=version)
            try:
                parsed = await library.read(version)
                result["usage"]["cache_hits" if parsed["cache_hit"] else "reads"] += 1
                break
            except (ReadError, OSError) as error:
                reason = str(error) if isinstance(error, ReadError) else "Local source library is unavailable"
                failures.append(reason)
                if isinstance(error, ReadError) and error.raw_document is not None:
                    retained_failures.append({"url": version, "content_hash": error.content_hash, "raw_document": error.raw_document})
                await _event(on_event, type="source_read_unavailable", summary=reason, url=version)
        if parsed and parsed.get("pdf_url") and parsed["access"] != "full_text":
            # Bibliographic metadata supplies a paper link; validate it exactly as
            # a discovery URL. A failed PDF never destroys the readable landing page.
            try:
                paper_url = public_url(parsed["pdf_url"])
                if paper_url != parsed["url"] and paper_url not in seen:
                    seen.add(paper_url)
                    await _event(on_event, type="source_read_started", summary=f"Reading linked paper: {title}", url=paper_url)
                    full = await library.read(paper_url)
                    result["usage"]["cache_hits" if full["cache_hit"] else "reads"] += 1
                    if len(full["text"]) > len(parsed["text"]):
                        parsed = full
            except (ReadError, OSError) as error:
                reason = str(error) if isinstance(error, ReadError) else "Linked source library read failed"
                failures.append(reason)
                if isinstance(error, ReadError) and error.raw_document is not None:
                    retained_failures.append({"url": paper_url, "content_hash": error.content_hash, "raw_document": error.raw_document})
                await _event(on_event, type="source_read_unavailable", summary=reason, url=url)
        if not parsed:
            reason = "; ".join(dict.fromkeys(failures)) or "No readable version found"
            result["papers"].append({"id": "unavailable_" + hashlib.sha256(url.encode()).hexdigest()[:20],
                "title": title, "url": url, "retrieved_at": datetime.now(timezone.utc).isoformat(), "content_hash": None,
                "parser_version": PARSER_VERSION, "access": "unavailable", "coverage": {"kind": "unavailable", "limitations": [reason]},
                "cache_hit": False, "status": "unavailable", "error": reason,
                "raw_fetches": retained_failures})
            result["coverage_gaps"].append(f"{title}: {reason}; this is an access gap, not evidence that the work does not exist.")
            continue
        packet, spans = project_text(parsed["text"], brief, min(MAX_SOURCE_BYTES, remaining))
        remaining -= len(packet.encode("utf-8"))
        if not packet:
            result["coverage_gaps"].append(f"{title}: source found but this round's text budget is exhausted.")
            break
        truncated = parsed["parse_truncated"] or sum(s["end"] - s["start"] for s in spans) < len(parsed["text"])
        limitations = list(parsed["limitations"])
        if parsed.get("raw_document", {}).get("status") != "retained":
            limitations.append("Original downloaded bytes were not retained; saved parsed text cannot reconstruct the original file.")
        if truncated:
            limitations.append("Only the declared text spans entered this task; omitted sections require a targeted follow-up read.")
        if failures:
            limitations.append("Some alternate versions were inaccessible: " + "; ".join(dict.fromkeys(failures)))
        coverage = {"kind": "partial" if truncated else parsed["access"], "characters_total": len(parsed["text"]),
                    "characters_in_packet": sum(s["end"] - s["start"] for s in spans), "truncated": truncated,
                    "limitations": limitations, "spans": spans}
        paper = {key: parsed[key] for key in ("id", "url", "retrieved_at", "content_hash", "parser_version", "cache_hit")}
        # Evidence points into a frozen projection. Targeted rereading may reuse
        # full source bytes but must never change text under an existing ID.
        packet_hash = hashlib.sha256((parsed["id"] + "\n" + packet + "\n" + json.dumps(spans)).encode()).hexdigest()
        paper["id"] = "paper_" + packet_hash[:24]
        paper["source_version_id"] = parsed["id"]
        paper["raw_document"] = parsed.get("raw_document", {"status": "not_retained", "reason": "legacy_fetch_did_not_retain_original_bytes"})
        if retained_failures:
            paper["raw_fetches"] = retained_failures
        paper.update({"title": parsed["title"] or title, "access": "partial" if truncated else parsed["access"],
                      "coverage": coverage, "status": "read", "error": None})
        provenance = {**paper, "gap_marker": GAP_MARKER, "requested_url": url,
                      "cache_freshness_seconds": CACHE_SECONDS, "discovery": {
                          key: candidate[key] for key in ("ref_id", "query", "tool_call_id") if key in candidate}}
        result["sources"].append({"id": paper["id"], "title": paper["title"], "uri": parsed["url"],
                                 "text": packet, "provenance": provenance, "coverage": coverage})
        result["papers"].append(paper)
        if truncated or parsed["access"] != "full_text":
            result["coverage_gaps"].append(f"{paper['title']}: {paper['access']} coverage; conclusions about omitted content remain unverified.")
        await _event(on_event, type="source_read_completed", summary=f"{'Reused source library' if parsed['cache_hit'] else 'Read source'}: {paper['title']} ({paper['access']})",
                     url=paper["url"], source_id=paper["id"])
    return result


def _parse_pdf_process():
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (18, 18))
    except ImportError:
        pass
    from io import BytesIO
    from pypdf import PdfReader
    reader = PdfReader(BytesIO(sys.stdin.buffer.read(MAX_DOWNLOAD + 1)), strict=False)
    if reader.is_encrypted:
        raise ValueError("encrypted PDF")
    pieces, limitations, length = [], ["Text extraction only; figures, image tables, equation layout and scanned text were not visually inspected."], 0
    total = len(reader.pages)
    pages_read, extracted_characters, empty_pages = 0, 0, 0
    for index, page in enumerate(reader.pages):
        if index >= 150 or length >= MAX_TEXT:
            break
        text = page.extract_text() or ""
        extracted_characters += len(text.strip())
        piece = f"[PDF page {index + 1}]\n{text}\n"
        pieces.append(piece[:MAX_TEXT - length])
        length += len(pieces[-1])
        pages_read += 1
        if not text.strip():
            empty_pages += 1
            limitations.append(f"Page {index + 1} has no extractable text.")
    truncated = pages_read < total or length >= MAX_TEXT
    title = str((reader.metadata or {}).get("/Title", ""))[:500]
    json.dump({"title": title, "text": "\n".join(pieces), "access": "partial" if truncated or empty_pages else "full_text",
               "extracted_characters": extracted_characters, "parse_truncated": truncated, "limitations": limitations}, sys.stdout, ensure_ascii=False)


if __name__ == "__main__" and sys.argv[1:] == ["--parse-pdf"]:
    _parse_pdf_process()
