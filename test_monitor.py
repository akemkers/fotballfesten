"""Tester for monitor.py.

Kjøres uten Playwright og requests installert - begge stubbes ut, og
`scrape`/`notify` monkeypatches, slik at hele varslingslogikken kan drives
gjennom oppdiktede sider.

    python3 test_monitor.py

Hver test svarer til en konkret feil som har vært i koden. De er beholdt
som regresjonsvern: flere av dem gjelder varsler som gikk tapt helt
lydløst, og som ingen logglinje ville avslørt.
"""
import contextlib
import importlib.util
import io
import json
import os
import sys
import types

sys.modules.setdefault("requests", types.ModuleType("requests"))
_pw = types.ModuleType("playwright.sync_api")
_pw.sync_playwright = None
sys.modules.setdefault("playwright", types.ModuleType("playwright"))
sys.modules["playwright.sync_api"] = _pw

_spec = importlib.util.spec_from_file_location(
    "monitor", os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor.py"))
monitor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(monitor)

NORMAL = {"kilde": "api", "noTicketBanner": False, "productMarkup": False, "queue": False}
TOM = {"kilde": "api", "noTicketBanner": True, "productMarkup": False, "queue": False}

_results = []

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "testdata", "resale_empty.json")


def card(name="Kamp", venue="Ullevaal", **kw):
    """Et DOM-produktkort slik scrape() leverer det (fallback-veien)."""
    base = {"name": name, "venue": venue, "number": None, "looseNumber": None,
            "availability": None, "cardText": "", "cardHtml": "<div/>"}
    base.update(kw)
    return base


def it(name="Kamp", venue="Ullevaal", count=0, detail="test"):
    """Et normalisert produkt slik både API- og DOM-veien leverer det."""
    return monitor.item(name, venue, count, detail)


def api_product(**kw):
    base = {"productId": 1, "name": "Kamp", "venue": "Ullevaal",
            "ticketCount": None, "availableQuantity": 0}
    base.update(kw)
    return base


def api_payload(*products):
    return {"resaleItems": [], "topics": [],
            "topicWithProductsList": [{"key": None, "name": "", "topicId": None,
                                       "products": list(products)}],
            "seatRelease": False}


def drive(scenario, state=None, notify_ok=True):
    """Kjører en serie sjekker. Hvert steg er (produkter, kildetilstand)."""
    state = state or monitor.new_state()
    sent = []

    def fake_notify(title, message, priority="5", tags="", attempts=3, timeout=30):
        if not notify_ok:
            raise RuntimeError("ntfy nede")
        sent.append((title, message))

    monitor.notify = fake_notify
    monitor.notify_diagnostic = lambda t, m, tags: fake_notify(t, m)
    with contextlib.redirect_stdout(io.StringIO()):
        for i, (items, source_state) in enumerate(scenario):
            monitor.run_check(items, source_state, False, state, now_ts=i * 20)
    return state, sent


def tickets(sent):
    return [m for t, m in sent if "Ledige billetter" in t]


def down_alerts(sent):
    return [m for t, m in sent if "VARSLING NEDE" in t]


def check(label, ok, detail=""):
    _results.append((label, ok))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f"\n         {detail}" if detail and not ok else ""))


def test_api():
    print("\nAPI-tolkning (mot ekte svar fra resale-siden)")
    with open(FIXTURE, encoding="utf-8") as fh:
        items, state = monitor.parse_api_payload(json.load(fh))
    check("ekte payload gir 1 produkt", len(items) == 1, len(items))
    check("navn leses", items[0]["name"] == "Nations League - A-herrer", items[0]["name"])
    check("sted leses", items[0]["venue"] == "Ullevaal Stadion", items[0]["venue"])
    # ticketCount er null i ekte svar - availableQuantity er signalet.
    check("availableQuantity=0 gir 0, ikke ULESELIG", items[0]["count"] == 0, items[0])
    check("kilden navngis", items[0]["detail"] == "availableQuantity", items[0]["detail"])
    check("resaleItems telles", state["resaleItems"] == 0, state)

    items, _ = monitor.parse_api_payload(api_payload(api_product(availableQuantity=3)))
    check("availableQuantity=3 gir 3", items[0]["count"] == 3, items[0])

    items, _ = monitor.parse_api_payload(
        api_payload(api_product(availableQuantity=None, ticketCount=4)))
    check("faller tilbake paa ticketCount", items[0]["count"] == 4, items[0])

    # Begge null for ALLE produkter = feltene har byttet navn.
    try:
        monitor.parse_api_payload(api_payload(api_product(availableQuantity=None)))
        check("alle antall null gir ApiShapeError", False, "ingen feil kastet")
    except monitor.ApiShapeError:
        check("alle antall null gir ApiShapeError", True)

    # Ett av to ulesbart skal ikke velte hele svaret.
    items, _ = monitor.parse_api_payload(api_payload(
        api_product(name="A", availableQuantity=2),
        api_product(name="B", availableQuantity=None)))
    check("ett ulesbart produkt beholdes som ULESELIG",
          [i["count"] for i in items] == [2, None], [i["count"] for i in items])

    for bad, label in [({}, "manglende topicWithProductsList"),
                       ({"topicWithProductsList": "nei"}, "feil type"),
                       ([], "liste i stedet for objekt")]:
        try:
            monitor.parse_api_payload(bad)
            check(f"{label} gir ApiShapeError", False, "ingen feil kastet")
        except monitor.ApiShapeError:
            check(f"{label} gir ApiShapeError", True)

    # Tomt produktsett er legitimt (ingen arrangementer til salgs).
    items, state = monitor.parse_api_payload({"topicWithProductsList": [], "resaleItems": []})
    check("tom produktliste er lovlig", items == [] and state["noTicketBanner"], state)

    check("negativt antall regnes som ukjent",
          monitor.api_count({"availableQuantity": -1, "ticketCount": None})[0] is None)
    check("boolsk verdi regnes ikke som antall",
          monitor.api_count({"availableQuantity": True, "ticketCount": None})[0] is None)


