import json
import io
from zipfile import ZipFile
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from xiaoetong_assistant.downloads import DownloadPaused, download_file, safe_name

CONTENT = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 4096


class Handler(BaseHTTPRequestHandler):
    etag = '"version-1"'
    ranges = []
    wrong_range = False

    def log_message(self, *_):
        pass

    documents = {}

    def do_GET(self):
        if self.path in type(self).documents:
            content = type(self).documents[self.path]
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return
        if self.path == "/forbidden":
            self.send_response(403)
            self.end_headers()
            return
        if self.path == "/html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html>sign in</html>")
            return
        if self.path == "/playlist":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            self.wfile.write(b"#EXTM3U\n#EXT-X-KEY:METHOD=AES-128\n")
            return
        offset = int(self.headers.get("Range", "bytes=0-").split("=")[1].split("-")[0])
        if offset:
            type(self).ranges.append(offset)
        self.send_response(206 if offset else 200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("ETag", type(self).etag)
        self.send_header("Content-Length", str(len(CONTENT) - offset))
        if offset:
            start = offset + 1 if type(self).wrong_range else offset
            self.send_header("Content-Range", f"bytes {start}-{len(CONTENT)-1}/{len(CONTENT)}")
        self.end_headers()
        try:
            self.wfile.write(CONTENT[offset:])
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


class DownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "video.mp4"
        Handler.etag = '"version-1"'
        Handler.ranges = []
        Handler.wrong_range = False
        self.cancel = threading.Event()

    def tearDown(self):
        self.temp.cleanup()

    def pause_after_first_chunk(self):
        with self.assertRaises(DownloadPaused):
            download_file(self.base + "/file", self.path, self.cancel, lambda *_: self.cancel.set())
        self.assertFalse(self.path.exists())
        self.assertTrue(self.path.with_suffix(".mp4.part").exists())

    def test_streamed_file_exact_bytes(self):
        download_file(self.base + "/file", self.path, self.cancel, lambda *_: None)
        self.assertEqual(self.path.read_bytes(), CONTENT)
        self.assertFalse(self.path.with_suffix(".mp4.part.json").exists())

    def test_pause_resume_range_exact_bytes(self):
        self.pause_after_first_chunk()
        self.cancel.clear()
        download_file(self.base + "/file", self.path, self.cancel, lambda *_: None)
        self.assertTrue(Handler.ranges)
        self.assertEqual(self.path.read_bytes(), CONTENT)

    def test_changed_source_not_appended(self):
        self.pause_after_first_chunk()
        Handler.etag = '"version-2"'
        self.cancel.clear()
        with self.assertRaisesRegex(ValueError, "源文件已变化"):
            download_file(self.base + "/file", self.path, self.cancel, lambda *_: None)
        self.assertFalse(self.path.exists())

    def test_wrong_range_rejected(self):
        self.pause_after_first_chunk()
        Handler.wrong_range = True
        self.cancel.clear()
        with self.assertRaisesRegex(ValueError, "错误的断点"):
            download_file(self.base + "/file", self.path, self.cancel, lambda *_: None)

    def test_no_validator_restarts_without_range(self):
        self.pause_after_first_chunk()
        metadata_path = self.path.with_suffix(".mp4.part.json")
        data = json.loads(metadata_path.read_text())
        data["etag"] = None
        metadata_path.write_text(json.dumps(data))
        self.cancel.clear()
        download_file(self.base + "/file", self.path, self.cancel, lambda *_: None)
        self.assertFalse(Handler.ranges)
        self.assertEqual(self.path.read_bytes(), CONTENT)

    def test_different_url_restarts_without_range(self):
        self.pause_after_first_chunk()
        self.cancel.clear()
        download_file(self.base + "/another-file", self.path, self.cancel, lambda *_: None)
        self.assertFalse(Handler.ranges)
        self.assertEqual(self.path.read_bytes(), CONTENT)

    def test_forbidden_response_not_saved(self):
        with self.assertRaises(PermissionError):
            download_file(self.base + "/forbidden", self.path, self.cancel, lambda *_: None)
        self.assertFalse(self.path.exists())

    def test_html_and_mislabeled_playlist_not_saved(self):
        for endpoint in ("/html", "/playlist"):
            with self.assertRaises(ValueError):
                download_file(self.base + endpoint, self.path, self.cancel, lambda *_: None)
            self.assertFalse(self.path.exists())

    def test_existing_file_not_overwritten(self):
        self.path.write_bytes(b"user-data")
        with self.assertRaises(FileExistsError):
            download_file(self.base + "/file", self.path, self.cancel, lambda *_: None)
        self.assertEqual(self.path.read_bytes(), b"user-data")

    def test_windows_names(self):
        self.assertEqual(safe_name("CON"), "_CON")
        self.assertEqual(safe_name("../../课:程?.mp4"), "_.._课_程_.mp4")
        self.assertNotIn("/", safe_name("../课程"))


    def test_pdf_and_word_download_exact_bytes_with_validation(self):
        from unittest.mock import patch
        from xiaoetong_assistant.documents import download_document
        buffer = io.BytesIO()
        with ZipFile(buffer, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("word/document.xml", "<document>fixture</document>")
        samples = {"pdf": b"%PDF-1.7\nfixture", "docx": buffer.getvalue()}
        try:
            for extension, content in samples.items():
                url = "/sample." + extension
                Handler.documents[url] = content
                destination = Path(self.temp.name) / ("sample." + extension)
                info = {"file_url": self.base + url}
                with patch("xiaoetong_assistant.documents.document_url", lambda url: url):
                    download_document(info, destination, self.cancel)
                self.assertEqual(destination.read_bytes(), content)
        finally:
            Handler.documents.clear()

    def test_mismatched_document_never_publishes_final_or_invalid_partial(self):
        from xiaoetong_assistant.documents import validate_document
        with self.assertRaises(ValueError):
            download_file(self.base + "/file", self.path, self.cancel, lambda *_: None,
                          validate=lambda path: validate_document(path, "pdf"))
        self.assertFalse(self.path.exists())
        self.assertFalse(self.path.with_suffix(".mp4.part").exists())
        self.assertFalse(self.path.with_suffix(".mp4.part.json").exists())


if __name__ == "__main__":
    unittest.main()
