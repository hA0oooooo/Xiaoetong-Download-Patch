"""Native-authorized HLS transport, memory-only keys, and MP4 validation."""
from concurrent.futures import ThreadPoolExecutor
import hmac
import ipaddress
import math
import os
import re
import secrets
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

from .downloads import DownloadPaused
from .network import make_session

SEGMENT_PREFETCH = 6


def https_url(url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Unsupported media URL")
    if parsed.hostname.lower() in {"localhost", "localhost.localdomain"} or parsed.hostname.lower().endswith(".localhost"):
        raise ValueError("Local media destination rejected")
    try:
        if not ipaddress.ip_address(parsed.hostname).is_global:
            raise ValueError("Private media destination rejected")
    except ValueError as error:
        if "destination" in str(error):
            raise
    return url


def rewrite_playlist(text, source, extension=None):
    """Return a local-only VOD playlist and the exact remote-resource mapping."""
    if not text.lstrip().startswith("#EXTM3U") or "#EXT-X-ENDLIST" not in text:
        raise ValueError("Expected a complete VOD media playlist")
    extension = extension or {}
    mapping, lines = {}, []
    durations = []
    encrypted = False

    def register(url):
        number = len(mapping)
        local = f"/segment/{number}"
        mapping[local] = https_url(url)
        return local

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-STREAM-INF") or line.startswith("#EXT-X-SESSION-KEY"):
            raise ValueError("Master playlists require explicit variant selection")
        if line.startswith("#EXT-X-KEY:"):
            attributes = dict(re.findall(r'([A-Z0-9-]+)=("[^"]*"|[^,]+)', line.split(":", 1)[1]))
            method = attributes.get("METHOD")
            if method == "NONE":
                lines.append(line)
                continue
            if method != "AES-128" or attributes.get("KEYFORMAT", '"identity"') != '"identity"':
                raise ValueError("Unsupported HLS encryption format")
            encrypted = True
            if "URI" not in attributes:
                raise ValueError("Missing HLS key URI")
            line = re.sub(r'URI="[^"]*"', 'URI="/key"', line)
        elif line.startswith("#EXT-X-MAP:"):
            match = re.search(r'URI="([^"]*)"', line)
            if not match:
                raise ValueError("Missing initialization segment URI")
            line = line.replace(match[0], f'URI="{register(urljoin(source, match[1]))}"')
        elif line.startswith("#EXTINF:"):
            value = float(line.split(":", 1)[1].split(",", 1)[0])
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Invalid segment duration")
            durations.append(value)
        elif not line.startswith("#"):
            if extension and urlparse(line).path.lower().endswith(".ts"):
                if urlparse(line).scheme:
                    raise ValueError("Unexpected absolute private segment URL")
                host, prefix, parameter = (extension.get(key) for key in ("host", "path", "param"))
                if not all(isinstance(item, str) for item in (host, prefix, parameter)):
                    raise ValueError("Incomplete native segment transport metadata")
                remote = host.rstrip("/") + "/" + prefix.strip("/") + "/" + line.lstrip("/")
                remote += ("&" if "?" in line else "?") + parameter
            else:
                remote = urljoin(source, line)
            line = register(remote)
        lines.append(line)
    if not durations or not mapping:
        raise ValueError("No video segments in playlist")
    return "\n".join(lines) + "\n", mapping, sum(durations), encrypted


class _LargeSegment(Exception):
    pass


class MediaProxy:
    def __init__(self, playlist, mapping, key, cancel, progress):
        self.playlist = playlist.encode("utf-8")
        self.mapping, self.key = mapping, key
        self.cancel, self.progress = cancel, progress
        self.token = secrets.token_hex(32)
        self.bytes = 0
        self.completed = set()
        self.lock = threading.Lock()
        self.fetch_lock = threading.Lock()
        self.remote_session = make_session()
        self.failure = False
        self.prefetch = ThreadPoolExecutor(max_workers=SEGMENT_PREFETCH, thread_name_prefix="segment")
        self.prefetched = {}
        self.segment_paths = list(mapping)
        self.segment_positions = {path: index for index, path in enumerate(self.segment_paths)}
        self.local = threading.local()
        self.prefetch_sessions = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                if not hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {owner.token}"):
                    self.send_error(403)
                    return
                if self.path == "/manifest.m3u8":
                    self.respond(owner.playlist, "application/vnd.apple.mpegurl")
                elif self.path == "/key" and owner.key is not None:
                    self.respond(owner.key, "application/octet-stream")
                elif self.path in owner.mapping:
                    try:
                        if not self.headers.get("Range"):
                            buffered = owner.prefetch_segment(self.path)
                            if buffered is not None:
                                status, headers, body = buffered
                                self.send_response(status)
                                self.send_header("Content-Type", "application/octet-stream")
                                self.send_header("Cache-Control", "no-store")
                                for field, value in headers.items():
                                    self.send_header(field, value)
                                self.end_headers()
                                self.wfile.write(body)
                                owner.complete(self.path, len(body))
                                return
                        with owner.fetch_lock:
                            headers = {"Accept-Encoding": "identity"}
                            if self.headers.get("Range"):
                                if not re.fullmatch(r"bytes=\d+-\d*", self.headers["Range"]):
                                    raise ValueError("Invalid segment byte range")
                                headers["Range"] = self.headers["Range"]
                            with owner.remote_session.get(owner.mapping[self.path], headers=headers, stream=True, timeout=(10, 25)) as response:
                                response.raise_for_status()
                                if response.status_code not in (200, 206):
                                    raise ValueError("Invalid segment response")
                                self.send_response(response.status_code)
                                self.send_header("Content-Type", "application/octet-stream")
                                self.send_header("Cache-Control", "no-store")
                                for field in ("Content-Length", "Content-Range"):
                                    if field in response.headers:
                                        self.send_header(field, response.headers[field])
                                self.end_headers()
                                written = 0
                                for chunk in response.iter_content(256 * 1024):
                                    if owner.cancel.is_set():
                                        raise DownloadPaused()
                                    self.wfile.write(chunk)
                                    written += len(chunk)
                                owner.complete(self.path, written)
                    except Exception:
                        owner.failure = True
                        self.close_connection = True
                else:
                    self.send_error(404)

            def respond(self, data, content_type):
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def complete(self, path, size):
        with self.lock:
            self.bytes += size
            self.completed.add(path)
            self.progress(len(self.completed), len(self.mapping), self.bytes)

    def fetch_segment(self, path):
        for attempt in range(3):
            try:
                return self.fetch_segment_once(path)
            except requests.RequestException as error:
                if isinstance(error, requests.HTTPError) and error.response is not None:
                    if error.response.status_code < 500 and error.response.status_code != 429:
                        raise
                if attempt == 2:
                    raise
                if self.cancel.wait(0.3 * (attempt + 1)):
                    raise DownloadPaused()

    def fetch_segment_once(self, path):
        if self.cancel.is_set():
            raise DownloadPaused()
        if not hasattr(self.local, "session"):
            self.local.session = make_session()
            with self.lock:
                self.prefetch_sessions.append(self.local.session)
        with self.local.session.get(self.mapping[path], headers={"Accept-Encoding": "identity"},
                                    stream=True, timeout=(10, 25)) as response:
            response.raise_for_status()
            if response.status_code not in (200, 206):
                raise ValueError("Invalid segment response")
            chunks, size = [], 0
            for chunk in response.iter_content(256 * 1024):
                if self.cancel.is_set():
                    raise DownloadPaused()
                size += len(chunk)
                if size > 32 * 1024 * 1024:
                    raise _LargeSegment()
                chunks.append(chunk)
            headers = {field: response.headers[field] for field in ("Content-Length", "Content-Range")
                       if field in response.headers}
            return response.status_code, headers, b"".join(chunks)

    def prefetch_segment(self, path):
        # Keep only six segment futures, even for out-of-order or range requests.
        with self.lock:
            position = self.segment_positions[path]
            if path not in self.prefetched:
                if len(self.prefetched) >= SEGMENT_PREFETCH:
                    return None
                self.prefetched[path] = self.prefetch.submit(self.fetch_segment, path)
            for upcoming in self.segment_paths[position + 1:position + SEGMENT_PREFETCH]:
                if len(self.prefetched) >= SEGMENT_PREFETCH:
                    break
                if upcoming not in self.prefetched and upcoming not in self.completed:
                    self.prefetched[upcoming] = self.prefetch.submit(self.fetch_segment, upcoming)
            future = self.prefetched[path]
        try:
            return future.result()
        except _LargeSegment:
            return None  # Stream unusually large segments without buffering them.
        finally:
            with self.lock:
                self.prefetched.pop(path, None)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.prefetch.shutdown(wait=True, cancel_futures=True)
        for session in self.prefetch_sessions:
            session.close()
        self.prefetched.clear()
        self.remote_session.close()
        self.key = None


def ffmpeg_exe():
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def inspect_mp4(file, expected_duration=None):
    executable = ffmpeg_exe()
    result = subprocess.run([executable, "-hide_banner", "-i", str(file)], capture_output=True, text=True, errors="replace", timeout=20, stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    info = result.stderr
    duration_match = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", info)
    if not duration_match or not re.search(r"Stream .*Video:", info):
        raise ValueError("Downloaded file has no playable video stream")
    hour, minute, second = map(float, duration_match.groups())
    duration = hour * 3600 + minute * 60 + second
    if expected_duration and abs(duration - expected_duration) > max(3, expected_duration * 0.005):
        raise ValueError("Downloaded video duration does not match the playlist")
    for start in (0, max(0, duration - 4)):
        check = subprocess.run([executable, "-v", "error", "-nostdin", "-ss", str(start), "-i", str(file), "-t", "3", "-f", "null", "-"], capture_output=True, timeout=30, stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if check.returncode != 0 or check.stderr.strip():
            raise ValueError("Downloaded video sample failed decoding")
    return {"duration_seconds": round(duration, 3), "has_video": True, "has_audio": bool(re.search(r"Stream .*Audio:", info)), "sample_decode": "passed"}


def select_stream(video_data):
    """Choose the highest resolution actually supplied by the authorized API."""
    streams = video_data.get("streams")
    if not isinstance(streams, list) or not streams:
        raise ValueError("No native video streams")
    candidates = [item for item in streams if urlparse(item.get("url", "")).path.lower().endswith((".m3u8", ".mp4"))]
    if not candidates:
        raise ValueError("No supported video stream")
    def resolution(item):
        label = str(item.get("definition_p", "")).upper()
        if label in {"原画", "原始", "ORIGINAL", "SOURCE"}:
            return float("inf")
        if label in {"4K", "8K"}:
            return int(label[0]) * 540
        value = re.search(r"\d+", label)
        return int(value[0]) if value else int(item.get("height") or 0)
    return max(candidates, key=resolution)


def download_hls(video_data, destination, cancel, progress=lambda *_: None):
    destination = Path(destination).resolve()
    if destination.exists():
        raise FileExistsError("Destination already exists")
    stream = select_stream(video_data)
    source = https_url(stream["url"])
    with make_session() as session, session.get(source, timeout=(10, 25)) as response:
        response.raise_for_status()
        if len(response.content) > 4 * 1024 * 1024:
            raise ValueError("Oversized playlist")
        text = response.text
    playlist, mapping, duration, encrypted = rewrite_playlist(text, source, stream.get("ext") if stream.get("is_support") else None)
    key = None
    if encrypted:
        key_hex = video_data.get("key_hex")
        if not isinstance(key_hex, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", key_hex):
            raise ValueError("Native playback key is unavailable")
        key = bytes.fromhex(key_hex)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.stem + "." + secrets.token_hex(6) + ".part.mp4")
    environment = os.environ.copy()
    for variable in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        environment[variable] = ""
    environment["NO_PROXY"] = "*"
    process = None
    try:
        with MediaProxy(playlist, mapping, key, cancel, progress) as proxy, tempfile.TemporaryFile() as errors:
            command = [ffmpeg_exe(), "-nostdin", "-v", "error", "-n", "-protocol_whitelist", "http,tcp,crypto", "-headers", f"Authorization: Bearer {proxy.token}\r\n", "-i", f"http://127.0.0.1:{proxy.server.server_port}/manifest.m3u8", "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-movflags", "+faststart", "-f", "mp4", str(partial)]
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors, env=environment, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                while True:
                    try:
                        code = process.wait(timeout=0.25)
                        break
                    except subprocess.TimeoutExpired:
                        if cancel.is_set():
                            raise DownloadPaused()
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
            if cancel.is_set():
                raise DownloadPaused()
            if code or proxy.failure or len(proxy.completed) != len(mapping):
                raise RuntimeError("Media transport or remux failed; no completed file published")
            verification = inspect_mp4(partial, duration)
            summary = {**verification, "segments": len(mapping), "downloaded_bytes": proxy.bytes, "definition": stream.get("definition_p"), "file_bytes": partial.stat().st_size}
        if destination.exists():
            raise FileExistsError("Destination appeared during download")
        partial.rename(destination)
        return summary
    finally:
        partial.unlink(missing_ok=True)
