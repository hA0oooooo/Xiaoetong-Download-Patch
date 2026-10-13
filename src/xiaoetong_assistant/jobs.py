"""One shared resource download path for GUI and CLI."""
from concurrent.futures import ThreadPoolExecutor
from itertools import zip_longest
import queue
import threading
from urllib.parse import urlparse

from .downloads import DownloadPaused, safe_name, download_file
from .media import download_hls, select_stream, inspect_mp4
from .documents import document_format, download_document

MAX_CONCURRENT_DOWNLOADS = 8


def ordered_resources(items):
    """Alternate video and document jobs, preserving order within each kind."""
    videos, documents, other = [], [], []
    for item in items:
        (videos if item.resource_type == 3 else documents if item.resource_type == 51 else other).append(item)
    return [item for pair in zip_longest(videos, documents) for item in pair if item is not None] + other


def download_resource(client, item, destination, cancel, progress=lambda *_: None):
    if not item.unlocked or item.trial:
        raise PermissionError("Resource is locked or trial-only")
    folder = destination / safe_name(item.course_title)
    if item.resource_type == 3:
        name = safe_name(item.title)
        if name.lower().endswith(".mp4"):
            name = name[:-4]
        file = folder / (name + ".mp4")
        if file.exists():
            raise FileExistsError("Destination already exists")
        data = client.video(**item.video_identity())
        stream = select_stream(data)
        quality = str(stream.get("definition_p") or "最高可用画质")
        def report(done, total, size):
            progress(f"{quality} · {done / total:.0%}")
        if urlparse(stream["url"]).path.lower().endswith(".mp4"):
            def report_file(done, total):
                progress(f"{quality} · {done / total:.0%}" if total else f"{quality} · {done / 1048576:.1f} MB")
            download_file(stream["url"], file, cancel, report_file, validate=inspect_mp4)
            return file, {"definition": quality, **inspect_mp4(file)}
        return file, download_hls(data, file, cancel, report)
    if item.resource_type == 51:
        resolve = client.courseware_document if item.source == "courseware" else client.document
        info = resolve(**item.video_identity())
        extension = document_format(info)
        name = safe_name(item.title)
        if name.lower().endswith("." + extension):
            name = name[:-(len(extension) + 1)]
        file = folder / (name + "." + extension)
        def report(done, total):
            progress(f"{done / total:.0%}" if total else f"{done / 1048576:.1f} MB")
        download_document(info, file, cancel, report)
        return file, {"format": extension}
    raise ValueError("Unsupported resource type")


def download_batch(client_factory, items, destination, cancel, notify, max_workers=MAX_CONCURRENT_DOWNLOADS):
    """Bounded workers own separate sessions; colliding output names serialize."""
    if max_workers < 1:
        raise ValueError("At least one download worker is required")
    pending = queue.Queue()
    output_locks = {}
    items = ordered_resources(items)
    for number, item in enumerate(items, 1):
        stem = safe_name(item.title).casefold()
        for extension in (".mp4", ".pdf", ".docx", ".doc"):
            if stem.endswith(extension):
                stem = stem[:-len(extension)]
                break
        identity = (safe_name(item.course_title).casefold(), item.resource_type, stem)
        lock = output_locks.setdefault(identity, threading.Lock())
        pending.put((number, item, lock))
    counts = [0, 0, 0]  # completed, failed, existing
    count_lock = threading.Lock()
    total = len(items)

    def worker():
        client = None
        try:
            while not cancel.is_set():
                try:
                    number, item, output_lock = pending.get_nowait()
                except queue.Empty:
                    return
                with output_lock:
                    if cancel.is_set():
                        return
                    notify(("job", item.key, "读取资源…", number, total))
                    try:
                        if client is None:
                            client = client_factory()
                        def progress(state):
                            notify(("job", item.key, state, number, total))
                        download_resource(client, item, destination, cancel, progress)
                        result, state = 0, "完成"
                    except DownloadPaused:
                        notify(("job", item.key, "已停止", number, total))
                        return
                    except FileExistsError:
                        result, state = 2, "已存在"
                    except Exception as error:
                        result = 1
                        state = "无观看权限" if isinstance(error, PermissionError) else "格式未支持" if isinstance(error, ValueError) else "下载失败"
                    notify(("job", item.key, state, number, total))
                    with count_lock:
                        counts[result] += 1
                        notify(("batch", sum(counts), total))
        finally:
            if client is not None:
                client.close()

    with ThreadPoolExecutor(max_workers=min(max_workers, total or 1), thread_name_prefix="download") as executor:
        futures = [executor.submit(worker) for _ in range(min(max_workers, total))]
        for future in futures:
            future.result()
    return tuple(counts)
