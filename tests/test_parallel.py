import http.client
import queue
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from xiaoetong_assistant.downloads import DownloadPaused
from xiaoetong_assistant.jobs import MAX_CONCURRENT_DOWNLOADS, download_batch, ordered_resources
from xiaoetong_assistant.media import SEGMENT_PREFETCH, MediaProxy, download_hls, inspect_mp4
from xiaoetong_assistant.native import NativeResource


def item(title):
    return NativeResource("app", "user", "course", title, title, "Course", 3, True, False)


class BatchTests(unittest.TestCase):
    def test_concurrent_workers_own_sessions_and_report_monotonic_totals(self):
        count = MAX_CONCURRENT_DOWNLOADS * 2
        barrier = threading.Barrier(MAX_CONCURRENT_DOWNLOADS, timeout=5)
        clients, events = [], queue.Queue()
        lock = threading.Lock()
        active = peak = 0
        def factory():
            client = Mock()
            client.owner_thread = threading.get_ident()
            clients.append(client)
            return client
        def download(client, resource, destination, cancel, progress):
            nonlocal active, peak
            self.assertEqual(client.owner_thread, threading.get_ident())
            with lock:
                active += 1
                peak = max(peak, active)
            barrier.wait()
            progress("50%")
            with lock:
                active -= 1
        with patch("xiaoetong_assistant.jobs.download_resource", side_effect=download):
            counts = download_batch(factory, [item(str(i)) for i in range(count)], Path("unused"),
                                    threading.Event(), events.put)
        self.assertEqual(counts, (count, 0, 0))
        self.assertEqual(peak, MAX_CONCURRENT_DOWNLOADS)
        self.assertEqual(len(clients), MAX_CONCURRENT_DOWNLOADS)
        self.assertTrue(all(c.close.call_count == 1 for c in clients))
        totals = [e[1] for e in list(events.queue) if e[0] == "batch"]
        self.assertEqual(totals, list(range(1, count + 1)))

    def test_cancel_stops_active_tasks_and_does_not_start_queued_tasks(self):
        cancel, events = threading.Event(), queue.Queue()
        barrier = threading.Barrier(MAX_CONCURRENT_DOWNLOADS, timeout=5)
        started = []
        def download(client, resource, destination, cancel, progress):
            started.append(resource.key)
            barrier.wait()
            cancel.set()
            raise DownloadPaused()
        with patch("xiaoetong_assistant.jobs.download_resource", side_effect=download):
            counts = download_batch(Mock, [item(str(i)) for i in range(20)], Path("unused"), cancel, events.put)
        self.assertEqual(len(started), MAX_CONCURRENT_DOWNLOADS)
        self.assertEqual(counts, (0, 0, 0))
        self.assertEqual(sum(e[0] == "job" and e[2] == "已停止" for e in list(events.queue)), MAX_CONCURRENT_DOWNLOADS)

    def test_mixed_queue_starts_documents_before_all_videos_finish(self):
        videos = [item(str(i)) for i in range(10)]
        docs = [item("doc" + str(i)) for i in range(3)]
        for resource in docs:
            resource.resource_type = 51
        selected = videos + docs
        expected = [videos[0], docs[0], videos[1], docs[1], videos[2], docs[2]] + videos[3:]
        self.assertEqual(ordered_resources(selected), expected)
        started = []
        with patch("xiaoetong_assistant.jobs.download_resource", side_effect=lambda client, resource, *args: started.append(resource)):
            counts = download_batch(Mock, selected, Path("unused"), threading.Event(), lambda _: None, max_workers=1)
        self.assertEqual(started, expected)
        self.assertEqual(counts, (13, 0, 0))
        self.assertEqual(selected, videos + docs)

    def test_document_only_and_empty_queues(self):
        doc = item("doc")
        doc.resource_type = 51
        self.assertEqual(ordered_resources([doc]), [doc])
        self.assertEqual(download_batch(Mock, [], Path("unused"), threading.Event(), lambda _: None), (0, 0, 0))

    def test_same_title_video_and_document_do_not_block_each_other(self):
        video, document = item("Same"), item("Same.pdf")
        document.resource_type = 51
        barrier = threading.Barrier(2, timeout=5)
        with patch("xiaoetong_assistant.jobs.download_resource", side_effect=lambda *args: barrier.wait()):
            counts = download_batch(Mock, [video, document], Path("unused"), threading.Event(), lambda _: None, max_workers=2)
        self.assertEqual(counts, (2, 0, 0))

    def test_windows_output_name_collisions_serialize_and_preserve_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "same.mp4"
            def download(*args):
                if output.exists():
                    raise FileExistsError()
                with output.open("xb") as file:
                    file.write(b"original")
            with patch("xiaoetong_assistant.jobs.download_resource", side_effect=download):
                counts = download_batch(Mock, [item("Same"), item("same.MP4")], Path(directory),
                                        threading.Event(), lambda _: None)
            self.assertEqual(counts, (1, 0, 1))
            self.assertEqual(output.read_bytes(), b"original")

    def test_individual_failure_does_not_stop_other_resources(self):
        with patch("xiaoetong_assistant.jobs.download_resource", side_effect=[PermissionError(), None, FileExistsError()]):
            counts = download_batch(Mock, [item(str(i)) for i in range(3)], Path("unused"),
                                    threading.Event(), lambda _: None, max_workers=1)
        self.assertEqual(counts, (1, 1, 1))


