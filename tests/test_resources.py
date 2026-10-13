import tempfile
import threading
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from zipfile import ZipFile

from xiaoetong_assistant.app import Application
from xiaoetong_assistant.documents import document_format, validate_document, download_document
from xiaoetong_assistant.native import NativeResource
from xiaoetong_assistant.service import VideoCatalog, selected_resources
from xiaoetong_assistant.media import select_stream
from xiaoetong_assistant.jobs import download_resource


def resource(id, kind=3):
    return NativeResource("app", "user", "course", id, id, "Course", kind, True, False)


class ResourceTests(unittest.TestCase):
    def test_http_attachment_uses_https_without_changing_signature(self):
        with patch("xiaoetong_assistant.documents.download_file") as download:
            download_document({"file_name":"Notes.pdf","file_url":"http://example.invalid/notes.pdf?signature=keep"},Path("Notes.pdf"),threading.Event())
        self.assertEqual(download.call_args.args[0], "https://example.invalid/notes.pdf?signature=keep")

    def test_sidebar_download_resolves_current_course_material(self):
        client = Mock()
        item = resource("notes.pdf", 51)
        item.source = "courseware"
        client.courseware_document.return_value = {"file_name":"notes.pdf","file_url":"https://example.invalid/notes.pdf"}
        with tempfile.TemporaryDirectory() as folder, patch("xiaoetong_assistant.jobs.download_document") as download:
            path, info = download_resource(client,item,Path(folder),threading.Event())
        client.document.assert_not_called()
        client.courseware_document.assert_called_once_with(**item.video_identity())
        self.assertEqual(path.name,"notes.pdf")
        download.assert_called_once()

    def test_incremental_catalog_displays_roots_early_and_keeps_user_checks(self):
        root = tk.Tk()
        root.withdraw()
        app = Application(root, connect=False)
        try:
            courses = [{"app_id": "app", "resource_id": "course", "title": "Course"},
                       {"app_id": "app", "resource_id": "another", "title": "Another Course"}]
            app.events.put(("catalog", VideoCatalog(course_rows=courses)))
            app.poll()
            first, second = app.table.get_children()
            self.assertEqual(app.table.get_children(first), ())
            video, document = resource("video"), resource("document", 51)
            app.events.put(("catalog_update", VideoCatalog(course_rows=courses, resources=[video])))
            app.poll()
            app.set_checked([first])
            app.table.item(first, open=True)
            app.events.put(("scan", 2, 2))
            app.events.put(("catalog_update", VideoCatalog(course_rows=courses, resources=[video, document])))
            app.poll()
            self.assertEqual(app.table.get_children(), (first, second))
            self.assertEqual(app.checked, {video.key})
            self.assertTrue(app.table.item(first, "open"))
            self.assertEqual(len(app.table.get_children(first)), 2)
            self.assertEqual(str(app.table.item(first, "image")[0]), str(app.check_images["partial"]))
            self.assertEqual(app.table.set(second, "state"), "读取中…")
            self.assertEqual(app.summary.get(), "1 个视频 · 1 份文档")
            app.events.put(("scan_done",))
            app.poll()
            self.assertEqual(app.table.set(second, "state"), "")
        finally:
            app.close()

    def test_connection_failure_is_visible_in_empty_tree_and_recovery_replaces_it(self):
        root = tk.Tk()
        root.withdraw()
        app = Application(root, connect=False)
        try:
            app.events.put(("connection", True))
            app.poll()
            self.assertEqual(app.table.item("connection_status", "text"), "等待小鹅通学员客户端登录…")
            app.events.put(("catalog", VideoCatalog(course_rows=[{"app_id": "app", "resource_id": "course", "title": "Course"}])))
            app.poll()
            self.assertFalse(app.table.exists("connection_status"))
            self.assertEqual(len(app.table.get_children()), 1)
        finally:
            app.close()

    def test_quality_uses_api_maximum_including_above_1080(self):
        streams = [{"url": "https://example.invalid/v.m3u8", "definition_p": p} for p in ("720P", "2160P", "1080P")]
        self.assertEqual(select_stream({"streams": streams})["definition_p"], "2160P")
        self.assertEqual(select_stream({"streams": streams[:1]})["definition_p"], "720P")
        streams.append({"url": "https://example.invalid/original.mp4", "definition_p": "原画"})
        self.assertEqual(select_stream({"streams": streams})["definition_p"], "原画")

    def test_formats_include_signed_urls_and_uppercase_extensions(self):
        self.assertEqual(document_format({"file_url": "https://example.invalid/abc.PDF?sig=x"}), "pdf")
        self.assertEqual(document_format({"file_name": "notes.DOCX"}), "docx")
        with self.assertRaises(ValueError):
            document_format({"file_name": "slides.pptx"})

    def test_pdf_word_signatures_and_wrong_content(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "sample"
            file.write_bytes(b"%PDF-1.7\nfixture")
            validate_document(file, "pdf")
            file.write_bytes(bytes.fromhex("d0cf11e0a1b11ae1") + b"fixture")
            validate_document(file, "doc")
            with ZipFile(file, "w") as archive:
                archive.writestr("[Content_Types].xml", "<Types/>")
                archive.writestr("word/document.xml", "<document/>")
            validate_document(file, "docx")
            for extension in ("pdf", "doc", "docx"):
                file.write_bytes(b"<html>login</html>")
                with self.assertRaises(ValueError):
                    validate_document(file, extension)

    def test_denied_resource_cannot_download(self):
        client = Mock()
        item = resource("locked")
        item.unlocked = False
        with self.assertRaises(PermissionError):
            download_resource(client, item, Path("output"), threading.Event())
        client.video.assert_not_called()

    def test_tree_course_and_child_selection_deduplicates_and_requires_directory(self):
        root = tk.Tk()
        root.withdraw()
        app = Application(root, connect=False)
        try:
            items = [resource("video"), resource("document", 51)]
            app.events.put(("catalog", VideoCatalog(resources=items)))
            app.poll()
            course = app.table.get_children()[0]
            app.table.selection_set(course, items[0].key)
            app.set_checked([course, items[0].key])
            self.assertEqual(selected_resources(app.table, app.resources), items)
            self.assertEqual(app.folder.get(), "")
            app.update_button()
            self.assertIn("disabled", app.button.state())
            app.folder.set(str(Path.home()))
            self.assertNotIn("disabled", app.button.state())
        finally:
            app.close()


    def test_persistent_checks_cover_multiple_courses_and_partial_course_state(self):
        root = tk.Tk()
        root.withdraw()
        app = Application(root, connect=False)
        try:
            items = [resource("first"), resource("second"), resource("third")]
            items[2].course_id, items[2].course_title = "another-course", "Another Course"
            app.events.put(("catalog", VideoCatalog(resources=items)))
            app.poll()
            first_course, second_course = app.table.get_children()
            app.set_checked([items[0].key])
            self.assertEqual(str(app.table.item(first_course, "image")[0]), str(app.check_images["partial"]))
            app.toggle_checked([second_course])
            app.table.selection_set(items[1].key)
            self.assertEqual(app.checked, {items[0].key, items[2].key})
            app.toggle_checked([first_course])
            self.assertEqual(len(app.checked), 3)
            app.set_checked([first_course, items[0].key])
            self.assertEqual(len(app.checked), 3)
            app.toggle_checked([items[1].key])
            self.assertEqual(app.checked, {items[0].key, items[2].key})
            app.toggle_checked([first_course])
            app.toggle_checked([first_course])
            self.assertEqual(app.checked, {items[2].key})
        finally:
            app.close()

    def test_row_clicks_keep_prior_checks_and_expansion_arrow_does_not_toggle(self):
        from types import SimpleNamespace
        root = tk.Tk()
        root.withdraw()
        app = Application(root, connect=False)
        try:
            items = [resource("first"), resource("second")]
            app.events.put(("catalog", VideoCatalog(resources=items)))
            app.poll()
            with patch.object(app.table, "identify_column", return_value="#0"), patch.object(app.table, "identify_element", return_value="Treeitem.image"):
                for item in items:
                    with patch.object(app.table, "identify_row", return_value=item.key):
                        self.assertEqual(app.check_click(SimpleNamespace(x=1, y=1, state=0)), "break")
            self.assertEqual(app.checked, {item.key for item in items})
            with patch.object(app.table, "identify_row", return_value=app.table.get_children()[0]), patch.object(app.table, "identify_element", return_value="Treeitem.indicator"):
                self.assertIsNone(app.check_click(SimpleNamespace(x=1, y=1, state=0)))
            self.assertEqual(len(app.checked), 2)
        finally:
            app.close()

    def test_batch_skips_existing_and_saves_other_course_under_selected_directory(self):
        root = tk.Tk()
        root.withdraw()
        app = Application(root, connect=False)
        items = [resource("Existing"), resource("New.MP4")]
        items[1].course_id, items[1].course_title = "another", "Another Course"
        client = Mock()
        client.video.return_value = {"streams": [{"url": "https://example.invalid/video.m3u8", "definition_p": "1080P"}]}
        def save(data, file, cancel, progress):
            if file.exists():
                raise FileExistsError()
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b"downloaded")
            return {}
        try:
            with tempfile.TemporaryDirectory() as directory:
                destination = Path(directory)
                existing = destination / "Course/Existing.mp4"
                existing.parent.mkdir()
                existing.write_bytes(b"user data")
                app.events.put(("catalog", VideoCatalog(resources=items)))
                app.poll()
                app.set_checked([*app.table.get_children(), items[0].key])
                app.folder.set(str(destination))
                with patch("xiaoetong_assistant.app.NativeClient", return_value=client), patch("xiaoetong_assistant.jobs.download_hls", side_effect=save):
                    app.download()
                    app.worker.join(timeout=5)
                    self.assertFalse(app.worker.is_alive())
                    app.poll()
                self.assertEqual(existing.read_bytes(), b"user data")
                self.assertEqual((destination / "Another Course/New.mp4").read_bytes(), b"downloaded")
                self.assertEqual(app.table.set(items[0].key, "state"), "已存在")
                self.assertEqual(app.table.set(items[1].key, "state"), "完成")
                self.assertEqual(app.status.get(), "完成 1 · 已存在 1 · 未完成 0")
                self.assertEqual(len(list(destination.rglob("*.mp4"))), 2)
        finally:
            app.close()


    def test_all_course_roots_exist_and_footer_only_counts_resources(self):
        root = tk.Tk()
        root.withdraw()
        app = Application(root, connect=False)
        try:
            item = resource("video")
            courses = [{"app_id": "app", "resource_id": "course", "title": "Course", "resource_type": 50},
                       {"app_id": "app", "resource_id": "live", "title": "Live Course", "resource_type": 4}]
            app.events.put(("catalog", VideoCatalog(resources=[item], course_rows=courses)))
            app.poll()
            first, empty = app.table.get_children()
            self.assertEqual(app.table.item(empty, "text"), "Live Course")
            self.assertEqual(app.table.get_children(empty), ())
            self.assertEqual(app.table.get_children(first), (item.key,))
            app.toggle_checked([empty])
            self.assertEqual(app.checked, set())
            self.assertEqual(app.summary.get(), "1 个视频 · 0 份文档")
            app.events.put(("finished", 1, 0, 0))
            app.poll()
            self.assertEqual(app.summary.get(), "1 个视频 · 0 份文档")
        finally:
            app.close()


