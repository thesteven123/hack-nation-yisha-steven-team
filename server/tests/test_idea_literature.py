"""Source boundaries, real text provenance, reuse fencing, and coverage contracts."""
import asyncio
from io import BytesIO
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch
from types import SimpleNamespace

import idea_literature as reader


BODY = b"<html><title>Source A</title><script>secret instruction</script><main><p>" + b"Observed result with an important limitation. " * 30 + b"</p></main></html>"


class URLTests(unittest.TestCase):
    def test_only_public_web_urls(self):
        for value in ("file:///etc/passwd", "http://127.0.0.1/a", "https://[::1]/", "http://169.254.169.254/",
                      "http://user:pass@example.org/", "https://example.org:8000/a", "https://foo.local/a",
                      "https://example.org/\nsecret", "https://example.org\\@localhost/", "https://[::ffff:127.0.0.1]/"):
            with self.subTest(url=value), self.assertRaises(reader.ReadError):
                reader.public_url(value)
        self.assertEqual(reader.public_url("https://arxiv.org/abs/1706.03762#page2"), "https://arxiv.org/abs/1706.03762")
        for value in ("https://[64:ff9b::7f00:1]/", "https://[2002:7f00:1::]/"):
            with self.assertRaises(reader.ReadError):
                reader.public_url(value)

    def test_projection_declares_every_omitted_span(self):
        text = "Introduction. " * 1000 + "\nDiversity prevents collapse under these assumptions. " * 1500
        projected, spans = reader.project_text(text, {"goal": "diversity collapse assumptions"}, 8000)
        self.assertLessEqual(len(projected.encode()), 8000)
        self.assertEqual(projected, reader.GAP_MARKER.join(text[s["start"]:s["end"]] for s in spans))
        self.assertGreater(len(spans), 1)
        self.assertIn("Diversity prevents", projected)
        self.assertLess(sum(s["end"] - s["start"] for s in spans), len(text))

    def test_multibyte_budget_does_not_corrupt_text(self):
        text = "有意义的证据与研究假设。" * 4000
        projected, spans = reader.project_text(text, {"goal": "研究假设"}, 7000)
        self.assertLessEqual(len(projected.encode()), 7000)
        self.assertNotIn("\ufffd", projected)

    def test_html_drops_scripts_and_reports_partial_coverage(self):
        result = reader._html(BODY, "text/html;charset=UTF-8", "https://example.org/paper")
        self.assertEqual(result["title"], "Source A")
        self.assertNotIn("secret instruction", result["text"])
        self.assertIn("Observed result", result["text"])
        self.assertEqual(result["access"], "partial")


class ReadingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.download = self.enterContext(patch.object(reader, "_download", AsyncMock(return_value=(BODY, "text/html", "https://example.org/paper"))))

    async def test_shared_cache_reuses_actual_text_and_deduplicates_concurrent_readers(self):
        library = reader.SourceLibrary(self.directory)
        async def download(url):
            await asyncio.sleep(0.03)
            return BODY, "text/html", url
        self.download.side_effect = download
        first, second = await asyncio.gather(library.read("https://example.org/paper"), library.read("https://example.org/paper"))
        self.assertEqual(self.download.await_count, 1)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual([first["cache_hit"], second["cache_hit"]], [False, True])
        reopened = reader.SourceLibrary(self.directory)
        result = await reopened.read("https://example.org/paper")
        self.assertTrue(result["cache_hit"])
        self.assertEqual(result["text"], first["text"])

    async def test_library_first_catalog_keeps_versions_and_does_not_claim_synthesis_reuse(self):
        library = reader.SourceLibrary(self.directory)
        source = await library.read("https://example.org/paper")
        catalog = library.search({"goal": "observed limitation"})
        self.assertEqual(catalog[0]["id"], source["id"])
        self.assertEqual(catalog[0]["match"], "keyword")
        self.assertIn("before quoting", catalog[0]["coverage"])
        alternate = library.search({"goal": "未出现的概念"})
        self.assertEqual(alternate[0]["match"], "recent_catalog_for_semantic_review")
        self.assertEqual(self.download.await_count, 1)

    async def test_content_extraction_key_deduplicates_different_urls(self):
        library = reader.SourceLibrary(self.directory)
        original = reader._parse
        async def parse(*args):
            await asyncio.sleep(0.03)
            return await original(*args)
        with patch.object(reader, "_parse", AsyncMock(side_effect=parse)) as parsing:
            a, b = await asyncio.gather(library.read("https://example.org/a"), library.read("https://example.org/b"))
        self.assertEqual(parsing.await_count, 1)
        self.assertEqual(a["id"], b["id"])

    async def test_equal_bytes_keep_each_fetch_url_and_relative_metadata(self):
        library = reader.SourceLibrary(self.directory)
        body = BODY.replace(b"<title>", b'<meta name="citation_pdf_url" content="paper.pdf"><title>')
        async def download(url):
            return body, "text/html", url
        self.download.side_effect = download
        a = await library.read("https://first.example/a/index.html")
        b = await library.read("https://second.example/b/index.html")
        cached = await library.read("https://second.example/b/index.html")
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(b["pdf_url"], "https://second.example/b/paper.pdf")
        self.assertEqual(cached["url"], "https://second.example/b/index.html")
        self.assertEqual(cached["requested_url"], cached["url"])
        self.assertEqual(cached["pdf_url"], b["pdf_url"])

    async def test_targeted_projection_changes_packet_identity_preserving_source_version(self):
        long = b"<main>" + b"Introduction filler. " * 1000 + b"Diversity result. " * 4000 + b"Calibration assumptions. " * 4000 + b"</main>"
        self.download.return_value = (long, "text/html", "https://example.org/paper")
        a = await reader.retrieve_papers([{"url": "https://example.org/paper"}], {"goal": "diversity"}, library_root=self.directory)
        b = await reader.retrieve_papers([{"url": "https://example.org/paper"}], {"goal": "calibration"}, library_root=self.directory)
        self.assertNotEqual(a["sources"][0]["id"], b["sources"][0]["id"])
        self.assertEqual(a["papers"][0]["source_version_id"], b["papers"][0]["source_version_id"])
        self.assertTrue(b["papers"][0]["cache_hit"])

    async def test_changed_version_and_parser_get_new_ids(self):
        library = reader.SourceLibrary(self.directory)
        before = await library.read("https://example.org/paper")
        with library._db() as db:
            db.execute("UPDATE urls SET fetched=0")
        self.download.return_value = (BODY.replace(b"Observed", b"Contrary"), "text/html", "https://example.org/paper")
        after = await library.read("https://example.org/paper")
        self.assertNotEqual(before["id"], after["id"])
        self.assertNotEqual(before["content_hash"], after["content_hash"])
        self.assertIn("Observed", library.version(before["id"])["text"])
        with patch.object(reader, "PARSER_VERSION", "different-parser"):
            changed_parser = await library.read("https://example.org/paper")
        self.assertNotEqual(after["id"], changed_parser["id"])

    async def test_failure_does_not_become_cached_negative_evidence(self):
        library = reader.SourceLibrary(self.directory)
        self.download.side_effect = reader.ReadError("Source returned HTTP 403")
        with self.assertRaises(reader.ReadError):
            await library.read("https://example.org/paper")
        self.download.side_effect = None
        recovered = await library.read("https://example.org/paper")
        self.assertFalse(recovered["cache_hit"])
        self.assertEqual(self.download.await_count, 2)

    async def test_stale_lease_cannot_replace_source(self):
        library = reader.SourceLibrary(self.directory)
        url = "https://example.org/paper"
        self.assertTrue(library.acquire(url, "old"))
        with library._db() as db:
            db.execute("UPDATE leases SET expires=0")
        self.assertTrue(library.acquire(url, "new"))
        with self.assertRaises(reader.ReadError):
            library.publish(url, "old", {})
        library.release(url, "old")
        self.assertFalse(library.acquire(url, "third"))

    async def test_receipts_use_downloaded_text_not_discovery_snippets(self):
        events = []
        result = await reader.retrieve_papers([{"title": "A", "url": "https://example.org/paper", "snippet": "Fabricated result"}],
            {"goal": "research"}, events.append, library_root=self.directory)
        source, paper = result["sources"][0], result["papers"][0]
        self.assertIn("Observed result", source["text"])
        self.assertNotIn("Fabricated", source["text"])
        self.assertEqual(source["id"], paper["id"])
        self.assertEqual(source["provenance"]["content_hash"], paper["content_hash"])
        self.assertEqual(paper["status"], "read")
        self.assertTrue(result["coverage_gaps"])
        self.assertEqual(events[-1]["type"], "source_read_completed")

    async def test_failed_fulltext_falls_back_to_abstract_honestly(self):
        async def download(url):
            if "/abs/" not in url:
                raise reader.ReadError("Source returned HTTP 404")
            return BODY, "text/html", url
        self.download.side_effect = download
        result = await reader.retrieve_papers([{"title": "A", "url": "https://arxiv.org/abs/1706.03762"}],
            {"goal": "research"}, library_root=self.directory)
        self.assertEqual(self.download.await_count, 3)
        self.assertEqual(result["papers"][0]["access"], "abstract")
        self.assertIn("inaccessible", " ".join(result["papers"][0]["coverage"]["limitations"]))

    async def test_all_access_failures_leave_explicit_unavailable_receipts(self):
        self.download.side_effect = reader.ReadError("Source returned HTTP 403")
        result = await reader.retrieve_papers([{"title": "A", "url": "https://example.org/paper"}],
            {"goal": "research"}, library_root=self.directory)
        self.assertEqual(result["sources"], [])
        self.assertEqual(result["papers"][0]["access"], "unavailable")
        self.assertIn("not evidence", result["coverage_gaps"][0])

    async def test_cancellation_releases_only_its_lease(self):
        library = reader.SourceLibrary(self.directory)
        entered = asyncio.Event()
        async def pending(url):
            entered.set()
            await asyncio.Future()
        self.download.side_effect = pending
        task = asyncio.create_task(library.read("https://example.org/paper"))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(library.acquire("https://example.org/paper", "replacement"))

    async def test_dns_mixed_public_private_is_rejected(self):
        loop = asyncio.get_running_loop()
        with patch.object(loop, "getaddrinfo", AsyncMock(return_value=[
            (2, 1, 6, "", ("8.8.8.8", 443)), (2, 1, 6, "", ("127.0.0.1", 443))])):
            with self.assertRaises(reader.ReadError):
                await reader._resolve("example.org", 443)


class PDFTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_cancellation_joins_spawning_child(self):
        entered, release = asyncio.Event(), asyncio.Event()
        process = SimpleNamespace(returncode=None, kill=Mock(), wait=AsyncMock(return_value=-9))
        async def launch(*args, **kwargs):
            entered.set()
            await release.wait()
            return process
        with patch.object(reader.asyncio, "create_subprocess_exec", side_effect=launch):
            task = asyncio.create_task(reader._pdf(b"%PDF-1.4"))
            await entered.wait()
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()

    async def test_blank_page_labels_are_not_readable_evidence(self):
        from pypdf import PdfWriter
        writer = PdfWriter()
        for _ in range(10):
            writer.add_blank_page(width=600, height=800)
        buffer = BytesIO()
        writer.write(buffer)
        parsed = await reader._pdf(buffer.getvalue())
        self.assertEqual(parsed["extracted_characters"], 0)
        self.assertEqual(parsed["access"], "partial")
        with tempfile.TemporaryDirectory() as root, patch.object(reader, "_download", AsyncMock(return_value=(buffer.getvalue(), "application/pdf", "https://example.org/paper"))):
            with self.assertRaises(reader.ReadError):
                await reader.SourceLibrary(root).read("https://example.org/paper")

    async def test_real_pdf_subprocess_extracts_page_text(self):
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
        writer = PdfWriter()
        page = writer.add_blank_page(width=600, height=800)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        content = DecodedStreamObject()
        content.set_data(b"BT /F1 12 Tf 30 700 Td (A measured result depends on the reported conditions.) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(content)
        buffer = BytesIO()
        writer.write(buffer)
        parsed = await reader._pdf(buffer.getvalue())
        self.assertIn("A measured result depends", parsed["text"])
        self.assertIn("[PDF page 1]", parsed["text"])
        self.assertEqual(parsed["access"], "full_text")
        self.assertTrue(parsed["limitations"])


if __name__ == "__main__":
    unittest.main()
