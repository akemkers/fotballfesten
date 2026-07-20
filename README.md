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

## Miljøvariabler

- `PLAYWRIGHT_CHROMIUM_PATH` – valgfri sti til Chromium-binæren dersom
  Playwright ikke finner nettleseren selv. Trengs normalt ikke etter
  `playwright install chromium`.