def test_parsing():
    print("\nTolkning av billettantall")
    cases = [
        ("0 billetter", 0), ("3 billetter", 3), ("1 billett", 1),
        ("3 av 10 billetter", 3), ("0 av 5 billetter", 0),
        ("Kun 2 igjen av 10 billetter", 2),
        ("1 234 billetter", 1234), ("1.234 billetter", 1234),
        ("Utsolgt", 0), ("Ingen billetter", 0),
        # Tallet er ikke et antall ledige - skal gi ULESELIG, ikke 4.
        ("Maks 4 billetter per kjop", None),
        ("Maksimalt 6 billetter", None),
        ("Se tilgjengelighet", None), ("", None),
        # Tall fra forrige setning skal ikke bli "N av M".
        ("Sete 9 - resten av 40 billetter", None),
        ("Rad 5.\nDu kan kjope av 12 billetter", None),
    ]
    for text, expected in cases:
        got, source = monitor.resolve_count(card(availability=text, cardText=text))
        check(f"{text!r} -> {expected!r}", got == expected, f"fikk {got!r} via {source}")

    # Flertydighet skal gi opp, ikke gjette.
    got, _ = monitor.resolve_count(card(cardText="3 billetter i felt A, 8 billetter i felt B"))
    check("flere ulike tall -> ULESELIG", got is None, f"fikk {got!r}")

    # "Utsolgt" i en smal kilde skal ikke skygge for et ekte antall.
    got, _ = monitor.resolve_count(card(availability="Utsolgt", cardText="2 billetter ledig"))
    check("'Utsolgt' maskerer ikke ekte antall", got == 2, f"fikk {got!r}")

    # Wrapperen kan mangle - da ligger tallet i .resale-list-number alene.
    got, _ = monitor.resolve_count(card(looseNumber="3"))
    check("wrapper mangler -> leser blankt tall", got == 3, f"fikk {got!r}")


def test_varsling():
    print("\nBillettvarsling")
    _, s = drive([([it(count=0)], NORMAL)] * 10)
    check("stabil 0 gir ingen varsler", len(tickets(s)) == 0, s)

    _, s = drive([([it(count=0)], NORMAL),
                  ([it(count=3)], NORMAL)])
    check("0 -> 3 gir ett varsel", len(tickets(s)) == 1, s)

    _, s = drive([([it(count=2)], NORMAL)])
    check("billetter ved oppstart gir varsel", len(tickets(s)) == 1, s)

    _, s = drive([([it(count=2)], NORMAL),
                  ([it(count=5)], NORMAL)])
    check("2 -> 5 gir nytt varsel", len(tickets(s)) == 2, s)

    _, s = drive([([it(count=5)], NORMAL),
                  ([it(count=2)], NORMAL)])
    check("nedgang gir ikke nytt varsel", len(tickets(s)) == 1, s)


def test_per_arrangement():
    print("\nPer-arrangement-sporing")
    # En ren sum ville skjult at B stiger mens A synker.
    _, s = drive([([it("A", count=3), it("B", count=0)], NORMAL),
                  ([it("A", count=1), it("B", count=2)], NORMAL)])
    check("oekning maskeres ikke av annet arrangement som synker",
          len(tickets(s)) == 2 and "B" in tickets(s)[1], s)

    # Duplikate noekler skal summeres, ikke overskrive hverandre.
    _, s = drive([([it("A", count=0), it("A", count=0)], NORMAL),
                  ([it("A", count=0), it("A", count=3)], NORMAL),
                  ([it("A", count=2), it("A", count=3)], NORMAL)])
    check("duplikate noekler: begge oekninger varsles", len(tickets(s)) == 2, s)

    # Motsatt rekkefoelge skal ikke gi varsel paa hver eneste sjekk.
    _, s = drive([([it("A", count=3), it("A", count=0)], NORMAL)] * 6)
    check("duplikate noekler gir ikke spam", len(tickets(s)) == 1, s)


