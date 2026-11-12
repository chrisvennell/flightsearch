import json
import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents import discover  # noqa: E402
from agents.flight_rules import (  # noqa: E402
    airport_list,
    decide_reasons,
    expand_dates,
    expand_queries,
    is_valid,
    normalize,
    price_cap,
    search_plan,
    stitch,
    sweep_calls,
)
from agents.flights import FlightsAgent  # noqa: E402
from core.base import AgentError, Context  # noqa: E402
from core.http import HttpError  # noqa: E402

ME = {"DXB", "DOH"}
NOW = datetime(2026, 11, 13, 3, 0, tzinfo=timezone.utc)
RULES = {"base_cap": 1300, "no_middle_east_cap": 1600, "per_overnight_drop": 200}


def leg(frm, to, dep, arr, airline="Test Air", minutes=600):
    return {
        "departure_airport": {"id": frm, "time": dep},
        "arrival_airport": {"id": to, "time": arr},
        "airline": airline,
        "duration": minutes,
    }


def raw(price, legs, layovers=None, total=None):
    out = {"price": price, "flights": legs, "layovers": layovers or []}
    if total is not None:
        out["total_duration"] = total
    return out


def it(price=1000, airports=("DUD", "SIN", "MAD"), overnights=(), nights_extra=0, to=None):
    return {
        "price": price,
        "airports": list(airports),
        "overnights": list(overnights),
        "nights_extra": nights_extra,
        "to": to or airports[-1],
    }


class Parsing(unittest.TestCase):
    def test_normalize_path_overnights_and_airports(self):
        r = raw(
            1100,
            [
                leg("DUD", "SIN", "2027-07-24 10:00", "2027-07-24 20:00", "Air NZ"),
                leg("SIN", "MAD", "2027-07-25 09:00", "2027-07-25 17:00", "Singapore Airlines"),
            ],
            layovers=[{"id": "SIN", "duration": 780, "overnight": True}],
            total=1500,
        )
        n = normalize(r)
        self.assertEqual(n["path"], ["DUD", "SIN", "MAD"])
        self.assertEqual(n["overnights"], ["SIN"])
        self.assertEqual(n["stops"], 1)
        self.assertEqual(n["duration"], 1500)
        self.assertEqual(n["airlines"], ["Air NZ", "Singapore Airlines"])
        self.assertEqual(n["arrives"], "2027-07-25 17:00")

    def test_normalize_sums_duration_when_total_missing(self):
        r = raw(900, [leg("A", "B", "x", "y", minutes=100), leg("B", "C", "x", "y", minutes=200)],
                layovers=[{"id": "B", "duration": 60}])
        self.assertEqual(normalize(r)["duration"], 360)

    def test_normalize_rejects_unpriced_or_empty(self):
        self.assertIsNone(normalize({"flights": [leg("A", "B", "x", "y")]}))
        self.assertIsNone(normalize({"price": 100, "flights": []}))
        self.assertIsNone(normalize({"price": True, "flights": [leg("A", "B", "x", "y")]}))

    def test_expand_dates(self):
        self.assertEqual(expand_dates("2027-07-24"), ["2027-07-24"])
        self.assertEqual(len(expand_dates({"from": "2027-07-24", "to": "2027-07-28"})), 5)
        with self.assertRaises(ValueError):
            expand_dates({"from": "2027-07-28", "to": "2027-07-24"})

    def test_airport_sets(self):
        sets = {"a": ["mad", "BCN"], "b": ["@a", "AGP"]}
        self.assertEqual(airport_list("@b", sets), ["MAD", "BCN", "AGP"])
        with self.assertRaises(ValueError):
            airport_list("@nope", sets)

    def test_expand_queries_skips_return_before_depart(self):
        block = {"from": "DUD", "to": "WLG", "depart": ["2026-12-17", "2026-12-22"], "return": "2026-12-21"}
        qs = expand_queries(block, {}, {})
        self.assertEqual([q["depart"] for q in qs], ["2026-12-17"])

    def test_multi_airport_query_is_comma_joined(self):
        q = expand_queries({"from": ["SYD", "WSI"], "to": "@s", "depart": "2027-07-24"}, {}, {"s": ["MAD", "AGP"]})
        self.assertEqual((q[0]["from"], q[0]["to"]), ("SYD,WSI", "MAD,AGP"))


