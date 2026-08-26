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

## Varslingslogikk

Antallet spores **per arrangement**, ikke som én sum. En ren sum ville skjult at
ett arrangement stiger mens et annet synker – da kunne ekte nye billetter
forsvinne i støyen fra et helt annet arrangement.

Sporingsnøkkelen er `navn|sted`. Flere kort med samme nøkkel summeres. Mangler
navnet, kan kortene ikke skilles fra hverandre, og kortet regnes som uleselig –
ellers ville alle kollapset til én nøkkel og skjult økninger helt lydløst.

Varsel sendes når et arrangement får flere billetter enn sist, eller når et
arrangement dukker opp med billetter (også ved oppstart/restart).

Feiler selve push-varselet (ntfy nede, 429), avanseres **ikke** baselinen. Neste
sjekk ser samme økning og prøver på nytt, slik at et varsel ikke går tapt på en
forbigående nettverksfeil. Billettvarselet sendes alltid før diagnosevarslene,
og diagnosevarsler bruker ett forsøk med kort timeout – de skal aldri kunne
forsinke det ene varselet som haster.

### Baselinen etter en blind periode

Var et kort uleselig, settes baselinen til **ukjent** – ikke til forrige verdi og
ikke til 0. Begge de to enkle valgene er feil:

- Beholder man forrige verdi, kan et lavere, men ekte, antall bli usynlig:
  billettene kan ha vært innom null mens vi var blinde.
- Setter man 0, gir hvert eneste blaff et nytt varsel.

Med ukjent baseline varsler vi på ethvert positivt antall – heller ett
unødvendig varsel enn ett tapt – men demper gjentakelser av nøyaktig samme
antall i fem minutter.

## Når overvåkingen ikke kan lese antallet

Scriptet skiller mellom «det er 0 billetter» og «jeg klarte ikke lese
antallet». Det er en viktig forskjell: tidligere ble alle lesefeil tolket som 0,
slik at en ødelagt selektor så ut som helt normal drift i loggen – rolige
`0 billett(er)` i det uendelige – mens ekte billetter gikk upåaktet hen.

Antallet leses fra flere kilder i tur og orden, fra smalest til bredest:

1. `.resale-availability .resale-list-number`
2. `.resale-list-number` (i tilfelle wrapperen mangler)
3. `.resale-availability`
4. hele produktkortets tekst

Hver kilde prøves mot «N av M billetter» (der N er antallet), mot
«N billett(er)», og mot et blankt tall (`3`). Fraser der tallet ikke er et
antall ledige filtreres bort – «maks 4 billetter per kjøp», og «av 12 billetter»
uten teller foran, der 12 er totalen. Tusenskille normaliseres slik at
«1 234 billetter» ikke leses som 234. Inneholder en kilde flere ulike tall foran
«billett», gir vi opp i stedet for å gjette: et feil tall ville låst varslingen
permanent, mens «uleselig» utløser varsel.

«Utsolgt»/«ingen billetter» sjekkes først til slutt, når ingen kilde ga et tall.
Da kan det ikke skygge for et ekte antall i en bredere kilde.

Treffer ingenting, blir antallet `ULESELIG` – ikke 0. Råteksten og kortets HTML
logges som `[DIAG]`.

### Når du får beskjed om at overvåkingen er nede

En sjekk regnes som blind når antallet er uleselig, når et kort mangler navn,
når lista er tom uten at siden selv sier det (eller sier det mens lista likevel
inneholder produkt-markup), når siden viser venterom/kø, **eller når selve
sjekken kaster** – f.eks. fordi `#list_all_tickets` ikke lenger finnes. Det
siste er den mest sannsynlige måten en strukturendring viser seg på, og det er
derfor unntak telles med.

Varsel («VARSLING NEDE») sendes ved tre blinde sjekker på rad, eller ved 10
blinde blant de siste 20 – det siste fanger en side som feiler annenhver gang og
derfor aldri bygger opp en sammenhengende serie. Så lenge tilstanden varer,
gjentas varselet tidligst hver halvtime, og telleren nullstilles bare når
varselet faktisk ble levert.

Friskmelding krever ti gode sjekker på rad. Uten den terskelen ville en side som
blaffer sendt «nede» og «virker igjen» om hverandre i det uendelige – og
push-spam ender med at topicen dempes, og da er også det ekte billettvarselet
borte.

En sjekk som henger avbrytes etter to minutter og telles som blind.

Det som fortsatt **ikke** dekkes: dør prosessen helt, kan ingen varsling fyre
fra innsiden. Overvåk derfor at tjenesten faktisk kjører i Railway – stillhet
fra ntfy er ikke i seg selv bevis på at alt er i orden.

## Tester

```bash
python3 test_monitor.py
```

Kjører uten Playwright og requests installert. Hver test svarer til en konkret
feil som har vært i koden, og er beholdt som regresjonsvern – flere av dem
gjelder varsler som gikk tapt helt lydløst, uten at noen logglinje avslørte det.

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
