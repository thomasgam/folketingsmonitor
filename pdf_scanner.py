#!/usr/bin/env python3
"""
Folketingsmonitor – PDF-dybdescanner
-------------------------------------
Går igennem Folketingets side "Nyeste dokumenter" med en RIGTIG (headless)
browser (Playwright), åbner hvert nyt dokument, henter de tilknyttede
PDF'er, og scanner selve PDF-teksten for søgeord om TV2 Østjylland.

Dette er et supplement til monitor.py (som bruger oda.ft.dk-API'et og kun
kan se titel/begrundelse-felterne). Denne scanner læser den FULDE tekst i
selve dokumenterne — inkl. svar, bilag m.m. — men er teknisk mere skrøbelig,
se afsnittet "Vigtigt om Cloudflare" i README.md.

Fremgangsmåde (jf. den CSS-baserede model):
  A) Åbn https://www.ft.dk/da/dokumenter/dokumentlister/nyeste-dokumenter
  B) Find alle dokument-links i tabellen (kolonnen "Titel")
  C) Spring dem over, vi allerede har tjekket før (gemt i pdf_seen.json)
  D) Åbn hver ny dokumentside
  E) Find PDF-linket/-linkene der (".no-border a")
  F) Hent PDF'en og søg efter søgeordene i selve teksten
"""
from __future__ import annotations

import io
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright
from pypdf import PdfReader

LISTING_URL = "https://www.ft.dk/da/dokumenter/dokumentlister/nyeste-dokumenter"
SEEN_FILE = Path("pdf_seen.json")
REPORTS_DIR = Path("reports")

MAX_PAGES_PER_RUN = 30          # sikkerhedsnet: maks. antal sider (à 25 rækker) vi bladrer igennem pr. kørsel
CLOUDFLARE_WAIT_SECONDS = 12    # maks. ventetid på at Cloudflares sikkerhedstjek klarer sig selv
NAV_TIMEOUT_MS = 30_000
POLITE_DELAY_SECONDS = 1.0      # lille pause mellem sidekald, så vi ikke banker løs på ft.dk

