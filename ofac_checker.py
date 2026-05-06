from __future__ import annotations

import csv
import re
import time
import random
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Sequence

from playwright.sync_api import Page, sync_playwright


OFAC_URL = "https://sanctionssearch.ofac.treas.gov/"
ProgressCallback = Callable[[str], None]
ResultCallback = Callable[["OfacResult"], None]
PdfResultCallback = Callable[[int, "OfacResult"], None]


@dataclass(frozen=True)
class OfacResult:
    company: str
    status: str
    result_text: str
    pdf_path: Path | None = None
    error: str | None = None


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
) -> list[OfacResult]:
    companies = unique_names(company_list)

    if not companies:
        raise ValueError("No company or person names were provided.")

    report_path = Path(report_dir) if report_dir else None
    if report_path:
        report_path.mkdir(parents=True, exist_ok=True)

    def notify(message: str) -> None:
        if progress:
            progress(message)

    results: list[OfacResult] = []

    with sync_playwright() as playwright:
        notify("Starting browser engine")
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        configure_page(page)

        try:
            open_ofac(page)

            for index, company in enumerate(companies, start=1):
                notify(f"Searching {index} of {len(companies)}: {company}")

                try:
                    result = search_company(page, company)
                    if report_path:
                        pdf_path = unique_pdf_path(report_path / default_pdf_name(company))
                        write_pdf(page, pdf_path)
                        result = OfacResult(
                            company=result.company,
                            status=result.status,
                            result_text=result.result_text,
                            pdf_path=pdf_path,
                        )
                except Exception as exc:
                    result = OfacResult(
                        company=company,
                        status="Error",
                        result_text="",
                        error=short_error(exc),
                    )

                results.append(result)
                if result_callback:
                    result_callback(result)
        finally:
            browser.close()
            notify("Search complete")

    return results


def save_ofac_pdfs(
    pdf_requests: Iterable[PdfRequest],
    progress: ProgressCallback | None = None,
    result_callback: PdfResultCallback | None = None,
) -> list[OfacResult]:
    requests = [(company.strip(), Path(path)) for company, path in pdf_requests if company.strip()]

    if not requests:
        raise ValueError("No PDF save requests were provided.")

    def notify(message: str) -> None:
        if progress:
            progress(message)

    results: list[OfacResult] = []

    with sync_playwright() as playwright:
        notify("Starting browser engine")
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        configure_page(page)

        try:
            open_ofac(page)

            for index, (company, pdf_path) in enumerate(requests):
                notify(f"Saving PDF {index + 1} of {len(requests)}: {company}")

                try:
                    result = search_company(page, company)
                    pdf_path.parent.mkdir(parents=True, exist_ok=True)
                    write_pdf(page, pdf_path)
                    result = OfacResult(
                        company=result.company,
                        status=result.status,
                        result_text=result.result_text,
                        pdf_path=pdf_path,
                    )
                except Exception as exc:
                    result = OfacResult(
                        company=company,
                        status="Error",
                        result_text="",
                        pdf_path=pdf_path,
                        error=short_error(exc),
                    )

                results.append(result)
                if result_callback:
                    result_callback(index, result)
        finally:
            browser.close()
            notify("PDF save complete")

    return results


def batch_ofac_check(
    company_list: Iterable[str],
    save_location: Path | str | None = None,
    progress: ProgressCallback | None = None,
) -> list[OfacResult]:
    companies = unique_names(company_list)
    if save_location is None:
        return search_ofac_names(companies, progress=progress)

    output_dir = Path(save_location)
    requests = [(company, output_dir / default_pdf_name(company)) for company in companies]
    return save_ofac_pdfs(requests, progress=progress)


def configure_page(page: Page) -> None:
    page.route(
        "**/*",
        lambda route: route.abort()
        if route.request.resource_type in ["image", "media"]
        else route.continue_(),
    )


def open_ofac(page: Page) -> None:
    page.goto(OFAC_URL, wait_until="domcontentloaded", timeout=60000)


def search_company(page: Page, company: str) -> OfacResult:
    page.fill("input[id$='txtLastName']", company)
    time.sleep(random.uniform(0.6, 1.0))
    page.click("input[id$='btnSearch']")
    page.wait_for_load_state("networkidle", timeout=60000)

    result_text = page.locator("#ctl00_MainContent_lblResults").inner_text()
    status = "Clean" if "0 Found" in result_text else "Review needed"

    return OfacResult(
        company=company,
        status=status,
        result_text=result_text,
    )


def write_pdf(page: Page, pdf_path: Path) -> None:
    page.pdf(
        path=str(pdf_path),
        landscape=True,
        format="Legal",
        scale=0.84,
        print_background=False,
        margin={
            "top": "0",
            "bottom": "0",
            "left": "0",
            "right": "0",
        },
    )


def default_pdf_name(company: str) -> str:
    today = datetime.now().strftime("%m%d%Y")
    return f"OFAC {safe_filename(company)} {today}.pdf"


def safe_filename(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]', "_", value).strip().rstrip(".")
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
    return str(exc).splitlines()[0] or exc.__class__.__name__