class Caps(unittest.TestCase):
    def test_no_overnight(self):
        self.assertEqual(price_cap(it(airports=("DUD", "DXB", "MAD")), RULES, ME), 1300)
        self.assertEqual(price_cap(it(), RULES, ME), 1600)

    def test_overnights_reduce_by_200_each(self):
        me = ("DUD", "DXB", "MAD")
        self.assertEqual(price_cap(it(airports=me, overnights=["DXB"]), RULES, ME), 1100)
        self.assertEqual(price_cap(it(airports=me, overnights=["DXB", "DXB"]), RULES, ME), 900)
        self.assertEqual(price_cap(it(overnights=["SIN"]), RULES, ME), 1400)

    def test_gap_nights_count_as_overnights(self):
        self.assertEqual(price_cap(it(airports=("DUD", "DXB", "MAD"), nights_extra=2), RULES, ME), 900)

    def test_return_rules_and_dunedin_alternative(self):
        rules = {"base_cap": 1100, "per_overnight_drop": 200, "cap_by_destination": {"DUD": 1300}}
        self.assertEqual(price_cap(it(to="BNE"), rules, ME), 1100)
        self.assertEqual(price_cap(it(to="BNE", overnights=["SIN"]), rules, ME), 900)
        self.assertEqual(price_cap(it(to="BNE", overnights=["SIN", "KUL"]), rules, ME), 700)
        self.assertEqual(price_cap(it(to="DUD"), rules, ME), 1300)

    def test_no_cap(self):
        self.assertIsNone(price_cap(it(), {}, ME))


class Validity(unittest.TestCase):
    SEARCH = {"arrive_between": ["2027-07-26", "2027-07-30"]}

    def test_arrival_window(self):
        base = {"stops": 1, "overnights": [], "arrives": "2027-07-26 08:00"}
        self.assertTrue(is_valid(base, self.SEARCH, None))
        self.assertTrue(is_valid({**base, "arrives": "2027-07-30 23:59"}, self.SEARCH, None))
        self.assertFalse(is_valid({**base, "arrives": "2027-07-25 23:59"}, self.SEARCH, None))
        self.assertFalse(is_valid({**base, "arrives": "2027-07-31 00:01"}, self.SEARCH, None))
        self.assertFalse(is_valid({**base, "arrives": None}, self.SEARCH, None))

    def test_overnight_only_at_allowed_airports(self):
        base = {"stops": 1, "arrives": "2027-07-26 08:00"}
        ok = {"SIN", "CHC"}
        self.assertTrue(is_valid({**base, "overnights": ["SIN"]}, self.SEARCH, ok))
        self.assertFalse(is_valid({**base, "overnights": ["SIN", "LHR"]}, self.SEARCH, ok))
        self.assertTrue(is_valid({**base, "overnights": ["LHR"]}, self.SEARCH, None))

    def test_max_stops(self):
        base = {"stops": 2, "overnights": [], "arrives": "2027-07-26 08:00"}
        self.assertFalse(is_valid(base, {**self.SEARCH, "max_stops": 1}, None))


class Stitching(unittest.TestCase):
    def a(self, arrives="2027-07-25 14:00"):
        return {"price": 300, "duration": 240, "departs": "2027-07-25 08:00", "arrives": arrives,
                "from": "DUD", "to": "OOL", "path": ["DUD", "OOL"], "stops": 0, "airlines": ["Jetstar"],
                "overnights": [], "nights_extra": 0, "airports": ["DUD", "OOL"], "stitched": False}

    def b(self, departs):
        return {"price": 700, "duration": 1400, "departs": departs, "arrives": "2027-07-27 06:00",
                "from": "BNE", "to": "MAD", "path": ["BNE", "SIN", "MAD"], "stops": 1, "airlines": ["Qantas"],
                "overnights": [], "nights_extra": 0, "airports": ["BNE", "SIN", "MAD"], "stitched": False}

    def test_gap_bounds(self):
        first = [self.a()]
        second = [self.b("2027-07-25 17:00"), self.b("2027-07-25 20:00"), self.b("2027-07-28 14:00"),
                  self.b("2027-07-28 14:01")]
        out = stitch(first, second, 5, 72)
        # 3h gap too short, 6h ok, exactly 72h ok, 72h01 too long
        self.assertEqual([o["departs"] for o in out], ["2027-07-25 08:00"] * 2)
        self.assertEqual(len(out), 2)

    def test_combined_fields(self):
        out = stitch([self.a()], [self.b("2027-07-26 09:00")], 5, 72)[0]
        self.assertEqual(out["price"], 1000)
        self.assertEqual(out["path"], ["DUD", "OOL", "|", "BNE", "SIN", "MAD"])
        self.assertEqual(out["nights_extra"], 1)
        self.assertEqual(out["duration"], 240 + 19 * 60 + 1400)
        self.assertEqual(out["to"], "MAD")
        self.assertTrue(out["stitched"])
        self.assertIn("BNE", out["airports"])

    def test_gap_nights_can_be_ignored(self):
        out = stitch([self.a()], [self.b("2027-07-26 09:00")], 5, 72, count_gap_nights=False)[0]
        self.assertEqual(out["nights_extra"], 0)


