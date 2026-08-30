"""Tester for monitor.py.

Kjøres uten `requests` installert - modulen stubbes ut, og `send`
monkeypatches, slik at beslutningslogikken kan drives uten nettverk.

    python3 test_monitor.py

Testene beskriver oppførsel, ikke mekanikk: hva som skal utløse et varsel,
og hva som skal si fra når overvåkingen ikke virker.
"""
import contextlib
import importlib.util
import io
import json
import os
import sys
import types

sys.modules.setdefault("requests", types.ModuleType("requests"))
_here = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("monitor", os.path.join(_here, "monitor.py"))
monitor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(monitor)

FIXTURE = os.path.join(_here, "testdata", "resale_empty.json")
_results = []


def check(label, ok, detail=""):
    _results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"\n         {detail}" if detail and not ok else ""))


def catalogue(*products):
    return {"resaleItems": [], "topicWithProductsList": [{"products": list(products)}]}


def product(name="Kamp", venue="Ullevaal", quantity=0):
    return {"name": name, "venue": venue, "availableQuantity": quantity, "ticketCount": None}


def run(steps, state=None, send_ok=True):
    """Kjører en serie (antall, feil)-steg. Returnerer (tilstand, sendte varsler)."""
    state = state or monitor.new_state()
    sent = []

    def fake_send(title, message, priority="5", tags=""):
        if not send_ok:
            return False
        sent.append((title, message))
        return True

    monitor.send = fake_send
    with contextlib.redirect_stdout(io.StringIO()):
        for i, (counts, problem) in enumerate(steps):
            monitor.step(state, counts, problem, i)
    return state, sent


def titles(sent, word):
    return [m for t, m in sent if word in t]


def test_lesing():
    print("\nLesing av katalogen")
    with open(FIXTURE, encoding="utf-8") as fh:
        counts, unreadable = monitor.read_counts(json.load(fh))
    check("ekte svar gir ett arrangement", len(counts) == 1, counts)
    check("navn og sted blir noekkel",
          "Nations League - A-herrer @ Ullevaal Stadion" in counts, counts)
    check("availableQuantity=0 leses som 0", list(counts.values()) == [0], counts)
    check("ingen uleselige", unreadable == 0, unreadable)

    counts, _ = monitor.read_counts(catalogue(product(quantity=3)))
    check("availableQuantity=3 leses som 3", list(counts.values()) == [3], counts)

    # null er IKKE null billetter - det er et ulest antall.
    counts, unreadable = monitor.read_counts(catalogue(product(quantity=None)))
    check("null antall telles som uleselig", counts == {} and unreadable == 1, (counts, unreadable))
    for verdi in (True, -1, "3"):
        _c, u = monitor.read_counts(catalogue(product(quantity=verdi)))
        check(f"{verdi!r} telles som uleselig", u == 1, u)

    counts, u = monitor.read_counts(catalogue(product(name="")))
    check("arrangement uten navn telles som uleselig", u == 1, u)

    # Duplikater summeres, ellers kunne en oekning paa den foerste bli borte.
    counts, _ = monitor.read_counts(catalogue(product(quantity=2), product(quantity=3)))
    check("duplikate arrangementer summeres", list(counts.values()) == [5], counts)

    for bad, label in [({}, "manglende felt"), ({"topicWithProductsList": 5}, "feil type")]:
        try:
            monitor.read_counts(bad)
            check(f"{label} kaster", False, "ingen feil")
        except Exception:
            check(f"{label} kaster", True)


def test_varsling():
    print("\nBillettvarsling")
    _, sent = run([({"A": 0}, None)] * 10)
    check("stabil 0 gir ingen varsler", sent == [], sent)

    _, sent = run([({"A": 0}, None), ({"A": 3}, None)])
    check("0 -> 3 varsler", len(sent) == 1 and "3" in sent[0][1], sent)

    _, sent = run([({"A": 2}, None)])
    check("billetter ved oppstart varsler", len(sent) == 1, sent)

    _, sent = run([({"A": 2}, None), ({"A": 5}, None)])
    check("2 -> 5 varsler igjen", len(sent) == 2, sent)

    _, sent = run([({"A": 5}, None), ({"A": 2}, None), ({"A": 2}, None)])
    check("nedgang varsler ikke", len(sent) == 1, sent)

    # En sum ville skjult at B stiger mens A synker.
    _, sent = run([({"A": 3, "B": 0}, None), ({"A": 1, "B": 2}, None)])
    check("oekning skjules ikke av annet arrangement", len(sent) == 2 and "B" in sent[1][1], sent)


