"""Tester for monitor.py. Krever verken nettverk eller `requests`.

    python3 -m unittest -v
"""
import contextlib
import io
import json
import os
import sys
import types
import unittest

sys.modules.setdefault("requests", types.ModuleType("requests"))
import monitor  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "testdata", "resale_empty.json")
FAIL = (None, "HTTP 503")


def catalogue(*products):
    return {"resaleItems": [], "topicWithProductsList": [{"products": list(products)}]}


def product(name="Kamp", venue="Ullevaal", quantity=0):
    return {"name": name, "venue": venue, "availableQuantity": quantity, "ticketCount": None}


class FakeNotify:
    """Falsk `send`. `results` gir returverdien per kall, deretter `default`."""

    def __init__(self, *results, default=True):
        self.results = list(results)
        self.default = default
        self.sent = []
        self.calls = 0

    def __call__(self, title, message, priority="5", tags=""):
        self.calls += 1
        ok = self.results.pop(0) if self.results else self.default
        if ok:
            self.sent.append((title, message))
        return ok

    def titled(self, word):
        return [m for t, m in self.sent if word in t]


def run_logged(steps, notify=None, state=None):
    """Kjører en serie (antall, feil)-steg, ett sekund fra hverandre.
    Returnerer (tilstand, notify, logg)."""
    state = state or monitor.State()
    notify = notify or FakeNotify()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        for now, (counts, problem) in enumerate(steps):
            monitor.step(state, counts, problem, now, notify)
    return state, notify, out.getvalue()


def run(steps, notify=None, state=None):
    """Som `run_logged`, uten loggen."""
    return run_logged(steps, notify, state)[:2]


class Lesing(unittest.TestCase):
    def test_ekte_svar(self):
        with open(FIXTURE, encoding="utf-8") as fh:
            counts, unreadable = monitor.read_counts(json.load(fh))
        self.assertEqual(counts, {"Nations League - A-herrer @ Ullevaal Stadion": 0})
        self.assertEqual(unreadable, 0)

    def test_positivt_antall(self):
        counts, _ = monitor.read_counts(catalogue(product(quantity=3)))
        self.assertEqual(list(counts.values()), [3])

    def test_null_er_ikke_null_billetter(self):
        counts, unreadable = monitor.read_counts(catalogue(product(quantity=None)))
        self.assertEqual((counts, unreadable), ({}, 1))

    def test_ugyldige_antall(self):
        for value in (True, -1, "3", 1.5):
            with self.subTest(value=value):
                _, unreadable = monitor.read_counts(catalogue(product(quantity=value)))
                self.assertEqual(unreadable, 1)

    def test_uten_navn(self):
        _, unreadable = monitor.read_counts(catalogue(product(name="")))
        self.assertEqual(unreadable, 1)

    def test_duplikater_summeres(self):
        counts, _ = monitor.read_counts(catalogue(product(quantity=2), product(quantity=3)))
        self.assertEqual(list(counts.values()), [5])

    def test_uventet_form_kaster(self):
        for bad in ({}, {"topicWithProductsList": 5}):
            with self.subTest(bad=bad), self.assertRaises(Exception):
                monitor.read_counts(bad)


class FakeSession:
    def __init__(self, data=None, error=None):
        self.data, self.error = data, error

    def get(self, *args, **kwargs):
        if self.error:
            raise self.error
        return types.SimpleNamespace(raise_for_status=lambda: None, json=lambda: self.data)


class Henting(unittest.TestCase):
    def test_alt_lest(self):
        counts, problem = monitor.check(FakeSession(catalogue(product(quantity=2))))
        self.assertEqual((list(counts.values()), problem), ([2], None))

    def test_uleselig_arrangement_beholder_de_lesbare(self):
        data = catalogue(product(name="A", quantity=4), product(name="B", quantity=None))
        counts, problem = monitor.check(FakeSession(data))
        self.assertEqual(counts, {"A @ Ullevaal": 4})
        self.assertIn("uten lesbart antall", problem)

    def test_nettverksfeil(self):
        counts, problem = monitor.check(FakeSession(error=TimeoutError("treigt")))
        self.assertIsNone(counts)
        self.assertIn("TimeoutError", problem)


