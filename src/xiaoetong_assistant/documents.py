"""PDF and Word download validation before publishing the final file."""
from pathlib import Path
from urllib.parse import urlparse, unquote
from zipfile import ZipFile, BadZipFile
from .downloads import download_file
from .media import https_url

SUPPORTED = {"pdf", "doc", "docx"}


def document_format(info):
    candidates = [info.get("extension", ""), info.get("file_name", ""), unquote(urlparse(info.get("file_url", "")).path)]
    for value in candidates:
        extension = str(value).lower().strip(" .")
        if extension not in SUPPORTED:
            extension = Path(extension).suffix.lstrip(".")
        if extension in SUPPORTED:
            return extension
    raise ValueError("Unsupported document format; only PDF and Word are supported")


def validate_document(file, extension):
    with Path(file).open("rb") as stream:
        prefix = stream.read(1024)
    if extension == "pdf" and prefix.startswith(b"%PDF-"):
        return
    if extension == "doc" and prefix.startswith(bytes.fromhex("d0cf11e0a1b11ae1")):
        return
    if extension == "docx":
        try:
            with ZipFile(file) as archive:
                if "word/document.xml" in archive.namelist() and "[Content_Types].xml" in archive.namelist() and archive.testzip() is None:
                    return
        except (BadZipFile, OSError):
            pass
    raise ValueError("Document content does not match its format")


def document_url(url):
    # Some native course attachments advertise HTTP CDN addresses. Use TLS
    # while preserving the original signed path/query; never fall back to HTTP.
    parsed = urlparse(url)
    url = parsed._replace(scheme="https").geturl() if parsed.scheme == "http" else url
    return https_url(url)


def download_document(info, destination, cancel, progress=lambda *_: None):
    extension = document_format(info)
    url = document_url(info["file_url"])
    return download_file(url, destination, cancel, progress,
                         validate=lambda file: validate_document(file, extension))