# Samme søgeord som i monitor.py — case-insensitive substring-match.
KEYWORDS = [
    "tv2 østjylland",
    "tv2østjylland",
    "tv2 øj",
    "tv2øj",
    "tv2ostjylland",
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


@dataclass
class DocRow:
    doc_id: str
    nr: str
    titel: str
    dato: str
    url: str


@dataclass
class PdfMatch:
    doc: DocRow
    pdf_url: str
    pdf_titel: str
    keyword: str
    snippet: str


def doc_id_from_url(url: str) -> str:
    """Bruger selve URL'en (uden query/fragment) som unik nøgle for et dokument."""
    return url.split("?")[0].rstrip("/")


def load_seen() -> set[str]:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return set()


def save_seen(seen: set[str]) -> None:
    SEEN_FILE.write_text(json.dumps(sorted(seen), indent=2, ensure_ascii=False), encoding="utf-8")


def wait_for_cloudflare(page: Page, expect_selector: str) -> bool:
    """Venter på at Cloudflares "Et øjeblik ..."-side klarer sig selv og den
    rigtige side dukker op. Returnerer True hvis det lykkedes."""
    deadline = time.time() + CLOUDFLARE_WAIT_SECONDS
    while time.time() < deadline:
        if page.locator(expect_selector).count() > 0:
            return True
        time.sleep(0.5)
    return page.locator(expect_selector).count() > 0


def matches_keyword(text: str | None) -> tuple[str, str] | None:
    """Returnerer (søgeord, kontekst-uddrag) hvis et søgeord findes i teksten."""
    if not text:
        return None
    lowered = text.lower()
    for kw in KEYWORDS:
        idx = lowered.find(kw)
        if idx != -1:
            start = max(0, idx - 80)
            end = min(len(text), idx + len(kw) + 80)
            snippet = text[start:end].replace("\n", " ").strip()
            return kw, snippet
    return None


def extract_pdf_text(pdf_bytes: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as exc:  # defekt/krypteret PDF e.l.
        print(f"  ⚠️ Kunne ikke læse PDF-tekst: {exc}")
        return ""


def collect_new_rows(page: Page, seen: set[str]) -> tuple[list[DocRow], list[str]]:
    """Bladrer igennem 'Nyeste dokumenter'-listen, side for side, og
    returnerer de rækker der endnu ikke er i `seen`. Stopper så snart en hel
    side kun indeholder allerede-sete dokumenter (listen er nyest-først), da
    alt derefter nødvendigvis også allerede er set."""
    new_rows: list[DocRow] = []
    errors: list[str] = []

    print(f"Åbner {LISTING_URL} ...")
    page.goto(LISTING_URL, timeout=NAV_TIMEOUT_MS)
    if not wait_for_cloudflare(page, "table tbody tr"):
        errors.append("Kunne ikke komme forbi Cloudflares sikkerhedstjek på dokumentlisten.")
        return new_rows, errors

    for page_num in range(1, MAX_PAGES_PER_RUN + 1):
        rows = page.query_selector_all("table tbody tr")
        page_had_new = False
        for row in rows:
            cells = row.query_selector_all("td")
            if len(cells) < 3:
                continue
            link_el = cells[0].query_selector("a")
            if not link_el:
                continue
            href = link_el.get_attribute("href") or ""
            if not href:
                continue
            nr_text = link_el.inner_text().strip()
            titel = cells[1].inner_text().strip()
            dato = cells[2].inner_text().strip()
            key = doc_id_from_url(href)
            if key in seen:
                continue
            page_had_new = True
            new_rows.append(DocRow(doc_id=key, nr=nr_text, titel=titel, dato=dato, url=href))

        print(f"  Side {page_num}: {len(rows)} rækker, {sum(1 for r in new_rows if True)} nye i alt indtil videre.")
        if not page_had_new:
            break  # resten af listen (ældre) er allerede kendt

        next_link = page.query_selector("a:has-text('Næste side')")
        if not next_link:
            break
        time.sleep(POLITE_DELAY_SECONDS)
        next_link.click()
        try:
            page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            pass
        if not wait_for_cloudflare(page, "table tbody tr"):
            errors.append(f"Cloudflare blokerede side {page_num + 1} af dokumentlisten undervejs.")
            break

    return new_rows, errors


def scan_document(page: Page, row: DocRow) -> tuple[list[PdfMatch], list[str]]:
    matches: list[PdfMatch] = []
    errors: list[str] = []
    try:
        page.goto(row.url, timeout=NAV_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        errors.append(f"Timeout ved åbning af {row.url}")
        return matches, errors

    if not wait_for_cloudflare(page, ".no-border a, body"):
        errors.append(f"Cloudflare blokerede dokumentsiden: {row.url}")
        return matches, errors

    pdf_links = page.query_selector_all(".no-border a")
    for link in pdf_links:
        pdf_url = link.get_attribute("href") or ""
        pdf_titel = link.inner_text().strip()
        if not pdf_url.lower().endswith(".pdf"):
            continue
        try:
            resp = page.request.get(pdf_url, timeout=NAV_TIMEOUT_MS)
            if not resp.ok:
                errors.append(f"Kunne ikke hente PDF ({resp.status}): {pdf_url}")
                continue
            pdf_bytes = resp.body()
        except Exception as exc:
            errors.append(f"Fejl ved hentning af PDF {pdf_url}: {exc}")
            continue

        text = extract_pdf_text(pdf_bytes)
        hit = matches_keyword(text) or matches_keyword(pdf_titel)
        if hit:
            kw, snippet = hit
            matches.append(PdfMatch(doc=row, pdf_url=pdf_url, pdf_titel=pdf_titel, keyword=kw, snippet=snippet))
        time.sleep(POLITE_DELAY_SECONDS)

    return matches, errors


def build_report(matches: list[PdfMatch], checked: int, errors: list[str]) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [f"# PDF-dybdescan – Folketinget om TV2 Østjylland – {today}", ""]

    if errors:
        for err in errors:
            lines.append(f"⚠️ {err}")
        lines.append("")

    if not matches:
        lines.append(
            "Ingen match i selve dokumentteksten blandt de nye dokumenter, der blev scannet."
            if not errors else
            "Ingen match fundet i det, der kunne nås (se fejl ovenfor)."
        )
    else:
        for m in matches:
            lines.append(f"## {m.doc.nr}")
            lines.append(f"- **Dato:** {m.doc.dato}")
            lines.append(f"- **Titel:** {m.doc.titel}")
            lines.append(f"- **PDF:** {m.pdf_titel}")
            lines.append(f"- **Fundet søgeord:** \"{m.keyword}\"")
            lines.append(f"- **Uddrag:** …{m.snippet}…")
            lines.append(f"- **Dokumentside:** {m.doc.url}")
            lines.append(f"- **PDF-link:** {m.pdf_url}")
            lines.append("")

    lines.append("---")
    lines.append(f"Nye dokumenter scannet i dag: {checked}.")
    return "\n".join(lines)


def main() -> int:
    seen = load_seen()
    all_errors: list[str] = []
    all_matches: list[PdfMatch] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1366, "height": 900},
            locale="da-DK",
            timezone_id="Europe/Copenhagen",
        )
        page = context.new_page()

        new_rows, listing_errors = collect_new_rows(page, seen)
        all_errors.extend(listing_errors)
        print(f"{len(new_rows)} nye dokumenter fundet siden sidst.")

        for i, row in enumerate(new_rows, start=1):
            print(f"[{i}/{len(new_rows)}] Scanner {row.nr}: {row.titel[:70]} ...")
            matches, errors = scan_document(page, row)
            all_matches.extend(matches)
            all_errors.extend(errors)
            seen.add(row.doc_id)  # markér som set, uanset match, så vi ikke tjekker den igen
            time.sleep(POLITE_DELAY_SECONDS)

        browser.close()

    save_seen(seen)

    report = build_report(all_matches, len(new_rows), all_errors)
    REPORTS_DIR.mkdir(exist_ok=True)
    report_path = REPORTS_DIR / f"pdf-scan-{datetime.now().strftime('%Y-%m-%d')}.md"
    report_path.write_text(report, encoding="utf-8")
    print()
    print(report)
    print(f"\nRapport skrevet til {report_path}")

    return 1 if all_errors and not all_matches else 0


if __name__ == "__main__":
    sys.exit(main())