class Billettvarsling(unittest.TestCase):
    def test_stabil_null(self):
        _, notify = run([({"A": 0}, None)] * 10)
        self.assertEqual(notify.sent, [])

    def test_okning_varsler(self):
        _, notify = run([({"A": 0}, None), ({"A": 3}, None)])
        self.assertEqual(len(notify.sent), 1)
        self.assertIn("3", notify.sent[0][1])

    def test_billetter_ved_oppstart(self):
        _, notify = run([({"A": 2}, None)])
        self.assertEqual(len(notify.sent), 1)

    def test_ny_okning_varsler_igjen(self):
        _, notify = run([({"A": 2}, None), ({"A": 5}, None)])
        self.assertEqual(len(notify.sent), 2)

    def test_nedgang_varsler_ikke(self):
        _, notify = run([({"A": 5}, None), ({"A": 2}, None), ({"A": 2}, None)])
        self.assertEqual(len(notify.sent), 1)

    def test_okning_skjules_ikke_av_annet_arrangement(self):
        # En sum ville skjult at B stiger når A synker.
        _, notify = run([({"A": 3, "B": 0}, None), ({"A": 1, "B": 2}, None)])
        self.assertEqual(len(notify.sent), 2)
        self.assertIn("B", notify.sent[1][1])

    def test_uleselig_arrangement_blinder_ikke_de_andre(self):
        problem = "1 arrangement(er) uten lesbart antall"
        _, notify = run([({"A": 0}, problem), ({"A": 4}, problem)])
        self.assertEqual(len(notify.titled("Ledige")), 1)


class Levering(unittest.TestCase):
    def test_feilet_varsel_avanserer_ikke_tilstanden(self):
        state, _ = run([({"A": 5}, None)], FakeNotify(False))
        self.assertEqual(state.counts, {})

    def test_kort_hikk_forsinker_varselet_ett_sekund(self):
        _, notify = run([({"A": 5}, None)] * 2, FakeNotify(False))
        self.assertEqual([t for t, _ in notify.sent], ["NFF Resale - Ledige billetter!"])
        self.assertEqual(notify.calls, 2)

    def test_ntfy_nede_hamres_ikke(self):
        # Forsøk ved 0, 1, 3, 7, 15, og deretter hvert NTFY_RETRY: 30, 45 … 105.
        _, notify = run([({"A": 2}, None)] * 120, FakeNotify(default=False))
        self.assertEqual(notify.calls, 11)

    def test_nede_varsel_hamres_ikke(self):
        _, notify = run([FAIL] * (monitor.BLIND_AFTER + 120), FakeNotify(default=False))
        self.assertEqual(notify.calls, 11)

    def test_feilet_helsevarsel_holder_ikke_igjen_billettvarsel(self):
        # «Nede»-varselet feiler i siste runde, billettene kommer i neste.
        steps = ([({"A": 0}, "1 uleselig")] * (monitor.BLIND_AFTER + 1)
                 + [({"A": 3}, "1 uleselig")])
        _, notify = run(steps, FakeNotify(False))
        self.assertEqual(len(notify.titled("Ledige")), 1)

    def test_vellykket_varsel_opphever_pausen_for_andre(self):
        # Billettvarselet står i pause, men friskmeldingen går gjennom:
        # ntfy virker, så billettvarselet sendes neste runde.
        state = monitor.State(alerted_down=0)
        state.ticket_ntfy.retry_at = 1000
        _, notify = run([({"A": 2}, None)] * 2, state=state)
        self.assertEqual([t for t, _ in notify.sent],
                         ["NFF Resale - virker igjen", "NFF Resale - Ledige billetter!"])

    def test_tapt_varsel_logges(self):
        # Billettene forsvinner før ntfy har tatt imot varselet.
        steps = [({"A": 0}, None)] + [({"A": 2}, None)] * 3 + [({"A": 0}, None)]
        state, _, out = run_logged(steps, FakeNotify(default=False))
        self.assertIn("Varsel tapt", out)
        self.assertIn("A: 0 → 2", out)
        self.assertEqual(state.unsent, {})

    def test_tapt_varsel_logges_selv_om_annet_varsel_sendes(self):
        # A forsvinner samtidig som B dukker opp og blir varslet.
        steps = [({"A": 0, "B": 0}, None), ({"A": 2, "B": 0}, None),
                 ({"A": 0, "B": 1}, None)]
        _, _, out = run_logged(steps, FakeNotify(False))
        self.assertIn("Varsel tapt, billettene forsvant før ntfy svarte: A: 0 → 2", out)
        self.assertIn("Varsel sendt: B: 0 → 1", out)

    def test_levert_varsel_logges_ikke_som_tapt(self):
        steps = [({"A": 0}, None), ({"A": 2}, None), ({"A": 0}, None)]
        _, _, out = run_logged(steps)
        self.assertNotIn("Varsel tapt", out)

    def test_feilet_friskmelding_prøves_igjen(self):
        steps = [FAIL] * (monitor.BLIND_AFTER + 1) + [({"A": 0}, None)] * 2
        # «Nede» lykkes, første friskmelding feiler.
        _, notify = run(steps, FakeNotify(True, False))
        self.assertEqual(len(notify.titled("virker igjen")), 1)


