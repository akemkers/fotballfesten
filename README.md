# fotballfesten – resale-billettvarsler

Overvåker NFFs videresalgskatalog og sender et push-varsel via [ntfy](https://ntfy.sh)
når det dukker opp ledige resale-billetter.

Endepunkt: `https://resale.fotball.no/list/resale/resaleProductCatalog.json`

## Hvordan antallet leses

Katalogen hentes som JSON. Antallet leses fra `availableQuantity` på hvert
produkt under `topicWithProductsList[].products[]`.

`ticketCount` er `null` i praksis og brukes bare som reserve. Er **begge** null,
er antallet **ukjent** – ikke null. Den forskjellen er avgjørende: tolkes en
lesefeil som «0 billetter», ser en ødelagt overvåker ut som helt normal drift,
og varselet uteblir uten at noe ser galt ut i loggen.

Negative og boolske verdier avvises på samme måte, som ukjent.

## Varslingslogikk

Antallet spores **per arrangement**, ikke som én sum. En ren sum ville skjult at
ett arrangement stiger mens et annet synker – da kunne ekte nye billetter
forsvinne i støyen fra et helt annet arrangement.

Sporingsnøkkelen er `navn|sted`. Flere produkter med samme nøkkel summeres.
Mangler navnet, kan produktene ikke skilles fra hverandre, og produktet regnes
som uleselig – ellers ville alle kollapset til én nøkkel og skjult økninger helt
lydløst.

Varsel sendes når et arrangement får flere billetter enn sist, eller når et
arrangement dukker opp med billetter (også ved oppstart/restart).

Feiler selve push-varselet (ntfy nede, 429), avanseres **ikke** baselinen. Neste
sjekk ser samme økning og prøver på nytt, slik at et varsel ikke går tapt på en
forbigående nettverksfeil. Billettvarselet sendes alltid før diagnosevarslene,
og diagnosevarsler bruker ett forsøk med kort timeout – de skal aldri kunne
forsinke det ene varselet som haster.

### Baselinen etter en blind periode

Var et produkt uleselig, settes baselinen til **ukjent** – ikke til forrige verdi
og ikke til 0. Begge de enkle valgene er feil:

- Beholder man forrige verdi, kan et lavere, men ekte, antall bli usynlig:
  billettene kan ha vært innom null mens vi var blinde.
- Setter man 0, gir hvert eneste blaff et nytt varsel.

Med ukjent baseline varsler vi på ethvert positivt antall – heller ett
unødvendig varsel enn ett tapt – men demper gjentakelser av nøyaktig samme
antall i fem minutter.

## Når du får beskjed om at overvåkingen er nede

En sjekk regnes som blind når et produkt mangler lesbart antall eller navn,
**eller når selve sjekken kaster** – HTTP-feil, tidsavbrudd, eller et svar som
ikke har forventet struktur (manglende `topicWithProductsList`, feil type, eller
ingen produkter med lesbart antall). Uten at unntak telles med, ville den
vanligste bruddformen gått helt stille forbi.

Varsel («VARSLING NEDE») sendes ved tre blinde sjekker på rad, eller ved 10
blinde blant de siste 20 – det siste fanger et API som feiler annenhver gang og
derfor aldri bygger opp en sammenhengende serie. Så lenge tilstanden varer,
gjentas varselet tidligst hver halvtime, og telleren nullstilles bare når
varselet faktisk ble levert. Friskmelding krever ti gode sjekker på rad, slik at
et blaffende API ikke gir vekselvis «nede» og «virker igjen».

En tom katalog er derimot legitim: API-et sier uttrykkelig fra ved å svare med
feltet til stede og lista tom.

En sjekk som henger avbrytes etter ett minutt og telles som blind.

Det som **ikke** dekkes: dør prosessen helt, kan ingen varsling fyre fra
innsiden. Overvåk derfor at tjenesten faktisk kjører i Railway – stillhet fra
ntfy er ikke i seg selv bevis på at alt er i orden.

## Oppsett

```bash
pip install -r requirements.txt
```

## Kjøring

```bash
python monitor.py                 # sjekker hvert 10. sekund
python monitor.py -i 5            # sjekker hvert 5. sekund
python monitor.py -d              # [DIAG]-logging for hver sjekk
python monitor.py --once          # kjør én sjekk og avslutt
python monitor.py --test-notify   # send testmelding til ntfy og avslutt
```

Abonner på varslene ved å legge til topicen `nff-resale-billetter` i ntfy-appen
(eller åpne https://ntfy.sh/nff-resale-billetter).

## Tester

```bash
python3 test_monitor.py
```

Kjører uten `requests` installert. Hver test svarer til en konkret feil som har
vært i koden, og er beholdt som regresjonsvern – flere av dem gjelder varsler
som gikk tapt helt lydløst, uten at noen logglinje avslørte det.
`testdata/resale_empty.json` er et ekte svar fra endepunktet.

## Deploy på Railway

Repoet har en `Dockerfile` som Railway plukker opp automatisk og bruker i stedet
for auto-byggeren. Entrypointet er `python monitor.py`, satt som `CMD`.
Tjenesten er en bakgrunns-worker og lytter ikke på noen HTTP-port, så det trengs
verken port eller healthcheck.
