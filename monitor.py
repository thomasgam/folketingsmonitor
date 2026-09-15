#!/usr/bin/env python3
"""
Folketingsmonitor – TV2 Østjylland
-----------------------------------
Tjekker dagligt Folketingets åbne data-API (oda.ft.dk) for nye sager og
dokumenter, der omtaler eller henviser til "TV2 Østjylland", og skriver en
dansk rapport til reports/<dato>.md. Sender også en e-mail via SMTP, hvis
de nødvendige miljøvariabler er sat (se README.md).

Bruger UDELUKKENDE oda.ft.dk (Folketingets officielle OData-API), da
www.ft.dk (både HTML-sider og PDF'er) er beskyttet af Cloudflare og ikke
kan tilgås af automatiske scripts. oda.ft.dk er ikke Cloudflare-beskyttet.

Vilkår for brug af Folketingets åbne data:
https://www.ft.dk/-/media/sites/ft/pdf/dokumenter/aabne-data/vilkaar_for_brug_af_aabne_data.ashx
"""
from __future__ import annotations

import json
import os
import smtplib
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python <3.9 fallback
    ZoneInfo = None  # type: ignore

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

API_BASE = "https://oda.ft.dk/api"
STATE_FILE = Path("state.json")
REPORTS_DIR = Path("reports")
TIMEZONE = "Europe/Copenhagen"
PAGE_SIZE = 100  # oda.ft.dk's standardsidestørrelse
REQUEST_TIMEOUT = 30
REQUEST_DELAY = 0.3  # høflig pause mellem kald (sekunder)

# Søgeord der udløser et match (case-insensitive substring-match).
# Bemærk: "tv2ostjylland" fanger også henvisninger til tv2ostjylland.dk-links.
KEYWORDS = [
    "tv2 østjylland",
    "tv2østjylland",
    "tv2 øj",
    "tv2øj",
    "tv2ostjylland",
]

# Oversættelse af Sag.nummerprefix til en forståelig sagstype.
PREFIX_MAP = {
    "L": "Lovforslag",
    "B": "Beslutningsforslag",
    "F": "Forespørgsel",
    "S": "§ 20-spørgsmål (spørgsmål til minister)",
    "V": "Forslag til vedtagelse",
    "R": "Redegørelse",
    "KOM": "EU-/Kommissionsforslag",
    "Aktstk.": "Aktstykke",
    "": "Udvalgssag (Alm. del)",
}

# Hvor langt tilbage der skal kigges ved allerførste kørsel (ingen state.json endnu).
FIRST_RUN_LOOKBACK_HOURS = 48
# Ekstra buffer lagt til det gemte cutoff-tidspunkt, så vi ikke misser noget,
# der lige akkurat blev opdateret i sekunderne omkring sidste kørsel.
OVERLAP_BUFFER_MINUTES = 5


# ---------------------------------------------------------------------------
# Hjælpefunktioner: tid
# ---------------------------------------------------------------------------

def now_local() -> datetime:
    """Nuværende tidspunkt i dansk lokal tid (naiv, uden offset) —
    oda.ft.dk's datofelter er selv naive og i dansk lokal tid."""
    if ZoneInfo is not None:
        return datetime.now(ZoneInfo(TIMEZONE)).replace(tzinfo=None)
    return datetime.utcnow() + timedelta(hours=2)  # grov fallback (CEST)


def fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------------------
# oda.ft.dk klient
# ---------------------------------------------------------------------------

