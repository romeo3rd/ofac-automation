from __future__ import annotations

import csv
import re
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from math import ceil
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Event
from time import monotonic
from typing import Callable, Iterable, Iterator, Sequence

from playwright.sync_api import Error as PlaywrightError, Page, Response, sync_playwright


OFAC_URL = "https://sanctionssearch.ofac.treas.gov/"
NAME_INPUT = "input[id$='txtLastName']"
SEARCH_BUTTON = "input[id$='btnSearch']"
RESULT_LABEL = "#ctl00_MainContent_lblResults"
SITE_TIMEOUT_MS = 20000
SEARCH_INTERVAL = 1.5
SEARCH_ATTEMPTS = 4
MAX_AUTO_WAIT = 120
RATE_LIMIT_BACKOFF = 15
RETRYABLE_HTTP_CODES = {408, 429, 500, 502, 503, 504}
ProgressCallback = Callable[[str], None]
ResultCallback = Callable[["OfacResult"], None]
PdfResultCallback = Callable[[int, "OfacResult"], None]


class CheckCancelled(Exception):
    pass


class OfacUnavailable(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        retry_after: float = 0.0,
        rate_limited: bool = False,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after
        self.rate_limited = rate_limited


@dataclass
class SearchPacing:
    interval: float = SEARCH_INTERVAL
    next_request_at: float = 0.0

    def seconds_remaining(self) -> float:
        return max(0.0, self.next_request_at - monotonic())

    def postpone(self, seconds: float) -> None:
        self.next_request_at = max(self.next_request_at, monotonic() + seconds)

    def wait(self, cancel: Event, notify: ProgressCallback) -> None:
        delay = self.seconds_remaining()
        if cancel.is_set():
            raise CheckCancelled
        if delay > MAX_AUTO_WAIT:
            raise OfacUnavailable(f"OFAC requested a cooldown. Try resuming in {ceil(delay)} seconds.")
        if delay > self.interval:
            notify(f"Waiting {ceil(delay)}s before contacting OFAC…")
        if cancel.wait(delay):
            raise CheckCancelled
        self.next_request_at = monotonic() + self.interval


@dataclass(frozen=True)
class OfacResult:
    company: str
    status: str
    result_text: str
    pdf_path: Path | None = None
    error: str | None = None

    @property
    def display_status(self) -> str:
        return "No matches found" if self.status == "Clean" else self.status


PdfRequest = tuple[str, Path]


def parse_names(text: str) -> list[str]:
    return unique_names(line.strip() for line in text.splitlines())


def load_names_from_file(path: Path) -> list[str]:
    if path.suffix.lower() == ".csv":
        return load_names_from_csv(path)

    with path.open("r", encoding="utf-8-sig") as file:
        return unique_names(line.strip() for line in file if line.strip())


def load_names_from_csv(path: Path) -> list[str]:
    names: list[str] = []

    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.reader(file)
        for row_index, row in enumerate(reader):
            value = first_nonempty_cell(row)
            if not value:
                continue
            if row_index == 0 and value.strip().lower() in {"name", "company", "company name", "entity"}:
                continue
            names.append(value)

    return unique_names(names)


def unique_names(names: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    clean_names: list[str] = []

    for name in names:
        clean_name = " ".join(name.strip().split())
        if not clean_name:
            continue

        key = clean_name.casefold()
        if key in seen:
            continue

        seen.add(key)
        clean_names.append(clean_name)

    return clean_names


def first_nonempty_cell(row: Sequence[str]) -> str:
    for cell in row:
        value = cell.strip()
        if value:
            return value
    return ""


def search_ofac_names(
    company_list: Iterable[str],
    progress: ProgressCallback | None = None,
    result_callback: ResultCallback | None = None,
    report_dir: Path | str | None = None,
    *,
    cancel_event: Event | None = None,
    pacing: SearchPacing | None = None,
) -> list[OfacResult]:
    companies = unique_names(company_list)
    if not companies:
        raise ValueError("No company or person names were provided.")

    directory = Path(report_dir) if report_dir else None
    requests = [(company, directory / default_pdf_name(company) if directory else None) for company in companies]

    def on_result(_index: int, result: OfacResult) -> None:
        if result_callback:
            result_callback(result)

    return run_batch(requests, progress, on_result, cancel_event, unique_paths=True, pacing=pacing)


def save_ofac_pdfs(
    pdf_requests: Iterable[PdfRequest],
    progress: ProgressCallback | None = None,
    result_callback: PdfResultCallback | None = None,
    *,
    cancel_event: Event | None = None,
) -> list[OfacResult]:
    requests = [(company.strip(), Path(path)) for company, path in pdf_requests if company.strip()]
    if not requests:
        raise ValueError("No PDF save requests were provided.")

    return run_batch(requests, progress, result_callback, cancel_event)


def batch_ofac_check(
    company_list: Iterable[str],
    save_location: Path | str | None = None,
    progress: ProgressCallback | None = None,
    *,
    cancel_event: Event | None = None,
) -> list[OfacResult]:
    return search_ofac_names(company_list, progress=progress, report_dir=save_location, cancel_event=cancel_event)


def run_batch(
    requests: Sequence[tuple[str, Path | None]],
    progress: ProgressCallback | None,
    result_callback: PdfResultCallback | None,
    cancel_event: Event | None,
    *,
    unique_paths: bool = False,
    pacing: SearchPacing | None = None,
) -> list[OfacResult]:
    cancel = cancel_event or Event()
    pacing = pacing or SearchPacing()

    def notify(message: str) -> None:
        if progress:
            progress(message)

    results: list[OfacResult] = []
    try:
        if cancel.is_set():
            raise CheckCancelled
        with sync_playwright() as playwright:
            notify("Starting browser engine…")
            browser = playwright.chromium.launch(headless=True, timeout=10000)
            try:
                page = browser.new_page()
                page.set_default_timeout(5000)
                page.set_default_navigation_timeout(SITE_TIMEOUT_MS)
                form_ready = False

                for index, (company, pdf_path) in enumerate(requests):
                    for attempt in range(SEARCH_ATTEMPTS):
                        try:
                            if not form_ready:
                                pacing.wait(cancel, notify)
                                notify("Opening OFAC Sanctions List Search…")
                                open_ofac(page)
                                form_ready = True
                            pacing.wait(cancel, notify)
                            notify(f"Searching {index + 1} of {len(requests)}")
                            result = search_company(page, company)
                            break
                        except (OfacUnavailable, PlaywrightError) as exc:
                            backoff = 2
                            if isinstance(exc, OfacUnavailable):
                                pacing.postpone(exc.retry_after)
                                if exc.rate_limited:
                                    pacing.interval = min(max(pacing.interval * 2, SEARCH_INTERVAL), 8.0)
                                    backoff = RATE_LIMIT_BACKOFF
                            pacing.postpone(backoff * 2**attempt)
                            if cancel.is_set():
                                raise CheckCancelled from exc
                            if isinstance(exc, OfacUnavailable) and not exc.retryable:
                                raise
                            if not browser.is_connected() or page.is_closed():
                                raise OfacUnavailable("The OFAC browser closed unexpectedly.") from exc

                            form_ready = False
                            reason = short_error(exc)
                            wait_seconds = pacing.seconds_remaining()
                            if wait_seconds > MAX_AUTO_WAIT:
                                raise OfacUnavailable(
                                    f"{reason}. Try resuming in {ceil(wait_seconds)} seconds."
                                ) from exc
                            if attempt + 1 == SEARCH_ATTEMPTS:
                                raise OfacUnavailable(f"{reason}. The search did not recover after retrying.") from exc
                            notify(f"{reason}. Waiting to retry…")

                    if pdf_path is not None and result.error is None:
                        try:
                            target = unique_pdf_path(pdf_path) if unique_paths else pdf_path
                            write_pdf(page, target)
                            result = replace(result, pdf_path=target)
                        except (OSError, PlaywrightError) as exc:
                            raise OfacUnavailable(f"PDF could not be saved for {company}. {short_error(exc)}") from exc

                    results.append(result)
                    if result_callback:
                        result_callback(index, result)
            finally:
                browser.close()
    except CheckCancelled:
        notify(f"Cancelled after {len(results)} of {len(requests)}")
    else:
        notify("Check complete")

    return results


def validate_response(response: Response | None) -> None:
    if response is None:
        raise OfacUnavailable("OFAC did not return a search page", retryable=True)
    if not response.ok:
        status = response.status
        detail = " (rate limited)" if status == 429 else ""
        raise OfacUnavailable(
            f"OFAC returned HTTP {status}{detail}",
            retryable=status in RETRYABLE_HTTP_CODES,
            retry_after=retry_after_seconds(response.headers.get("retry-after")),
            rate_limited=status == 429,
        )


def open_ofac(page: Page) -> None:
    validate_response(page.goto(OFAC_URL, wait_until="domcontentloaded"))
    if not page.locator(NAME_INPUT).count() or not page.locator(SEARCH_BUTTON).count():
        raise OfacUnavailable("OFAC did not display the search form.")


def search_company(page: Page, company: str) -> OfacResult:
    name_input = page.locator(NAME_INPUT)
    name_input.fill(company)
    if name_input.input_value() != company:
        return OfacResult(company, "Error", "", error="The OFAC search field could not accept the full name.")

    # The form posts back to the same URL, so an existing URL or result label cannot identify a new lookup.
    with page.expect_event("domcontentloaded", timeout=SITE_TIMEOUT_MS):
        with page.expect_response(
            lambda response: (
                response.request.is_navigation_request()
                and response.request.method == "POST"
                and response.url == OFAC_URL
            ),
            timeout=SITE_TIMEOUT_MS,
        ) as submitted:
            page.locator(SEARCH_BUTTON).click(no_wait_after=True)
        validate_response(submitted.value)

    result_text = page.locator(RESULT_LABEL).inner_text().strip()
    count = re.fullmatch(r"Lookup Results:\s*([0-9]+(?:,[0-9]{3})*)\s+Found", result_text)
    if count is None or name_input.input_value() != company:
        raise OfacUnavailable("OFAC did not return a verified result for the submitted name", retryable=True)

    status = "Clean" if int(count[1].replace(",", "")) == 0 else "Review needed"
    return OfacResult(company, status, result_text)


def retry_after_seconds(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return max(0, int(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, OverflowError):
            return 0.0


def write_pdf(page: Page, pdf_path: Path) -> None:
    pdf = page.pdf(
        landscape=True,
        format="Legal",
        scale=0.84,
        print_background=False,
        margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
    )
    with staged_pdf_path(pdf_path) as temporary_path:
        temporary_path.write_bytes(pdf)


@contextmanager
def staged_pdf_path(pdf_path: Path) -> Iterator[Path]:
    pdf_path.parent.mkdir(parents=True, exist_ok=True)

    with NamedTemporaryFile(dir=pdf_path.parent, suffix=".tmp", delete=False) as file:
        temporary_path = Path(file.name)
    try:
        yield temporary_path
        temporary_path.replace(pdf_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def default_pdf_name(company: str) -> str:
    today = datetime.now().strftime("%m%d%Y")
    return f"OFAC {safe_filename(company)} {today}.pdf"


def safe_filename(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)[:100].strip().rstrip(".")
    return cleaned or "Unnamed"


def unique_pdf_path(path: Path) -> Path:
    if not path.exists():
        return path

    index = 2
    while True:
        candidate = path.with_name(f"{path.stem} ({index}){path.suffix}")
        if not candidate.exists():
            return candidate
        index += 1


def short_error(exc: Exception) -> str:
    return next(iter(str(exc).splitlines()), "") or type(exc).__name__