def make_ctx(http, env=None):
    return Context(env={"SERPAPI_API_KEY": "SECRETKEY"} if env is None else env, now=NOW, http_get=http)


def outbound_response():
    good_long = raw(1200, [leg("DUD", "AKL", "2027-07-24 06:00", "2027-07-24 07:30"),
                           leg("AKL", "SIN", "2027-07-24 10:00", "2027-07-24 17:00"),
                           leg("SIN", "MAD", "2027-07-25 01:00", "2027-07-26 08:00")], total=2400)
    good_short = raw(1250, [leg("DUD", "SYD", "2027-07-24 06:00", "2027-07-24 07:30"),
                            leg("SYD", "MAD", "2027-07-24 12:00", "2027-07-26 07:00")], total=1800)
    middle_east = raw(1500, [leg("DUD", "DXB", "2027-07-24 06:00", "2027-07-25 07:30"),
                             leg("DXB", "MAD", "2027-07-25 10:00", "2027-07-26 07:00")], total=1500)
    wrong_day = raw(900, [leg("DUD", "SIN", "2027-07-28 06:00", "2027-07-28 17:00"),
                          leg("SIN", "MAD", "2027-07-29 01:00", "2027-07-31 08:00")], total=1500)
    bad_overnight = raw(
        900,
        [leg("DUD", "LHR", "2027-07-24 06:00", "2027-07-25 07:30"), leg("LHR", "MAD", "2027-07-26 10:00", "2027-07-26 13:00")],
        layovers=[{"id": "LHR", "duration": 1500, "overnight": True}], total=2000,
    )
    return {
        "search_metadata": {"google_flights_url": "https://www.google.com/travel/flights?x=1"},
        "best_flights": [good_long, good_short],
        "other_flights": [middle_east, wrong_day, bad_overnight, {"flights": []}],
    }


SETTINGS = {
    "currency": "NZD",
    "drop_alert_percent": 10,
    "airport_sets": {"spain": ["MAD", "AGP"], "ok": ["SIN", "CHC"], "middle_east": ["DXB"]},
    "searches": [
        {
            "name": "Out",
            "from": "DUD",
            "to": "@spain",
            "depart": {"from": "2027-07-24", "to": "2027-07-25"},
            "arrive_between": ["2027-07-26", "2027-07-30"],
            "overnight_ok": "@ok",
            "rules": {**RULES, "prefer": "shortest"},
        }
    ],
}


def agent(settings=None, state=None, budget=None, http=None, env=None):
    return FlightsAgent(settings or SETTINGS, state or {}, make_ctx(http or (lambda u, t: outbound_response()), env), budget)