def test_levering():
    print("\nLevering")
    st, _ = run([({"A": 5}, None)], send_ok=False)
    check("feilet varsel avanserer ikke tilstanden", st["counts"] == {}, st["counts"])

    # Neste sjekk skal se samme oekning og faa den ut.
    state = monitor.new_state()
    sent = []
    kall = {"n": 0}

    def flaky(title, message, priority="5", tags=""):
        kall["n"] += 1
        if kall["n"] == 1:
            return False
        sent.append(title)
        return True

    monitor.send = flaky
    with contextlib.redirect_stdout(io.StringIO()):
        for i in range(2):
            monitor.step(state, {"A": 5}, None, i)
    check("varselet kommer ut ved neste sjekk", sent == ["NFF Resale - Ledige billetter!"], sent)


def test_nede():
    print("\nNår overvåkingen ikke virker")
    _, sent = run([(None, "HTTP 503")] * 30)
    check("kortvarig feil varsler ikke", sent == [], sent)

    steps = [(None, "HTTP 503")] * (monitor.BLIND_AFTER + 5)
    _, sent = run(steps)
    check("vedvarende feil varsler", len(titles(sent, "NEDE")) == 1, sent)

    # Ett varsel, ikke ett i sekundet.
    steps = [(None, "HTTP 503")] * (monitor.BLIND_AFTER + 600)
    _, sent = run(steps)
    check("gjentas ikke oftere enn BLIND_REPEAT", len(titles(sent, "NEDE")) == 1, len(titles(sent, "NEDE")))

    steps = [(None, "HTTP 503")] * (monitor.BLIND_AFTER + monitor.BLIND_REPEAT + 5)
    _, sent = run(steps)
    check("gjentas etter BLIND_REPEAT", len(titles(sent, "NEDE")) == 2, len(titles(sent, "NEDE")))

    # Friskmelding bare hvis vi faktisk sa fra.
    steps = [(None, "HTTP 503")] * (monitor.BLIND_AFTER + 5) + [({"A": 0}, None)]
    _, sent = run(steps)
    check("friskmelding etter nede-varsel", len(titles(sent, "virker igjen")) == 1, sent)

    steps = [(None, "HTTP 503")] * 10 + [({"A": 0}, None)]
    _, sent = run(steps)
    check("ingen friskmelding uten nede-varsel", sent == [], sent)

    # Uleselig antall behandles som feil, ikke som null billetter.
    steps = [(None, "1 arrangement(er) uten lesbart antall")] * (monitor.BLIND_AFTER + 5)
    _, sent = run(steps)
    check("uleselig antall varsler som nede", len(titles(sent, "NEDE")) == 1, sent)


def test_logg():
    print("\nLogging")
    state = monitor.new_state()
    monitor.send = lambda *a, **k: True
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        for i in range(120):
            monitor.step(state, {"A": 0}, None, i)
    linjer = [l for l in out.getvalue().strip().split("\n") if l]
    check("uendret status spammer ikke loggen", len(linjer) == 1, f"{len(linjer)} linjer")

    state = monitor.new_state()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        for i in range(monitor.LOG_EVERY + 10):
            monitor.step(state, {"A": 0}, None, i)
    linjer = [l for l in out.getvalue().strip().split("\n") if l]
    check("livstegn etter LOG_EVERY", len(linjer) == 2, f"{len(linjer)} linjer")


def main():
    for fn in (test_lesing, test_varsling, test_levering, test_nede, test_logg):
        fn()
    feil = len(_results) - sum(_results)
    print(f"\n{sum(_results)}/{len(_results)} bestått")
    return 1 if feil else 0


if __name__ == "__main__":
    sys.exit(main())