def test_uleselig():
    print("\nUleselige kort")
    # Baselinen er ukjent etter blindhet: et positivt antall skal varsles,
    # selv om det er LAVERE enn foer. Billettene kan ha vaert innom null.
    _, s = drive([([it(count=4)], NORMAL), ([it(count=None)], NORMAL),
                  ([it(count=None)], NORMAL), ([it(count=3)], NORMAL)])
    check("4 -> ULESELIG -> 3 varsler likevel", len(tickets(s)) == 2, s)

    # Men samme antall rett etter et blaff skal ikke spamme.
    _, s = drive([([it(count=3)], NORMAL), ([it(count=None)], NORMAL),
                  ([it(count=3)], NORMAL)])
    check("3 -> ULESELIG -> 3 gir kun ett varsel", len(tickets(s)) == 1, s)

    # Navnloese kort kan ikke skilles fra hverandre og maa regnes uleselige,
    # ellers kollapser alle til samme noekkel og oekninger blir usynlige.
    st, s = drive([([it("", "", 0), it("", "", 0)], NORMAL),
                   ([it("", "", 0), it("", "", 6)], NORMAL),
                   ([it("", "", 4), it("", "", 6)], NORMAL)])
    check("kort uten navn flagges blindt", st["blind_streak"] >= 3, st["blind_streak"])
    check("kort uten navn gir VARSLING NEDE", len(down_alerts(s)) >= 1, s)


def test_blind():
    print("\nBlind-deteksjon")
    st = monitor.new_state()
    sent = []
    monitor.notify_diagnostic = lambda t, m, tags: sent.append((t, m))
    with contextlib.redirect_stdout(io.StringIO()):
        for _ in range(5):
            monitor.register_check(st, True, "TimeoutError")
    check("feilende sjekker utloeser VARSLING NEDE", len(down_alerts(sent)) == 1, sent)

    # Annenhver blind bygger aldri en serie - det glidende vinduet skal ta den.
    st = monitor.new_state()
    sent = []
    monitor.notify_diagnostic = lambda t, m, tags: sent.append((t, m))
    with contextlib.redirect_stdout(io.StringIO()):
        for i in range(24):
            monitor.register_check(st, i % 2 == 0, "blaffer")
    check("intermitterende blindhet varsles", len(down_alerts(sent)) >= 1, sent)

    # Blaffing skal ikke gi vekselvis NEDE og friskmelding i det uendelige.
    st = monitor.new_state()
    sent = []
    monitor.notify_diagnostic = lambda t, m, tags: sent.append((t, m))
    with contextlib.redirect_stdout(io.StringIO()):
        for _ in range(18):
            for i in range(10):
                monitor.register_check(st, i < 3, "blaffer")
    check("blaffing gir ikke push-spam", len(sent) <= 4, f"{len(sent)} push: {[t for t, _ in sent]}")

    # Et blind-varsel som ikke ble levert skal proeves igjen straks.
    st = monitor.new_state()
    forsok = []

    def flaky(t, m, tags):
        forsok.append(t)
        if len(forsok) == 1:
            raise RuntimeError("nede")
    monitor.notify_diagnostic = flaky
    with contextlib.redirect_stdout(io.StringIO()):
        for _ in range(4):
            monitor.register_check(st, True, "x")
    check("feilet blind-varsel proeves igjen straks", len(forsok) >= 2, len(forsok))


def test_tom_liste():
    print("\nTom liste og sidetilstand")
    _, s = drive([([], TOM)] * 5)
    check("ekte tom liste gir ingen varsler", len(s) == 0, s)

    markup = dict(TOM, productMarkup=True)
    _, s = drive([([], markup)] * 3)
    check("tom liste med produktmarkup flagges", len(down_alerts(s)) >= 1, s)

    _, s = drive([([], dict(TOM, queue=True))] * 3)
    check("venterom flagges", len(down_alerts(s)) >= 1, s)


def test_levering():
    print("\nLevering av varsel")
    st, _ = drive([([it(count=5)], NORMAL)], notify_ok=False)
    check("feilet varsel avanserer ikke baselinen", st["baselines"] == {}, st["baselines"])

    # Neste sjekk skal se samme oekning og faa den ut.
    st = monitor.new_state()
    sent = []
    calls = {"n": 0}

    def flaky(title, message, priority="5", tags="", attempts=3, timeout=30):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("429")
        sent.append(title)
    monitor.notify = flaky
    monitor.notify_diagnostic = lambda t, m, tags: None
    with contextlib.redirect_stdout(io.StringIO()):
        for i in range(2):
            monitor.run_check([it(count=5)], NORMAL, False, st, now_ts=i * 20)
    check("varsel leveres ved neste sjekk etter feil",
          sent == ["NFF Resale - Ledige billetter!"], sent)


def main():
    for fn in (test_api, test_parsing, test_varsling, test_per_arrangement, test_uleselig,
               test_blind, test_tom_liste, test_levering):
        fn()
    failed = [label for label, ok in _results if not ok]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} bestått")
    if failed:
        print("\nFeilet:")
        for label in failed:
            print(f"  - {label}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
