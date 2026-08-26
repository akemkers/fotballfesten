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

NORMAL = {"noTicketBanner": False, "listLen": 900, "productMarkup": True, "queue": False}
TOM = {"noTicketBanner": True, "listLen": 120, "productMarkup": False, "queue": False}

_results = []


def card(name="Kamp", venue="Ullevaal", **kw):
    base = {"name": name, "venue": venue, "number": None, "looseNumber": None,
            "availability": None, "cardText": "", "cardHtml": "<div/>"}
    base.update(kw)
    return base


def drive(scenario, state=None, notify_ok=True):
    """Kjører en serie sjekker. Hvert steg er (produkter, sidetilstand)."""
    state = state or monitor.new_state()
    sent = []

    def fake_notify(title, message, priority="5", tags="", attempts=3, timeout=30):
        if not notify_ok:
            raise RuntimeError("ntfy nede")
        sent.append((title, message))

    monitor.notify = fake_notify
    monitor.notify_diagnostic = lambda t, m, tags: fake_notify(t, m)
    with contextlib.redirect_stdout(io.StringIO()):
        for i, (products, page_state) in enumerate(scenario):
            monitor.scrape = lambda page, p=products, s=page_state: (p, s)
            monitor.run_check(None, False, state, now_ts=i * 20)
    return state, sent


def tickets(sent):
    return [m for t, m in sent if "Ledige billetter" in t]


def down_alerts(sent):
    return [m for t, m in sent if "VARSLING NEDE" in t]


def check(label, ok, detail=""):
    _results.append((label, ok))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f"\n         {detail}" if detail and not ok else ""))


# --------------------------------------------------------------------------
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
    _, s = drive([([card(number="0 billetter")], NORMAL)] * 10)
    check("stabil 0 gir ingen varsler", len(tickets(s)) == 0, s)

    _, s = drive([([card(number="0 billetter")], NORMAL),
                  ([card(number="3 billetter")], NORMAL)])
    check("0 -> 3 gir ett varsel", len(tickets(s)) == 1, s)

    _, s = drive([([card(number="2 billetter")], NORMAL)])
    check("billetter ved oppstart gir varsel", len(tickets(s)) == 1, s)

    _, s = drive([([card(number="2 billetter")], NORMAL),
                  ([card(number="5 billetter")], NORMAL)])
    check("2 -> 5 gir nytt varsel", len(tickets(s)) == 2, s)

    _, s = drive([([card(number="5 billetter")], NORMAL),
                  ([card(number="2 billetter")], NORMAL)])
    check("nedgang gir ikke nytt varsel", len(tickets(s)) == 1, s)


def test_per_arrangement():
    print("\nPer-arrangement-sporing")
    # En ren sum ville skjult at B stiger mens A synker.
    _, s = drive([([card("A", number="3 billetter"), card("B", number="0 billetter")], NORMAL),
                  ([card("A", number="1 billetter"), card("B", number="2 billetter")], NORMAL)])
    check("oekning maskeres ikke av annet arrangement som synker",
          len(tickets(s)) == 2 and "B" in tickets(s)[1], s)

    # Duplikate noekler skal summeres, ikke overskrive hverandre.
    _, s = drive([([card("A", number="0 billetter"), card("A", number="0 billetter")], NORMAL),
                  ([card("A", number="0 billetter"), card("A", number="3 billetter")], NORMAL),
                  ([card("A", number="2 billetter"), card("A", number="3 billetter")], NORMAL)])
    check("duplikate noekler: begge oekninger varsles", len(tickets(s)) == 2, s)

    # Motsatt rekkefoelge skal ikke gi varsel paa hver eneste sjekk.
    _, s = drive([([card("A", number="3 billetter"), card("A", number="0 billetter")], NORMAL)] * 6)
    check("duplikate noekler gir ikke spam", len(tickets(s)) == 1, s)


def test_uleselig():
    print("\nUleselige kort")
    # Baselinen er ukjent etter blindhet: et positivt antall skal varsles,
    # selv om det er LAVERE enn foer. Billettene kan ha vaert innom null.
    _, s = drive([([card(number="4 billetter")], NORMAL), ([card()], NORMAL),
                  ([card()], NORMAL), ([card(number="3 billetter")], NORMAL)])
    check("4 -> ULESELIG -> 3 varsler likevel", len(tickets(s)) == 2, s)

    # Men samme antall rett etter et blaff skal ikke spamme.
    _, s = drive([([card(number="3 billetter")], NORMAL), ([card()], NORMAL),
                  ([card(number="3 billetter")], NORMAL)])
    check("3 -> ULESELIG -> 3 gir kun ett varsel", len(tickets(s)) == 1, s)

    # Navnloese kort kan ikke skilles fra hverandre og maa regnes uleselige,
    # ellers kollapser alle til samme noekkel og oekninger blir usynlige.
    st, s = drive([([card("", "", number="0 billetter"), card("", "", number="0 billetter")], NORMAL),
                   ([card("", "", number="0 billetter"), card("", "", number="6 billetter")], NORMAL),
                   ([card("", "", number="4 billetter"), card("", "", number="6 billetter")], NORMAL)])
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
    st, _ = drive([([card(number="5 billetter")], NORMAL)], notify_ok=False)
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
            monitor.scrape = lambda page: ([card(number="5 billetter")], NORMAL)
            monitor.run_check(None, False, st, now_ts=i * 20)
    check("varsel leveres ved neste sjekk etter feil",
          sent == ["NFF Resale - Ledige billetter!"], sent)


def main():
    for fn in (test_parsing, test_varsling, test_per_arrangement, test_uleselig,
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
