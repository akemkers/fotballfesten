# fotballfesten – varsel om resale-billetter

Sender push via [ntfy](https://ntfy.sh) når NFF legger ut resale-billetter.
Skriptet henter
`https://resale.fotball.no/list/resale/resaleProductCatalog.json` hvert sekund
og leser `availableQuantity` for hvert arrangement.

## Før du endrer noe

**`null` er ikke 0.** `availableQuantity: null` betyr at antallet ikke kunne
leses. Behandles det som 0, ser en ødelagt overvåker ut som normal drift. Det
var den opprinnelige feilen her.

**Stillhet skal bety «ingen billetter».** Derfor varsler skriptet også når det
ikke får lest katalogen.

## Oppførsel

- **Billetter:** varsel når et arrangement har flere billetter enn sist, også
  ved oppstart. Antallet spores per arrangement, så en nedgang ett sted ikke
  skjuler en økning et annet sted.
- **Nede:** varsel etter `BLIND_AFTER` sekunder med feil (HTTP-feil, uventet
  format eller uleselig antall), deretter høyst hver `BLIND_REPEAT`. Et
  uleselig arrangement stopper ikke varsler for de andre.
- **Oppe igjen:** friskmelding, men bare hvis det ble sendt et «nede»-varsel.
- **Mislykket sending** prøves på nytt med økende pause: 1, 2, 4, 8 og
  deretter hvert `NTFY_RETRY` sekund. Billettvarsler og helsevarsler har hver
  sin pause, og alt prøves straks igjen når én sending lykkes. Forsvinner
  billettene før ntfy har tatt imot varselet, står det «Varsel tapt» i loggen.
- **Logg:** skrives når statusen endres, og ellers hvert `LOG_EVERY`.

Trykk på et varsel åpner resale-siden.

## Innstillinger

| Konstant | Standard | Betydning |
|---|---|---|
| `POLL_INTERVAL` | 1 s | tid mellom sjekker |
| `REQUEST_TIMEOUT` | 10 s | tidsavbrudd for HTTP |
| `BLIND_AFTER` | 60 s | feil før «nede»-varsel |
| `BLIND_REPEAT` | 1800 s | tid mellom gjentatte «nede»-varsler |
| `LOG_EVERY` | 300 s | livstegn i loggen |

Varslene går til `https://ntfy.sh/nff-resale-billetter`, eller til
`NTFY_URL` hvis den er satt. ntfy-topics er offentlige, så velg gjerne et navn
som er vanskelig å gjette.

Ett sekunds intervall blir ~86 000 kall i døgnet. Blir vi rate-limitet, kommer
det et «nede»-varsel. Øk da `POLL_INTERVAL`.

## Kjøring

```bash
pip install -r requirements.txt

python monitor.py                 # hvert sekund
python monitor.py -i 5            # hvert 5. sekund
python monitor.py --once          # én sjekk, exit 1 ved feil
python monitor.py --test-notify   # send testvarsel
```

Abonner på `nff-resale-billetter` i ntfy-appen, eller åpne
https://ntfy.sh/nff-resale-billetter.

## Tester

```bash
python3 -m unittest -v
```

Krever ikke `requests`. Kjøres også i GitHub Actions.
`testdata/resale_empty.json` er et ekte svar fra endepunktet.

## Deploy på Railway

Railway bygger fra `Dockerfile`. Tjenesten er en worker uten HTTP-port, så den
trenger verken port eller healthcheck.

Hvis prosessen dør, kan den ikke varsle om det selv. Sjekk i Railway at
tjenesten kjører.