class AgentRun(unittest.TestCase):
    def test_picks_shortest_qualifying_and_alerts(self):
        result = agent().run()
        self.assertEqual(result.calls_used, 2)
        card = result.items[0]
        self.assertEqual(card["value"], "NZD 1,250")
        self.assertEqual(card["badge"], "within limits")
        self.assertIn("DUD-SYD-MAD", card["subtitle"])
        self.assertEqual(len(result.alerts), 1)
        self.assertEqual(card["link"], "https://www.google.com/travel/flights?x=1")

    def test_second_run_same_day_makes_no_calls(self):
        first = agent().run()
        seen = []
        second = agent(state=first.state, http=lambda u, t: seen.append(u) or outbound_response()).run()
        self.assertEqual(seen, [])
        self.assertEqual(second.calls_used, 0)
        self.assertEqual(second.alerts, [])
        self.assertEqual(second.items[0]["value"], "NZD 1,250")

    def test_sweep_repeats_after_interval_and_stays_quiet(self):
        first = agent().run()
        later = FlightsAgent(SETTINGS, first.state,
                             Context(env={"SERPAPI_API_KEY": "k"}, now=datetime(2026, 11, 20, 3, tzinfo=timezone.utc),
                                     http_get=lambda u, t: outbound_response()), None).run()
        self.assertEqual(later.calls_used, 2)
        self.assertEqual(later.alerts, [])

    def test_budget_guard_skips_whole_sweep(self):
        seen = []
        result = agent(budget=1, http=lambda u, t: seen.append(u) or outbound_response()).run()
        self.assertEqual(seen, [])
        self.assertEqual(result.items[0]["badge"], "budget reached")

    def test_over_limit_shows_cheapest_valid_without_alert(self):
        resp = {"best_flights": [raw(1550, [leg("DUD", "DXB", "2027-07-24 06:00", "2027-07-25 07:30"),
                                            leg("DXB", "MAD", "2027-07-25 10:00", "2027-07-26 07:00")], total=1500)]}
        result = agent(http=lambda u, t: resp).run()
        self.assertEqual(result.alerts, [])
        self.assertEqual(result.items[0]["badge"], "NZD 250 over")

    def test_no_valid_flights(self):
        result = agent(http=lambda u, t: {"error": "Google Flights hasn't returned any results."}).run()
        self.assertEqual(result.calls_used, 0)
        self.assertEqual(result.items[0]["badge"], "no matching flights")

    def test_http_error_is_shown_not_raised(self):
        def boom(url, t):
            raise HttpError("HTTP 401: Invalid API key")

        result = agent(http=boom).run()
        self.assertEqual(result.items[0]["badge"], "error")
        self.assertNotIn("SECRETKEY", json.dumps(result.items))

    def test_failed_sweep_is_retried_next_run(self):
        def boom(url, t):
            raise HttpError("HTTP 500: down")

        failed = agent(http=boom).run()
        self.assertNotIn("last_sweep", failed.state["Out"])

    def test_missing_key_raises(self):
        with self.assertRaises(AgentError):
            agent(env={}).run()

    def test_request_parameters(self):
        seen = []
        agent(http=lambda u, t: seen.append(u) or outbound_response()).run()
        q = parse_qs(urlparse(seen[0]).query)
        self.assertEqual(q["departure_id"], ["DUD"])
        self.assertEqual(q["arrival_id"], ["MAD,AGP"])
        self.assertEqual(q["type"], ["2"])
        self.assertEqual(q["sort_by"], ["2"])
        self.assertNotIn("return_date", q)

    def test_round_trip_time_windows_are_sent(self):
        settings = {
            "currency": "NZD",
            "gl": "nz",
            "searches": [{
                "name": "WLG", "from": "DUD", "to": "WLG",
                "queries": [{"depart": "2026-12-17", "return": "2026-12-21", "outbound_times": "16,23", "return_times": "12,23"}],
                "rules": {"base_cap": 150},
            }],
        }
        seen = []
        resp = {"best_flights": [raw(140, [leg("DUD", "WLG", "2026-12-17 18:00", "2026-12-17 19:30")], total=90)]}
        result = agent(settings, http=lambda u, t: seen.append(u) or resp).run()
        q = parse_qs(urlparse(seen[0]).query)
        self.assertEqual((q["type"], q["return_date"], q["outbound_times"], q["return_times"], q["gl"]),
                         (["1"], ["2026-12-21"], ["16,23"], ["12,23"], ["nz"]))
        self.assertEqual(len(result.alerts), 1)

    def test_disabled_search_is_ignored(self):
        settings = {**SETTINGS, "searches": [{**SETTINGS["searches"][0], "enabled": False}]}
        seen = []
        agent(settings, http=lambda u, t: seen.append(u) or outbound_response()).run()
        self.assertEqual(seen, [])

    def test_stitched_search_end_to_end(self):
        first = {"best_flights": [raw(300, [leg("DUD", "OOL", "2027-07-25 08:00", "2027-07-25 11:00")], total=240)]}
        second = {"best_flights": [raw(700, [leg("BNE", "SIN", "2027-07-26 09:00", "2027-07-26 16:00"),
                                             leg("SIN", "MAD", "2027-07-26 23:00", "2027-07-27 08:00")], total=1100)]}
        settings = {
            "currency": "NZD",
            "airport_sets": {"spain": ["MAD"]},
            "searches": [{
                "name": "Stitch",
                "stitch": {"first": {"from": "DUD", "to": "OOL", "depart": "2027-07-25"},
                           "second": {"from": "BNE", "to": "@spain", "depart": "2027-07-26"},
                           "min_gap_hours": 5, "max_gap_hours": 72},
                "arrive_between": ["2027-07-26", "2027-07-30"],
                "rules": RULES,
            }],
        }
        result = agent(settings, http=lambda u, t: first if "departure_id=DUD" in u else second).run()
        card = result.items[0]
        self.assertEqual(card["value"], "NZD 1,000")
        self.assertIn("two tickets", card["subtitle"])
        # one night between tickets lowers the cap from 1600 to 1400, still within it
        self.assertEqual(card["badge"], "within limits")
        self.assertEqual(result.calls_used, 2)

    def test_no_secret_in_output(self):
        result = agent().run()
        self.assertNotIn("SECRETKEY", json.dumps([result.items, result.state]))

    def test_registry(self):
        self.assertIn("flights", discover())


