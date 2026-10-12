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
from xiaoetong_assistant.jobs import download_batch
from xiaoetong_assistant.media import MediaProxy, download_hls, inspect_mp4
from xiaoetong_assistant.native import NativeResource


def item(title):
    return NativeResource("app", "user", "course", title, title, "Course", 3, True, False)


class BatchTests(unittest.TestCase):
    def test_five_concurrent_workers_own_sessions_and_report_monotonic_totals(self):
        barrier = threading.Barrier(5, timeout=5)
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
            counts = download_batch(factory, [item(str(i)) for i in range(10)], Path("unused"),
                                    threading.Event(), events.put)
        self.assertEqual(counts, (10, 0, 0))
        self.assertEqual(peak, 5)
        self.assertEqual(len(clients), 5)
        self.assertTrue(all(c.close.call_count == 1 for c in clients))
        totals = [e[1] for e in list(events.queue) if e[0] == "batch"]
        self.assertEqual(totals, list(range(1, 11)))

    def test_cancel_stops_active_tasks_and_does_not_start_queued_tasks(self):
        cancel, events = threading.Event(), queue.Queue()
        barrier = threading.Barrier(5, timeout=5)
        started = []
        def download(client, resource, destination, cancel, progress):
            started.append(resource.key)
            barrier.wait()
            cancel.set()
            raise DownloadPaused()
        with patch("xiaoetong_assistant.jobs.download_resource", side_effect=download):
            counts = download_batch(Mock, [item(str(i)) for i in range(20)], Path("unused"), cancel, events.put)
        self.assertEqual(len(started), 5)
        self.assertEqual(counts, (0, 0, 0))
        self.assertEqual(sum(e[0] == "job" and e[2] == "已停止" for e in list(events.queue)), 5)

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
    def test_prefetch_overlaps_six_requests_and_serves_exact_bytes_in_order(self):
        barrier = threading.Barrier(6, timeout=5)
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
        mapping = {f"/segment/{i}": f"https://cdn.invalid/{i}" for i in range(6)}
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
