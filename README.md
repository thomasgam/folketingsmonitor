# Folketingsmonitor – TV2 Østjylland

To Python-scripts der overvåger Folketinget (ft.dk) for nye sager og
dokumenter, der omtaler eller henviser til "TV2 Østjylland":

1. **`monitor.py`** – hurtig, robust daglig tjek via Folketingets åbne
   data-API. Scanner titel/begrundelse/resume-felterne.
2. **`pdf_scanner.py`** – dybere tjek der åbner selve dokumenterne på
   ft.dk med en rigtig (headless) browser og læser den fulde tekst i de
   vedhæftede PDF'er. Mere grundig, men teknisk mere skrøbelig — se
   afsnittet **"Vigtigt om Cloudflare"** nedenfor, inden I regner med den.

Begge scripts er uafhængige af hinanden og kan bruges hver for sig eller
sammen.

---

## 1) `monitor.py` – daglig tjek via API'et

`www.ft.dk` (både de almindelige sider og PDF-filerne) er beskyttet af
Cloudflare, som blokerer automatiske kald — kun en rigtig browser kommer
normalt igennem. Folketingets åbne data-API (`oda.ft.dk`) er derimod
bygget netop til programmatisk adgang og er ikke Cloudflare-beskyttet, så
dette script bruger udelukkende det. Se [vilkårene for brug af
Folketingets åbne
data](https://www.ft.dk/-/media/sites/ft/pdf/dokumenter/aabne-data/vilkaar_for_brug_af_aabne_data.ashx)
og [oda.ft.dk](https://oda.ft.dk/) for baggrund om selve API'et.

**Begrænsning:** scriptet scanner titel/begrundelse/resume-felterne i
Folketingets egne data om sager og dokumenter — ikke selve teksten inde i
vedhæftede PDF- eller HTML-dokumenter (bilag, svar). Det dækker de
tilfælde, hvor et medlem selv skriver "TV2 Østjylland" i spørgsmålet eller
begrundelsen — langt den mest almindelige måde den slags sager opstår.
Det er præcis det, `pdf_scanner.py` (se nedenfor) supplerer med.

### Sådan virker det

1. Scriptet husker, hvornår det sidst tjekkede (`state.json`, gemt i
   repoet). Første gang kigges der 48 timer tilbage.
2. Det henter alle `Sag` og `Dokument`, der er opdateret siden sidste
   tjek, og leder efter søgeordene "tv2 østjylland", "tv2østjylland",
   "tv2 øj", "tv2øj" og "tv2ostjylland" (fanger også links til
   tv2ostjylland.dk) i titel/begrundelse/resume.
3. For hvert match oversættes sagsnummeret ("Nr.") til en forståelig type
   (fx "S 365 (§ 20-spørgsmål)"), og der findes et direkte link til det
   nyeste tilknyttede dokument (en PDF, som en almindelig browser kan
   åbne).
4. Rapporten skrives til `reports/<dato>.md` og committes tilbage til
   repoet af GitHub Actions.
5. Hvis SMTP-oplysninger er sat op som secrets (se nedenfor), sendes
   rapporten også som e-mail.

Kører automatisk via `.github/workflows/daily-check.yml`, ca. kl. 06:00
dansk tid.

---

## 2) `pdf_scanner.py` – dyb PDF-scan

Efterligner det, man selv ville gøre manuelt i en browser:

- **A)** Åbner <https://www.ft.dk/da/dokumenter/dokumentlister/nyeste-dokumenter>
- **B)** Finder alle dokument-links i tabellens "Titel"-kolonne
- **C)** Springer dem over, der allerede er tjekket før (gemt i `pdf_seen.json`)
- **D)** Åbner hver ny dokumentside
- **E)** Finder PDF-linket/-linkene der (CSS-markøren `.no-border a`, præcis som du foreslog)
- **F)** Henter PDF'en og søger i den fulde tekst efter søgeordene

