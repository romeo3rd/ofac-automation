from __future__ import annotations

import csv
import re
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from html import escape
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Event
from typing import Callable, Iterable, Iterator, Sequence

from playwright.sync_api import Error as PlaywrightError, Page, sync_playwright

from sanctions_data import (
    SANCTIONS_LISTS,
    CheckCancelled,
    SanctionsMatch,
    SanctionsSource,
    check_cancelled,
    download_list,
    index_list,
    name_key,
)

ProgressCallback = Callable[[str], None]
ResultCallback = Callable[["OfacResult"], None]
PdfResultCallback = Callable[[int, "OfacResult"], None]
OFAC_DETAILS_URL = "https://sanctionssearch.ofac.treas.gov/Details.aspx"


@dataclass(frozen=True)
class OfacResult:
    company: str
    status: str
    result_text: str
    pdf_path: Path | None = None
    error: str | None = None

    @property
    def display_status(self) -> str:
        return "No exact match" if self.status == "Clean" else self.status


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
) -> list[OfacResult]:
    companies = unique_names(company_list)
    if not companies:
        raise ValueError("No company or person names were provided.")

    directory = Path(report_dir) if report_dir else None
    requests = [(company, directory / default_pdf_name(company) if directory else None) for company in companies]

    def on_result(_index: int, result: OfacResult) -> None:
        if result_callback:
            result_callback(result)

    return run_batch(requests, progress, on_result, cancel_event, unique_paths=True)


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
) -> list[OfacResult]:
    cancel = cancel_event or Event()

    def notify(message: str) -> None:
        if progress:
            progress(message)

    results: list[OfacResult] = []
    keys = [name_key(company) for company, _ in requests]
    matches_by_name: dict[str, list[SanctionsMatch]] = {key: [] for key in keys if key}
    sources: list[SanctionsSource] = []

    try:
        for name, url in SANCTIONS_LISTS:
            notify(f"Downloading OFAC {name} list…")
            with download_list(name, url, cancel, notify) as data:
                notify(f"Reading OFAC {name} list…")
                published = index_list(data, name, matches_by_name, cancel)
            sources.append(SanctionsSource(name, url, published))
        check_cancelled(cancel)

        checked_at = datetime.now().astimezone().isoformat(sep=" ", timespec="seconds")
        with ExitStack() as stack:
            page = None
            if any(path is not None for _, path in requests):
                notify("Starting PDF renderer…")
                playwright = stack.enter_context(sync_playwright())
                browser = playwright.chromium.launch(headless=True, timeout=10000)
                stack.callback(browser.close)
                page = browser.new_page(offline=True)

            for index, ((company, pdf_path), key) in enumerate(zip(requests, keys)):
                check_cancelled(cancel)
                notify(f"Checking {index + 1} of {len(requests)}")

                if not key:
                    result = OfacResult(company, "Error", "", error="Enter a name containing letters or numbers.")
                else:
                    matches = matches_by_name[key]
                    match_count = len(matches)
                    match_text = "1 potential match" if match_count == 1 else f"{match_count} potential matches"
                    result = OfacResult(
                        company=company,
                        status="Review needed" if matches else "Clean",
                        result_text=match_text if matches else "0 exact matches found",
                    )
                    if page is not None and pdf_path is not None:
                        try:
                            target = unique_pdf_path(pdf_path) if unique_paths else pdf_path
                            write_pdf(page, target, result, matches, sources, checked_at)
                            result = replace(result, pdf_path=target)
                        except (OSError, PlaywrightError) as exc:
                            result = replace(result, error=f"PDF could not be saved. {short_error(exc)}")

                results.append(result)
                if result_callback:
                    result_callback(index, result)
    except CheckCancelled:
        notify(f"Cancelled after {len(results)} of {len(requests)}")
    else:
        notify("Check complete")

    return results


def write_pdf(
    page: Page,
    pdf_path: Path,
    result: OfacResult,
    matches: Sequence[SanctionsMatch],
    sources: Sequence[SanctionsSource],
    checked_at: str,
) -> None:
    sources_html = "".join(
        f'<li><a href="{escape(source.url)}">{escape(source.name)} list</a> '
        f'published {escape(source.published)}</li>'
        for source in sources
    )
    matches_html = ""
    if matches:
        rows = "".join(
            f"<tr><td>{escape(match.listed_name)}</td><td>{escape(match.matched_name)}<br>"
            f"<small>{escape(match.name_type)}</small></td><td>{escape(match.source)}<br>"
            f'<small><a href="{OFAC_DETAILS_URL}?id={escape(match.uid)}">UID {escape(match.uid)}</a></small></td>'
            f"<td>{escape(match.entity_type)}</td><td>{escape(match.programs)}</td>"
            f"<td>{''.join(f'<div>{escape(address)}</div>' for address in match.addresses)}</td></tr>"
            for match in matches
        )
        matches_html = (
            "<table><thead><tr><th>Listed name</th><th>Matched name</th><th>List</th>"
            f"<th>Type</th><th>Programs</th><th>Addresses</th></tr></thead><tbody>{rows}</tbody></table>"
        )

    page.set_content(
        f"""<!doctype html>
<html lang="en"><meta charset="utf-8"><title>OFAC Automation screening record</title>
<style>
    @page {{ margin: 15mm; }}
    body {{ font: 14px Arial, sans-serif; color: #17242a; overflow-wrap: anywhere; }}
    h1 {{ font-size: 24px; margin: 0 0 16px; }}
    h2 {{ font-size: 18px; margin: 20px 0 8px; }}
    table {{ border-collapse: collapse; table-layout: fixed; width: 100%; }}
    th, td {{ border-bottom: 1px solid #d8e3e6; padding: 8px; text-align: left; vertical-align: top; }}
    th {{ background: #edf4f6; }}
    th:nth-child(-n+2) {{ width: 17%; }}
    th:nth-child(3) {{ width: 13%; }}
    th:nth-child(4) {{ width: 8%; }}
    th:nth-child(5) {{ width: 10%; }}
    td div + div {{ margin-top: 5px; }}
    tr {{ break-inside: avoid; }}
</style>
<h1>OFAC Automation screening record</h1>
<p>Screened on {escape(checked_at)}</p>
<h2>Name screened</h2><p>{escape(result.company)}</p>
<h2>{escape(result.display_status)}</h2><p>{escape(result.result_text)}</p>
{matches_html}
<h2>Source lists</h2><ul>{sources_html}</ul>
<p>Screening checks for exact matches to names and aliases after normalizing Unicode, case, punctuation, and spacing.</p>
</html>""",
        wait_until="domcontentloaded",
        timeout=10000,
    )
    pdf = page.pdf(landscape=True, format="Legal", print_background=True)
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