class AlertRules(unittest.TestCase):
    def test_capped_first_qualify(self):
        self.assertTrue(decide_reasons({}, 1200, 1200, 10, True))

    def test_capped_quiet_until_further_drop(self):
        st = {"last_alert_price": 1200}
        self.assertEqual(decide_reasons(st, 1200, 1200, 10, True), [])
        self.assertEqual(decide_reasons(st, 1150, 1150, 10, True), [])
        self.assertTrue(decide_reasons(st, 1080, 1080, 10, True))

    def test_capped_nothing_qualifies(self):
        self.assertEqual(decide_reasons({}, None, 1500, 10, True), [])

    def test_uncapped_drop(self):
        self.assertTrue(decide_reasons({"last": 300}, None, 270, 10, False))
        self.assertEqual(decide_reasons({"last": 300}, None, 280, 10, False), [])


class RealConfig(unittest.TestCase):
    def setUp(self):
        self.settings = json.loads((ROOT / "config.json").read_text())["agents"]["flights"]

    def test_every_enabled_search_parses_and_budget_holds(self):
        sets = self.settings["airport_sets"]
        today = date(2026, 11, 13)
        total = 0.0
        for s in self.settings["searches"]:
            plan = search_plan(s, sets)
            self.assertTrue(plan)
            if s.get("enabled", True) is False:
                continue
            every = s.get("check_every_days", self.settings["check_every_days"])
            total += sweep_calls(s, sets, today) * 30 / every
        self.assertLessEqual(total, self.settings["monthly_call_budget"])

    def test_outbound_query_dates(self):
        sets = self.settings["airport_sets"]
        direct = search_plan(self.settings["searches"][0], sets)["main"]
        self.assertEqual([q["depart"] for q in direct][0], "2027-07-24")
        self.assertEqual(len(direct), 5)
        self.assertTrue(all(q["from"] == "DUD" and "AGP" in q["to"] for q in direct))

    def test_return_search_covers_agp_from_aug_4_and_spain_to_aug_12(self):
        sets = self.settings["airport_sets"]
        back = next(s for s in self.settings["searches"] if s["name"].startswith("Back"))
        qs = search_plan(back, sets)["main"]
        self.assertEqual(qs[0]["from"], "AGP")
        self.assertEqual(qs[0]["depart"], "2027-08-04")
        self.assertEqual(sorted({q["depart"] for q in qs})[-1], "2027-08-13")
        self.assertEqual(qs[1]["depart"], "2027-08-05")
        self.assertEqual(qs[0]["to"], "BNE,OOL,MCY,DUD")

    def test_wellington_is_enabled_with_february_dates(self):
        wlg = next(s for s in self.settings["searches"] if "Wellington" in s["name"])
        self.assertTrue(wlg.get("enabled", True))
        qs = search_plan(wlg, self.settings["airport_sets"])["main"]
        self.assertEqual(sorted({(q["depart"], q["return"]) for q in qs}),
                         [("2027-02-17", "2027-02-21"), ("2027-02-17", "2027-02-22"),
                          ("2027-02-18", "2027-02-21"), ("2027-02-18", "2027-02-22")])

    def test_gold_coast_gap_nights_do_not_reduce_the_limit(self):
        gc = next(s for s in self.settings["searches"] if "Gold Coast" in s["name"])
        self.assertIs(gc["stitch"]["count_gap_nights"], False)

    def test_brisbane_sydney_hong_kong_allowed_for_overnights(self):
        self.assertTrue({"BNE", "SYD", "HKG"} <= set(self.settings["airport_sets"]["overnight_ok"]))

    def test_middle_east_set_in_config_matches_default_codes(self):
        self.assertIn("DXB", self.settings["airport_sets"]["middle_east"])


if __name__ == "__main__":
    unittest.main()