class MediaParallelTests(unittest.TestCase):
    def test_prefetch_overlaps_requests_and_serves_exact_bytes_in_order(self):
        barrier = threading.Barrier(SEGMENT_PREFETCH, timeout=5)
        sessions = []
        completed = threading.Event()
        class Response:
            status_code = 200
            def __init__(self, body):
                self.body = body
                self.headers = {"Content-Length": str(len(body))}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def raise_for_status(self): pass
            def iter_content(self, size):
                barrier.wait()
                yield self.body
        class Session:
            def __init__(self): self.closed = False
            def get(self, url, **kwargs): return Response(url.encode())
            def close(self): self.closed = True
        def factory():
            session = Session()
            sessions.append(session)
            return session
        mapping = {f"/segment/{i}": f"https://cdn.invalid/{i}" for i in range(SEGMENT_PREFETCH)}
        with patch("xiaoetong_assistant.media.make_session", side_effect=factory):
            with MediaProxy("playlist", mapping, None, threading.Event(),
                            lambda done, total, size: completed.set() if done == total else None) as proxy:
                connection = http.client.HTTPConnection("127.0.0.1", proxy.server.server_port, timeout=8)
                for path, url in mapping.items():
                    connection.request("GET", path, headers={"Authorization": f"Bearer {proxy.token}"})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), url.encode())
                connection.close()
                self.assertTrue(completed.wait(2))
                self.assertEqual(proxy.completed, set(mapping))
                self.assertFalse(proxy.failure)
        self.assertTrue(all(s.closed for s in sessions))

    def test_transient_segment_failure_retries_without_returning_partial_bytes(self):
        import requests
        response = Mock(status_code=200, headers={"Content-Length": "2"})
        response.iter_content.return_value = [b"ok"]
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        session = Mock()
        session.get.side_effect = [requests.exceptions.SSLError("temporary"), response]
        with patch("xiaoetong_assistant.media.make_session", return_value=session):
            with MediaProxy("playlist", {"/segment/0": "https://cdn.invalid/0"}, None,
                            threading.Event(), lambda *_: None) as proxy:
                result = proxy.fetch_segment("/segment/0")
                self.assertEqual(result[2], b"ok")
                self.assertEqual(session.get.call_count, 2)

    def test_cancellation_during_retry_does_not_retry_or_publish_a_segment(self):
        import requests
        cancel = threading.Event()
        def fail(*args, **kwargs):
            cancel.set()
            raise requests.ConnectionError("temporary")
        session = Mock()
        session.get.side_effect = fail
        with patch("xiaoetong_assistant.media.make_session", return_value=session):
            with MediaProxy("playlist", {"/segment/0": "https://cdn.invalid/0"}, None,
                            cancel, lambda *_: None) as proxy:
                with self.assertRaises(DownloadPaused):
                    proxy.fetch_segment("/segment/0")
                self.assertEqual(session.get.call_count, 1)
                self.assertEqual(proxy.completed, set())

    def test_media_inspection_and_download_processes_disable_windows_console(self):
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        probe = SimpleNamespace(stderr="Duration: 00:00:06.00\nStream #0: Video: h264\n", returncode=0)
        with patch("xiaoetong_assistant.media.ffmpeg_exe", return_value="ffmpeg"), patch("xiaoetong_assistant.media.subprocess.run", side_effect=[probe, SimpleNamespace(stderr=b"", returncode=0), SimpleNamespace(stderr=b"", returncode=0)]) as run:
            inspect_mp4(Path("test.mp4"), 6)
            self.assertEqual(run.call_count, 3)
            for call in run.call_args_list:
                self.assertEqual(call.kwargs["creationflags"], flags)
                self.assertEqual(call.kwargs["stdin"], subprocess.DEVNULL)
        playlist = "#EXTM3U\n#EXTINF:6.0,\none.ts\n#EXT-X-ENDLIST\n"
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        response = Mock(text=playlist)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.content = playlist.encode()
        session.get.return_value = response
        data = {"streams": [{"url": "https://cdn.invalid/video.m3u8", "definition_p": "720P"}]}
        with tempfile.TemporaryDirectory() as directory:
            with patch("xiaoetong_assistant.media.make_session", return_value=session), patch("xiaoetong_assistant.media.ffmpeg_exe", return_value="ffmpeg"), patch("xiaoetong_assistant.media.subprocess.Popen", side_effect=RuntimeError("test stop")) as popen:
                with self.assertRaisesRegex(RuntimeError, "test stop"):
                    download_hls(data, Path(directory) / "video.mp4", threading.Event())
                self.assertEqual(popen.call_args.kwargs["creationflags"], flags)
                self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