Bruger [Playwright](https://playwright.dev/python/) til at styre en rigtig
(headless) Chromium-browser, fordi det — modsat almindelige HTTP-kald — kan
komme forbi Cloudflares sikkerhedstjek, ganske som da vi testede det
sammen i din egen browser.

### ⚠️ Vigtigt om Cloudflare

Jeg har testet selve teknikken (CSS-markørerne, PDF-linkene, det hele) live
i **din egen browser**, hvor det virkede fint efter et par sekunders
Cloudflare-tjek. Men GitHub Actions kører på servere i et datacenter, og
Cloudflare er generelt mere mistænksom over for datacenter-IP-adresser og
headless browsere end over for en almindelig persons computer — selv når
scriptet efterligner en rigtig browser. Det betyder:

- Det **kan** vise sig at virke fint fra GitHub Actions — mange lignende
  opsætninger gør. Første rigtige kørsel afslører det.
- Hvis Cloudflare begynder at blokere det (rapporten vil sige tydeligt
  "Cloudflare blokerede ..." i stedet for at lade som om alt er tjekket),
  er den mest robuste løsning at køre `pdf_scanner.py` fra en almindelig
  computer/server med en normal IP-adresse i stedet for GitHub Actions —
  fx jeres egen kontor-pc eller en lille server, sat op med Windows
  Opgaveplanlægning eller cron. Selve scriptet er det samme uanset hvor
  det kører.
- `monitor.py` (API-tjekket) er upåvirket af alt dette og bliver ved med
  at virke som en pålidelig bund, uanset hvad der sker med PDF-scanneren.

### Sådan virker det

1. Scriptet husker alle dokumenter, det allerede har tjekket
   (`pdf_seen.json`). Ved allerførste kørsel er alt nyt, så den kørsel kan
   tage længere tid end de efterfølgende.
2. Det bladrer igennem dokumentlisten, side for side (25 dokumenter pr.
   side), og stopper automatisk, så snart en hel side kun indeholder
   allerede-sete dokumenter — så der bladres ikke unødigt langt tilbage.
3. For hvert nyt dokument åbnes siden, og alle PDF'er under `.no-border a`
   hentes og læses igennem.
4. Alle dokumenter markeres som "set", uanset om de gav match, så de ikke
   tjekkes igen i morgen.
5. Rapporten skrives til `reports/pdf-scan-<dato>.md`.

Kører via `.github/workflows/pdf-scan.yml` én gang dagligt, ca. kl. 07:00
dansk tid. Justér cron-udtrykket, hvis I senere vil have det til at køre
oftere.

---

## Kom i gang

1. Opret et nyt (gerne privat) GitHub-repo, og læg alle filerne fra dette
   projekt ind i det.
2. Under **Settings → Actions → General → Workflow permissions**, vælg
   "Read and write permissions" (så workflows må committe rapporter
   tilbage).
3. Kør begge workflows manuelt én gang til at starte med, under fanen
   **Actions → [vælg workflow] → Run workflow** — så I kan se om
   PDF-scanneren rent faktisk kommer igennem Cloudflare fra GitHub, inden
   I regner med den.
4. Rapporterne dukker op i `reports/`-mappen i repoet.

### Valgfrit: e-mail-levering (kun `monitor.py` lige nu)

`monitor.py` sender kun e-mail, hvis alle disse er sat som **repository
secrets** (Settings → Secrets and variables → Actions → New repository
secret):

| Secret       | Eksempel                          |
|--------------|------------------------------------|
| `SMTP_HOST`  | `smtp.office365.com`               |
| `SMTP_PORT`  | `587`                               |
| `SMTP_USER`  | `dit-login@tv2oj.dk`               |
| `SMTP_PASS`  | app-adgangskode / kodeord           |
| `MAIL_FROM`  | `dit-login@tv2oj.dk` (kan udelades) |
| `MAIL_TO`    | `thni@tv2oj.dk`                    |

Uden dem skriver scriptet blot rapporten til `reports/`-mappen. Sig til,
hvis I får SMTP-adgang og vil have `pdf_scanner.py` til også at sende
e-mail — det er en lille tilføjelse.

### Ret kørselstidspunktet

Cron-udtrykkene i `.github/workflows/*.yml` er i UTC. `daily-check.yml`
(`0 4 * * *`) rammer ca. kl. 06:00 dansk sommertid; `pdf-scan.yml`
(`0 5 * * *`) rammer ca. kl. 07:00. GitHub Actions cron justerer ikke
automatisk for sommer-/vintertid, så ret timetallene med -1 omkring
skiftet til vintertid (slut oktober), hvis I vil ramme samme klokkeslæt
året rundt.

## Kør lokalt (til test/fejlsøgning)

```bash
pip install -r requirements.txt
playwright install --with-deps chromium   # kun nødvendigt for pdf_scanner.py

python monitor.py
python pdf_scanner.py
```

## Filoversigt

- `monitor.py` – daglig tjek via oda.ft.dk-API'et.
- `pdf_scanner.py` – dyb PDF-scan via en rigtig browser.
- `requirements.txt` – Python-afhængigheder for begge scripts.
- `.github/workflows/daily-check.yml` – automatisk kørsel af `monitor.py`.
- `.github/workflows/pdf-scan.yml` – automatisk kørsel af `pdf_scanner.py`.
- `state.json` / `pdf_seen.json` – oprettes automatisk; husker hvad der er
  tjekket (commit dem ikke manuelt, lad workflowsene vedligeholde dem).
- `reports/` – én markdown-rapport pr. kørsel.
