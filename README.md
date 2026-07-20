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
python monitor.py                 # sjekker hvert 60. sekund
python monitor.py -i 30           # sjekker hvert 30. sekund
python monitor.py -d              # debug-logging
```

Abonner på varslene ved å legge til topicen `nff-resale-billetter` i ntfy-appen
(eller åpne https://ntfy.sh/nff-resale-billetter).

## Deploy på Railway

Nixpacks (standard-byggeren som kjører `pip install` automatisk) får som regel
ikke med seg Chromium og systembibliotekene den krever. Derfor ligger det en
`Dockerfile` i repoet som bruker Playwrights offisielle image – da er Chromium
og alle OS-avhengigheter ferdig installert.

1. Railway oppdager `Dockerfile` automatisk og bruker den i stedet for Nixpacks.
2. Entrypointet er `python monitor.py` (satt som `CMD` i Dockerfile) – ingen
   ekstra start-kommando trengs.
3. Tjenesten er en bakgrunns-worker og lytter ikke på noen HTTP-port, så du
   trenger ikke sette opp en port eller healthcheck.

Hold Playwright-versjonen i `requirements.txt` og image-taggen i `Dockerfile`
(`v1.61.0-jammy`) omtrent i synk når du oppgraderer.

## Miljøvariabler

- `PLAYWRIGHT_CHROMIUM_PATH` – valgfri sti til Chromium-binæren dersom
  Playwright ikke finner nettleseren selv. Trengs ikke med Docker-imaget
  over, eller etter `playwright install chromium` lokalt.