def odata_get_all(entity: str, filter_str: str, select: str) -> list[dict[str, Any]]:
    """Henter ALLE poster for en OData-forespørgsel, med paginering.

    VIGTIGT: brug kun ét simpelt filter-udtryk (fx "felt ge datetime'X'" eller
    "substringof('x', felt)") — oda.ft.dk's ældre OData-implementering
    returnerer forkerte/urelaterede resultater, hvis man kombinerer en
    dato-betingelse med "and" og en parentes af flere "or"-betingelser i
    samme $filter. Denne funktion henter derfor bredt på ét simpelt filter
    (typisk kun på dato) og overlader selve nøgleords-matchningen til Python
    (se `matches_keyword`), hvilket er langt mere robust.
    """
    results: list[dict[str, Any]] = []
    skip = 0
    while True:
        params = {
            "$format": "json",
            "$filter": filter_str,
            "$select": select,
            "$top": PAGE_SIZE,
        }
        if skip:
            params["$skip"] = skip
        resp = requests.get(f"{API_BASE}/{entity}", params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        batch = resp.json().get("value", [])
        results.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        skip += PAGE_SIZE
        time.sleep(REQUEST_DELAY)
    return results


def odata_get_one(entity: str, record_id: int) -> dict[str, Any] | None:
    resp = requests.get(
        f"{API_BASE}/{entity}({record_id})", params={"$format": "json"}, timeout=REQUEST_TIMEOUT
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def odata_get_filtered(entity: str, filter_str: str, select: str, top: int = 5,
                        orderby: str | None = None) -> list[dict[str, Any]]:
    params = {"$format": "json", "$filter": filter_str, "$select": select, "$top": top}
    if orderby:
        params["$orderby"] = orderby
    resp = requests.get(f"{API_BASE}/{entity}", params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json().get("value", [])


# ---------------------------------------------------------------------------
# Matchning
# ---------------------------------------------------------------------------

def matches_keyword(*texts: str | None) -> str | None:
    """Returnerer det første søgeord der matcher i en af de givne tekster, ellers None."""
    for text in texts:
        if not text:
            continue
        lowered = text.lower()
        for kw in KEYWORDS:
            if kw in lowered:
                return kw
    return None


# ---------------------------------------------------------------------------
# Datamodel
# ---------------------------------------------------------------------------

@dataclass
class Match:
    sag_id: int
    nummer: str
    nummerprefix: str
    titel: str
    dato: str
    reason: str
    link: str | None = None


# ---------------------------------------------------------------------------
# Kernelogik
# ---------------------------------------------------------------------------

def find_sag_matches(cutoff: str) -> dict[int, Match]:
    """Henter alle Sag opdateret siden cutoff, og finder dem der matcher et søgeord
    i titel, begrundelse eller resume."""
    sager = odata_get_all(
        "Sag",
        f"opdateringsdato ge datetime'{cutoff}'",
        select="id,titel,nummer,nummerprefix,opdateringsdato,begrundelse,resume",
    )
    matches: dict[int, Match] = {}
    for sag in sager:
        reason_kw = matches_keyword(sag.get("titel"), sag.get("begrundelse"), sag.get("resume"))
        if not reason_kw:
            continue
        if sag.get("begrundelse") and reason_kw.replace(" ", "") in (sag["begrundelse"] or "").lower().replace(" ", ""):
            reason = f'nævnt i begrundelsen ("{reason_kw}")'
        else:
            reason = f'nævnt i sagens titel/spørgsmålstekst ("{reason_kw}")'
        matches[sag["id"]] = Match(
            sag_id=sag["id"],
            nummer=sag.get("nummer") or "",
            nummerprefix=(sag.get("nummerprefix") or "").strip(),
            titel=sag.get("titel") or "",
            dato=sag.get("opdateringsdato") or "",
            reason=reason,
        )
    return matches


def find_dokument_matches(cutoff: str, existing: dict[int, Match]) -> dict[int, Match]:
    """Henter alle Dokument oprettet siden cutoff, finder dem der matcher et
    søgeord i selve dokumenttitlen, og slår sagen op for dem der endnu ikke
    er fundet via find_sag_matches."""
    dokumenter = odata_get_all(
        "Dokument", f"dato ge datetime'{cutoff}'", select="id,titel,dato"
    )
    for dok in dokumenter:
        reason_kw = matches_keyword(dok.get("titel"))
        if not reason_kw:
            continue
        sag_id = _resolve_sag_id_for_dokument(dok["id"])
        if sag_id is None or sag_id in existing:
            continue
        sag = odata_get_one("Sag", sag_id)
        if not sag:
            continue
        existing[sag_id] = Match(
            sag_id=sag_id,
            nummer=sag.get("nummer") or "",
            nummerprefix=(sag.get("nummerprefix") or "").strip(),
            titel=sag.get("titel") or "",
            dato=dok.get("dato") or "",
            reason=f'dokumenttitlen nævner "{reason_kw}" ("{dok.get("titel")}")',
        )
    return existing


def _resolve_sag_id_for_dokument(dokument_id: int) -> int | None:
    rows = odata_get_filtered(
        "SagDokument", f"dokumentid eq {dokument_id}", select="sagid", top=1
    )
    return rows[0]["sagid"] if rows else None


def find_link_for_sag(sag_id: int) -> str | None:
    """Finder et direkte PDF-link til det nyeste dokument knyttet til sagen."""
    try:
        links = odata_get_filtered(
            "SagDokument",
            f"sagid eq {sag_id}",
            select="dokumentid",
            top=3,
            orderby="opdateringsdato desc",
        )
        for row in links:
            files = odata_get_filtered(
                "Fil", f"dokumentid eq {row['dokumentid']}", select="filurl", top=1
            )
            if files and files[0].get("filurl"):
                return files[0]["filurl"]
    except requests.RequestException:
        pass
    return None


def translate_nummer(prefix: str, nummer: str) -> str:
    label = PREFIX_MAP.get(prefix, f"({prefix})" if prefix else PREFIX_MAP[""])
    if not nummer:
        return label
    return f"{nummer} ({label})"


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_cutoff() -> str:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            return data["cutoff"]
        except (json.JSONDecodeError, KeyError):
            pass
    return fmt(now_local() - timedelta(hours=FIRST_RUN_LOOKBACK_HOURS))


def save_cutoff(cutoff: str) -> None:
    STATE_FILE.write_text(json.dumps({"cutoff": cutoff, "saved_at": fmt(now_local())}, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Rapport
# ---------------------------------------------------------------------------

def build_report(matches: list[Match], checked_counts: dict[str, int], errors: list[str]) -> str:
    today = now_local().strftime("%-d. %B %Y") if os.name != "nt" else now_local().strftime("%d. %B %Y")
    lines = [f"# Dagligt overblik – Folketinget om TV2 Østjylland – {today}", ""]

    if errors:
        for err in errors:
            lines.append(f"⚠️ {err}")
        lines.append("")

    if not matches:
        if not errors:
            lines.append("Ingen nye Folketings-sager eller -dokumenter siden sidste tjek, der omtaler TV2 Østjylland.")
        else:
            lines.append("Ingen match fundet i det, der kunne tjekkes (se fejl ovenfor).")
    else:
        matches_sorted = sorted(matches, key=lambda m: m.dato, reverse=True)
        for m in matches_sorted:
            lines.append(f"## {translate_nummer(m.nummerprefix, m.nummer)}")
            lines.append(f"- **Dato:** {m.dato}")
            lines.append(f"- **Titel:** {m.titel}")
            lines.append(f"- **Hvorfor den matcher:** {m.reason}")
            if m.link:
                lines.append(f"- **Link:** {m.link}")
            lines.append("")

    lines.append("---")
    lines.append(
        f"Tjekket i alt: {checked_counts.get('sager', 0)} sager og "
        f"{checked_counts.get('dokumenter', 0)} dokumenter opdateret siden sidste kørsel."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# E-mail (valgfri – kun hvis miljøvariabler er sat)
# ---------------------------------------------------------------------------

def maybe_send_email(subject: str, body_markdown: str) -> None:
    # Bemærk: GitHub Actions sætter miljøvariabler for ikke-udfyldte secrets til en
    # TOM streng (""), ikke til at de mangler helt. Derfor bruges "or" i stedet for
    # dict.get()'s default-parameter nedenfor — ellers ville fx SMTP_PORT="" aldrig
    # falde tilbage til "587", og int("") ville fejle med en uventet exception.
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    mail_to = os.environ.get("MAIL_TO")

    if not all([host, user, password, mail_to]):
        print("SMTP ikke konfigureret (mangler en eller flere af SMTP_HOST/SMTP_USER/SMTP_PASS/MAIL_TO) – springer e-mail over.")
        return

    mail_from = os.environ.get("MAIL_FROM") or user
    port = int(os.environ.get("SMTP_PORT") or "587")

    msg = MIMEText(body_markdown, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = mail_to

    with smtplib.SMTP(host, port, timeout=REQUEST_TIMEOUT) as server:
        server.starttls()
        server.login(user, password)
        server.sendmail(mail_from, [mail_to], msg.as_string())
    print(f"E-mail sendt til {mail_to}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    run_started_at = fmt(now_local())
    cutoff = load_cutoff()
    print(f"Tjekker sager/dokumenter opdateret siden {cutoff} ...")

    errors: list[str] = []
    matches: dict[int, Match] = {}
    checked_counts = {"sager": 0, "dokumenter": 0}

    # --- Sag ---
    try:
        sag_raw = odata_get_all(
            "Sag", f"opdateringsdato ge datetime'{cutoff}'",
            select="id,titel,nummer,nummerprefix,opdateringsdato,begrundelse,resume",
        )
        checked_counts["sager"] = len(sag_raw)
        for sag in sag_raw:
            reason_kw = matches_keyword(sag.get("titel"), sag.get("begrundelse"), sag.get("resume"))
            if not reason_kw:
                continue
            in_begrundelse = bool(sag.get("begrundelse")) and reason_kw in (sag.get("begrundelse") or "").lower()
            reason = (
                f'nævnt i begrundelsen ("{reason_kw}")' if in_begrundelse
                else f'nævnt i sagens titel/spørgsmålstekst ("{reason_kw}")'
            )
            matches[sag["id"]] = Match(
                sag_id=sag["id"],
                nummer=sag.get("nummer") or "",
                nummerprefix=(sag.get("nummerprefix") or "").strip(),
                titel=sag.get("titel") or "",
                dato=sag.get("opdateringsdato") or "",
                reason=reason,
            )
    except requests.RequestException as exc:
        errors.append(f"Kunne ikke tjekke Sag-data i dag pga. teknisk fejl: {exc}")

    # --- Dokument ---
    try:
        dok_raw = odata_get_all("Dokument", f"dato ge datetime'{cutoff}'", select="id,titel,dato")
        checked_counts["dokumenter"] = len(dok_raw)
        for dok in dok_raw:
            reason_kw = matches_keyword(dok.get("titel"))
            if not reason_kw:
                continue
            try:
                sag_id = _resolve_sag_id_for_dokument(dok["id"])
            except requests.RequestException:
                sag_id = None
            if sag_id is None or sag_id in matches:
                continue
            sag = odata_get_one("Sag", sag_id)
            if not sag:
                continue
            matches[sag_id] = Match(
                sag_id=sag_id,
                nummer=sag.get("nummer") or "",
                nummerprefix=(sag.get("nummerprefix") or "").strip(),
                titel=sag.get("titel") or "",
                dato=dok.get("dato") or "",
                reason=f'dokumenttitlen nævner "{reason_kw}"',
            )
    except requests.RequestException as exc:
        errors.append(f"Kunne ikke tjekke Dokument-data i dag pga. teknisk fejl: {exc}")

    # --- Links ---
    for m in matches.values():
        m.link = find_link_for_sag(m.sag_id)

    report = build_report(list(matches.values()), checked_counts, errors)

    REPORTS_DIR.mkdir(exist_ok=True)
    report_path = REPORTS_DIR / f"{now_local().strftime('%Y-%m-%d')}.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"Rapport skrevet til {report_path}")
    print()
    print(report)

    # Kun ryk cutoff frem hvis der ikke var fejl der forhindrede en fuld tjekning,
    # så vi ikke misser noget ved en forbigående fejl.
    if not errors:
        new_cutoff = fmt(now_local() - timedelta(minutes=OVERLAP_BUFFER_MINUTES))
        save_cutoff(new_cutoff)
    else:
        print("Ryk ikke state.json frem pga. fejl ovenfor – prøver samme periode igen næste gang.")

    maybe_send_email(
        subject=f"Folketingsmonitor TV2 Østjylland – {now_local().strftime('%Y-%m-%d')}",
        body_markdown=report,
    )

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
