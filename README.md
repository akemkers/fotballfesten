# fotballfesten – resale-billettvarsler

Sender et push-varsel via [ntfy](https://ntfy.sh) når det dukker opp ledige
resale-billetter hos NFF.

Katalogen hentes som JSON én gang i sekundet fra
`https://resale.fotball.no/list/resale/resaleProductCatalog.json`.
Antallet ledige ligger i `availableQuantity` per arrangement.

## To ting å vite før du endrer noe

**`availableQuantity` kan være `null`, og null er ikke null billetter.** Det
betyr at vi ikke fikk lest antallet. Tolkes de to likt, ser en ødelagt
overvåker ut som helt normal drift, og varselet uteblir uten at noe ser galt ut
i loggen. Det var den opprinnelige feilen i dette prosjektet.

**Derfor sier scriptet fra når det ikke får lest katalogen.** Stillhet fra ntfy
skal bety «ingen billetter», ikke «overvåkingen er død».

## Oppførsel

Varsel sendes når et arrangement har flere billetter enn sist, eller dukker opp
med billetter (også ved oppstart). Antallet spores per arrangement, ikke som én
sum – ellers kunne en økning på ett arrangement blitt skjult av en nedgang på et
annet.

Feiler push-varselet, oppdateres ikke tilstanden. Neste sjekk ser samme økning
og prøver på nytt ett sekund senere.

Feiler selve hentingen – HTTP-feil, uventet struktur, eller et arrangement uten
lesbart antall – varsles det etter `BLIND_AFTER` sekunder sammenhengende feil,
og gjentas høyst hver `BLIND_REPEAT`. Når det virker igjen, kommer en
friskmelding (bare hvis vi faktisk rakk å si fra).

Loggen skriver bare når statusen endrer seg, pluss et livstegn hvert
`LOG_EVERY`. Én linje i sekundet ville gjort den ubrukelig.

## Justerbart

| Konstant | Standard | Betydning |
|---|---|---|
| `POLL_INTERVAL` | 1 s | mellom hver sjekk |
| `REQUEST_TIMEOUT` | 10 s | på HTTP-kall |
| `BLIND_AFTER` | 60 s | sammenhengende feil før vi varsler |
| `BLIND_REPEAT` | 1800 s | mellom gjentatte «nede»-varsler |
| `LOG_EVERY` | 300 s | mellom ellers uendrede statuslinjer |

Ett sekunds intervall er ~86 000 forespørsler i døgnet mot et udokumentert
internt endepunkt. Blir vi rate-limitet, kommer det fram som vedvarende feil og
dermed et «VARSLING NEDE»-varsel – da er det bare å øke `POLL_INTERVAL`.

## Kjøring

```bash
pip install -r requirements.txt

python monitor.py                 # hvert sekund
python monitor.py -i 5            # hvert 5. sekund
python monitor.py --once          # én sjekk, exit 1 hvis den feilet
python monitor.py --test-notify   # send testmelding til ntfy
```

Abonner på topicen `nff-resale-billetter` i ntfy-appen, eller åpne
https://ntfy.sh/nff-resale-billetter.

## Tester

```bash
python3 test_monitor.py
```

Kjører uten `requests` installert. `testdata/resale_empty.json` er et ekte svar
fra endepunktet.

## Deploy på Railway

`Dockerfile` plukkes opp automatisk. Entrypointet er `python monitor.py`.
Tjenesten er en bakgrunns-worker uten HTTP-port, så det trengs verken port
eller healthcheck.

Det som **ikke** dekkes: dør prosessen helt, kan ingen varsling fyre fra
innsiden. Overvåk at tjenesten faktisk kjører i Railway.
