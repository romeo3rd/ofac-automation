from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from io import BytesIO
from threading import Event
from time import monotonic
from typing import Callable
from unicodedata import category, normalize
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from xml.etree.ElementTree import Element, ParseError, iterparse


EXPORT_URL = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports"
SANCTIONS_LISTS = (
    ("SDN", f"{EXPORT_URL}/SDN.XML"),
    ("Consolidated Non-SDN", f"{EXPORT_URL}/CONSOLIDATED.XML"),
)
RETRYABLE_HTTP_CODES = {408, 429, 500, 502, 503, 504}
DOWNLOAD_ATTEMPTS = 4
MAX_RETRY_WAIT = 60
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
ADDRESS_FIELDS = ("address1", "address2", "address3", "city", "stateOrProvince", "postalCode", "country")


class CheckCancelled(Exception):
    pass


@dataclass(frozen=True)
class SanctionsSource:
    name: str
    url: str
    published: str


@dataclass(frozen=True)
class SanctionsMatch:
    source: str
    uid: str
    listed_name: str
    matched_name: str
    name_type: str
    entity_type: str
    programs: str
    addresses: tuple[str, ...]


def check_cancelled(cancel_event: Event) -> None:
    if cancel_event.is_set():
        raise CheckCancelled


def name_key(name: str) -> str:
    normalized = normalize("NFKC", name).casefold()
    characters = (char if category(char)[0] in "LMN" else " " for char in normalized)
    return " ".join("".join(characters).split())


def retry_after_seconds(value: str | None) -> float:
    if not value:
        return 0
    try:
        return max(0, int(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0, (retry_at - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, OverflowError):
            return 0


def download_list(name: str, url: str, cancel_event: Event, notify: Callable[[str], None]) -> BytesIO:
    request = Request(url, headers={"User-Agent": "OFAC-Automation/1.0"})
    for attempt in range(DOWNLOAD_ATTEMPTS):
        check_cancelled(cancel_event)
        delay = 2 ** (attempt + 1)
        try:
            data = BytesIO()
            deadline = monotonic() + 180
            with urlopen(request, timeout=10) as response:
                while True:
                    check_cancelled(cancel_event)
                    if monotonic() >= deadline:
                        raise TimeoutError("Download took longer than three minutes")
                    chunk = response.read1(128 * 1024)
                    if not chunk:
                        break
                    data.write(chunk)
                    if data.tell() > MAX_DOWNLOAD_BYTES:
                        raise RuntimeError(f"The OFAC {name} list download exceeded 100 MB.")
                expected_size = response.headers.get("Content-Length")
                if expected_size and expected_size.isdecimal() and data.tell() != int(expected_size):
                    raise OSError("Download ended before the full file arrived")
            data.seek(0)
            return data
        except HTTPError as exc:
            exc.close()
            check_cancelled(cancel_event)
            reason = f"HTTP {exc.code}"
            if exc.code not in RETRYABLE_HTTP_CODES:
                raise RuntimeError(f"Could not download the OFAC {name} list ({reason}).") from exc
            delay = max(delay, retry_after_seconds(exc.headers.get("Retry-After")))
        except (OSError, HTTPException) as exc:
            check_cancelled(cancel_event)
            reason = str(exc) or type(exc).__name__

        if delay > MAX_RETRY_WAIT:
            raise RuntimeError(f"OFAC asked to wait {delay:.0f}s before retrying the {name} list ({reason}).")
        if attempt + 1 < DOWNLOAD_ATTEMPTS:
            notify(f"{name} list unavailable ({reason}). Retrying in {delay:.0f}s.")
            cancel_event.wait(delay)

    raise RuntimeError(f"Could not download the OFAC {name} list after {DOWNLOAD_ATTEMPTS} attempts ({reason}).")


def index_list(
    data: BytesIO, source: str, matches: dict[str, list[SanctionsMatch]], cancel_event: Event
) -> str:
    try:
        elements = iterparse(data, events=("start", "end"))
        _, root = next(elements)
        if root.tag.rsplit("}", 1)[-1] != "sdnList":
            raise ValueError("Expected a sanctions list XML file")

        published = ""
        expected_count = 0
        count = 0
        for event, element in elements:
            if event != "end":
                continue
            tag = element.tag.rsplit("}", 1)[-1]
            if tag == "publshInformation":
                published = element.findtext("{*}Publish_Date", "").strip()
                datetime.strptime(published, "%m/%d/%Y")
                expected_count = int(element.findtext("{*}Record_Count", ""))
            elif tag == "sdnEntry":
                check_cancelled(cancel_event)
                index_entry(element, source, matches)
                count += 1
            else:
                continue
            root.remove(element)

        if not published or count == 0 or count != expected_count:
            raise ValueError(f"Read {count} entries and expected {expected_count}")
    except (ParseError, ValueError) as exc:
        raise RuntimeError(f"The OFAC {source} list could not be verified. {exc}.") from exc

    return published


def index_entry(entry: Element, source: str, matches: dict[str, list[SanctionsMatch]]) -> None:
    uid = entry.findtext("{*}uid", "").strip()
    if not uid.isdecimal():
        raise ValueError("An entry is missing its numeric UID")

    entry_name = ""
    matched_names: dict[str, tuple[str, str]] = {}
    for name in (entry, *entry.findall("{*}akaList/{*}aka")):
        first = name.findtext("{*}firstName", "").strip()
        last = name.findtext("{*}lastName", "").strip()
        if not last:
            raise ValueError(f"Entry {uid} is missing a listed name")
        display_name = f"{last}, {first}" if first else last
        if name is entry:
            entry_name = display_name
            name_type = "Primary name"
        else:
            strength = name.findtext("{*}category", "").strip().capitalize()
            name_type = f"{strength} alias" if strength else "Alias"

        for key in {name_key(display_name), name_key(f"{first} {last}")}:
            if key in matches:
                matched_names.setdefault(key, (display_name, name_type))

    if matched_names:
        programs = ", ".join(program.text for program in entry.findall("{*}programList/{*}program") if program.text)
        entity_type = entry.findtext("{*}sdnType", "").strip()
        addresses = []
        for address in entry.findall("{*}addressList/{*}address"):
            parts = [address.findtext(f"{{*}}{field}", "").strip() for field in ADDRESS_FIELDS]
            formatted = ", ".join(part for part in parts if part)
            if formatted:
                addresses.append(formatted)
        for key, (matched_name, name_type) in matched_names.items():
            matches[key].append(
                SanctionsMatch(source, uid, entry_name, matched_name, name_type, entity_type, programs, tuple(addresses))
            )