class CancelButtonTests(unittest.TestCase):
    def test_cancel_button_stops_batch_keeps_window_and_reenables_download(self):
        import threading
        from xiaoetong_assistant.downloads import DownloadPaused
        root = tk.Tk()
        root.withdraw()
        app = Application(root, connect=False)
        started = threading.Event()
        def download(client, item, destination, cancel, progress):
            started.set()
            if not cancel.wait(5):
                raise RuntimeError("Cancellation was not delivered")
            raise DownloadPaused()
        try:
            with tempfile.TemporaryDirectory() as directory:
                app.populate_catalog(VideoCatalog(resources=[resource("cancel-test")]), reset=True)
                app.set_checked(list(app.table.get_children()))
                app.folder.set(directory)
                with patch("xiaoetong_assistant.app.NativeClient", return_value=Mock()), patch("xiaoetong_assistant.jobs.download_resource", side_effect=download):
                    app.download()
                    self.assertTrue(started.wait(3))
                    app.update_button()
                    self.assertNotIn("disabled", app.cancel_button.state())
                    app.cancel_button.invoke()
                    app.worker.join(timeout=5)
                    self.assertFalse(app.worker.is_alive())
                    app.poll()
                self.assertTrue(root.winfo_exists())
                self.assertFalse(app.closing)
                self.assertIn("disabled", app.cancel_button.state())
                self.assertNotIn("disabled", app.button.state())
                self.assertTrue(app.status.get().startswith("已取消"))
        finally:
            app.close()