class Nede(unittest.TestCase):
    def test_kortvarig_feil_varsler_ikke(self):
        _, notify = run([FAIL] * 30)
        self.assertEqual(notify.sent, [])

    def test_vedvarende_feil_varsler(self):
        _, notify = run([FAIL] * (monitor.BLIND_AFTER + 5))
        self.assertEqual(len(notify.titled("NEDE")), 1)

    def test_ett_varsel_ikke_ett_i_sekundet(self):
        _, notify = run([FAIL] * (monitor.BLIND_AFTER + 600))
        self.assertEqual(len(notify.titled("NEDE")), 1)

    def test_gjentas_etter_blind_repeat(self):
        _, notify = run([FAIL] * (monitor.BLIND_AFTER + monitor.BLIND_REPEAT + 5))
        self.assertEqual(len(notify.titled("NEDE")), 2)

    def test_friskmelding_etter_nede_varsel(self):
        _, notify = run([FAIL] * (monitor.BLIND_AFTER + 5) + [({"A": 0}, None)])
        self.assertEqual(len(notify.titled("virker igjen")), 1)

    def test_ingen_friskmelding_uten_nede_varsel(self):
        _, notify = run([FAIL] * 10 + [({"A": 0}, None)])
        self.assertEqual(notify.sent, [])

    def test_uleselig_antall_varsler_som_nede(self):
        problem = (None, "1 arrangement(er) uten lesbart antall")
        _, notify = run([problem] * (monitor.BLIND_AFTER + 5))
        self.assertEqual(len(notify.titled("NEDE")), 1)


class Henteklokke(unittest.TestCase):
    def test_pausen_dobles_til_taket(self):
        state = monitor.State()
        delays = []
        for now in range(12):
            monitor.step(state, None, "HTTP 403", now, FakeNotify())
            delays.append(state.fetch.delay)
        self.assertEqual(delays, [1, 2, 4, 8, 16, 32, 64, 128, 256, 300, 300, 300])

    def test_svar_nullstiller_pausen(self):
        state, _ = run([FAIL] * 5 + [({"A": 0}, None)])
        self.assertEqual(state.fetch.retry_at, 0)

    def test_uleselig_arrangement_gir_ingen_pause(self):
        # Katalogen svarer, så vi blir ikke blokkert, og de lesbare
        # arrangementene må fortsatt sjekkes hvert sekund.
        state, _ = run([({"A": 0}, "1 arrangement(er) uten lesbart antall")] * 5)
        self.assertEqual(state.fetch.retry_at, 0)

    def test_pausen_står_i_loggen(self):
        out = run_logged([FAIL] * 3)[2]
        self.assertIn("neste forsøk om 4 s", out)


class Logging(unittest.TestCase):
    def lines(self, n):
        return run_logged([({"A": 0}, None)] * n)[2].strip().splitlines()

    def test_uendret_status_spammer_ikke(self):
        self.assertEqual(len(self.lines(120)), 1)

    def test_livstegn_etter_log_every(self):
        self.assertEqual(len(self.lines(monitor.LOG_EVERY + 10)), 2)


if __name__ == "__main__":
    unittest.main()
