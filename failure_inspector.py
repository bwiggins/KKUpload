from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path


class _ImageUrlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = {name.casefold(): value for name, value in attrs}

        if tag.casefold() == "meta":
            property_name = (
                attributes.get("property")
                or attributes.get("name")
                or ""
            ).casefold()
            if property_name in {"og:image", "twitter:image", "image"}:
                self._add_url(attributes.get("content"))

        if tag.casefold() == "link":
            rel = (attributes.get("rel") or "").casefold()
            if rel in {"image_src", "preload"}:
                self._add_url(attributes.get("href"))

    def _add_url(self, url: str | None) -> None:
        if not url:
            return

        url = url.strip()
        if url and url not in self.urls:
            self.urls.append(url)


def investigate_unsupported_asset(file_path: Path) -> tuple[str, ...]:
    findings: list[str] = []

    try:
        sample = file_path.read_bytes()[:65536]
    except OSError as exc:
        return (f"Unable to inspect failed file: {exc}",)

    detected = _detect_content_type(sample)
    if detected is None:
        findings.append(
            "Unable to identify the failed file type from its file signature."
        )
    else:
        findings.append(f"Detected failed file content: {detected}.")

    if _looks_like_html(sample):
        findings.append(
            "File extension looks like media, but the file content appears "
            "to be HTML."
        )
        findings.extend(_html_image_url_findings(sample))

    return tuple(findings)


def _detect_content_type(sample: bytes) -> str | None:
    stripped = sample.lstrip()

    if stripped.startswith((b"<!DOCTYPE html", b"<!doctype html", b"<html", b"<HTML")):
        return "HTML document"
    if sample.startswith(b"\xff\xd8\xff"):
        return "JPEG image"
    if sample.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG image"
    if sample.startswith(b"GIF87a") or sample.startswith(b"GIF89a"):
        return "GIF image"
    if sample.startswith(b"RIFF") and sample[8:12] == b"WEBP":
        return "WebP image"
    if sample.startswith(b"BM"):
        return "BMP image"
    if sample.startswith((b"II*\x00", b"MM\x00*")):
        return "TIFF image"

    return None


def _looks_like_html(sample: bytes) -> bool:
    stripped = sample.lstrip()[:512].lower()
    return (
        stripped.startswith(b"<!doctype html")
        or stripped.startswith(b"<html")
        or b"<html" in stripped
    )


def _html_image_url_findings(sample: bytes) -> tuple[str, ...]:
    text = sample.decode("utf-8", errors="replace")
    parser = _ImageUrlParser()
    try:
        parser.feed(text)
    except Exception:  # noqa: BLE001
        return ()

    return tuple(f"Possible image URL found: {url}" for url in parser.urls[:5])
