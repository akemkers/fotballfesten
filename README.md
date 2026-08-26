# fotballfesten – resale-billettvarsler

Overvåker NFFs videresalgsside og sender et push-varsel via [ntfy](https://ntfy.sh)
når det dukker opp ledige resale-billetter til Ullevål.

Overvåket side: `https://resale.fotball.no/list/resaleProducts/?lang=no`

## Hvorfor Playwright?

Resale-siden er JavaScript-rendret: rå-HTML inneholder bare en «Laster opp»-
spinner og maler – selve billettantallet settes inn av JavaScript i nettleseren.
Derfor lastes siden i en ekte (headless) Chromium via Playwright, og antallet
leses fra det ferdig-rendrede innholdet.

## Oppsett

```bash
pip install -r requirements.txt
playwright install chromium
```

## Kjøring

```bash
python monitor.py                 # sjekker hvert 20. sekund
python monitor.py -i 30           # sjekker hvert 30. sekund
python monitor.py -d              # [DIAG]-logging for hver sjekk
python monitor.py --once          # kjør én sjekk og avslutt
python monitor.py --test-notify   # send testmelding til ntfy og avslutt
```

Abonner på varslene ved å legge til topicen `nff-resale-billetter` i ntfy-appen
(eller åpne https://ntfy.sh/nff-resale-billetter).

## Når overvåkingen ikke kan lese antallet

Scriptet skiller mellom «det er 0 billetter» og «jeg klarte ikke lese antallet».
Det er en viktig forskjell: tidligere ble alle leseferil tolket som 0, slik at en
ødelagt selektor så ut som helt normal drift i loggen – rolige `0 billett(er)` i
det uendelige – mens ekte billetter gikk upåaktet hen.

Antallet leses nå fra flere kilder i tur og orden, fra smalest til bredest:

1. `.resale-availability .resale-list-number`
2. `.resale-list-number` (i tilfelle wrapperen mangler)
3. `.resale-availability`
4. hele produktkortets tekst

Hver kilde prøves mot «N billett(er)», mot et blankt tall (`3`), og mot
«utsolgt»/«ingen billetter». Treffer ingen av dem, blir antallet `ULESELIG` –
ikke 0. Da logges råteksten og kortets HTML som `[DIAG]`, og etter
tre slike sjekker på rad sendes et eget ntfy-varsel («VARSLING NEDE»), som
gjentas omtrent hver halvtime til antallet er leselig igjen.

Det samme gjelder når lista er tom uten at siden selv sier at det ikke er noe
til salgs – da har vi mest sannsynlig ikke fått tak i lista.

Stillhet fra varslingen skal aldri kunne bety at overvåkingen er ødelagt.

## Deploy på Railway

Railway sin auto-bygger (Railpack, som er standard nå etter at Nixpacks ble
utfaset) får som regel ikke med seg Chromium og systembibliotekene den krever.
Derfor ligger det en `Dockerfile` i repoet som bruker Playwrights offisielle
image – da er Chromium og alle OS-avhengigheter ferdig installert.

1. Railway oppdager `Dockerfile` automatisk og bruker den i stedet for
   auto-byggeren (Railpack/Nixpacks).
2. Entrypointet er `python monitor.py` (satt som `CMD` i Dockerfile) – ingen
   ekstra start-kommando trengs.
3. Tjenesten er en bakgrunns-worker og lytter ikke på noen HTTP-port, så du
   trenger ikke sette opp en port eller healthcheck.

Hold Playwright-versjonen i `requirements.txt` og image-taggen i `Dockerfile`
(`v1.62.0-jammy`) omtrent i synk når du oppgraderer.

## Miljøvariabler

- `PLAYWRIGHT_CHROMIUM_PATH` – valgfri sti til Chromium-binæren dersom
  Playwright ikke finner nettleseren selv. Trengs ikke med Docker-imaget
  over, eller etter `playwright install chromium` lokalt.
