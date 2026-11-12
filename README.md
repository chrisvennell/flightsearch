# Agent Hub

A small app made of independent agents. A scheduled job runs each enabled agent, saves the results, sends phone alerts, and publishes a dashboard you can install on your phone. The first agent searches flight prices.

```
GitHub Actions (06:00 and 18:00 UTC)
  -> run.py runs each enabled agent in agents/
       -> flights agent calls SerpApi's Google Flights engine
  -> alerts go to your phone through ntfy
  -> results are saved to docs/data/hub.json and state/state.json
  -> docs/ is published as the dashboard (installable on a phone)
```

Everything uses the Python standard library. There is nothing to install.

## Setup

1. Create a **public** GitHub repository and upload these files. A free GitHub account can publish Pages only from public repositories, so the dashboard and `config.json` are visible to anyone with the link. Keep anything private out of `config.json`.
2. Get a SerpApi key at serpapi.com. The free plan lists 250 searches per month.
3. In the repository, go to Settings, Secrets and variables, Actions, and add two secrets:
   - `SERPAPI_API_KEY`: your SerpApi key
   - `NTFY_TOPIC`: a long random string only you know. Anyone who knows it can read your alerts.
4. In Settings, Pages, set Source to **GitHub Actions**.
5. Install the ntfy app on your phone and subscribe to the same topic.
6. Open the Actions tab, choose the `agents` workflow and press Run workflow once.
7. Open the Pages address shown by the finished run on your phone and use Add to Home Screen.

The schedule is 06:00 and 18:00 UTC, which is 7pm and 7am in New Zealand during daylight saving. Change the `cron` lines in `.github/workflows/run.yml` to move it.

## How the flight searches work

The workflow runs twice a day, but each search only calls the API every `check_every_days` days (default 7), because the free plan cannot pay for twice-daily sweeps of many date combinations. A search that is not due shows its last result on the dashboard. A failed check is retried on the next run.

Run `python run.py --plan` to see how many calls each search costs and the monthly total against the budget. If a sweep would go over `monthly_call_budget`, the whole sweep is skipped and the card says "budget reached".

### Fields

| Field | Meaning |
| --- | --- |
| `from`, `to` | An airport code, a list of codes, or `"@name"` for a set in `airport_sets`. A list is searched as one query. |
| `depart`, `return` | A date, a list of dates, or `{"from": ..., "to": ...}` for a range. Each date is one query. No `return` means one way. |
| `queries` | Optional list of blocks that override the search-level fields. Used when different dates need different airports or time windows. |
| `outbound_times`, `return_times` | Hour window for departure, for example `"16,23"`. |
| `stitch` | Two separate one-way tickets joined into one trip: `first`, `second`, `min_gap_hours`, `max_gap_hours`, `count_gap_nights`. |
| `arrive_between` | `["2027-07-26", "2027-07-30"]`. Only itineraries landing on those local dates count. |
| `overnight_ok` | Airport set where an overnight layover is acceptable. An overnight anywhere else rules the itinerary out. |
| `max_stops` | Optional stop limit. |
| `rules.base_cap` | Price limit with no overnight. |
| `rules.no_middle_east_cap` | Higher limit that applies when the route touches no airport in `airport_sets.middle_east`. |
| `rules.per_overnight_drop` | Amount taken off the limit for each overnight, including nights between two tickets. |
| `rules.cap_by_destination` | Replaces the base limit for a given arrival airport, for example `{"DUD": 1300}`. |
| `rules.prefer` | `"shortest"` or `"cheapest"`: which qualifying itinerary the card shows. |
| `enabled` | Set `false` to switch a search off without deleting it. |

An alert fires when something first fits within its limit, and again only after the cheapest qualifying price falls by a further `drop_alert_percent`. Repeated checks at the same price stay quiet. If nothing fits, the card shows the cheapest valid option and how far over its limit it is.

### What the current config assumes

- Spanish airports searched are mainland only: AGP, SVQ, GRX, MAD, VLC, ALC, BCN, BIO, XRY. Islands are left out because they cannot be reached by train. Add codes to `airport_sets.spain` to widen it.
- Overnight-friendly airports: AKL and CHC come from airporthotelsnearby.com (hotels opposite or inside the terminal zone). SIN, KUL, ICN, BKK, DEL, DOH, DXB, SHJ, BAH, MCT, JED, AMS, FRA, IST, CDG and ZRH come from sleepinginairports.net's transit hotel list, which says it covers only a few airports and does not say whether each hotel is airside. Edit `airport_sets.overnight_ok` freely. BNE, SYD and HKG were added at the user's request.
- Nights between the Gold Coast arrival and the Brisbane departure do not lower the limit, because accommodation is provided (`count_gap_nights` is `false`). The Sydney and Western Sydney gap still counts its nights.
- Gold Coast and Brisbane gap: 5 to 72 hours. Sydney and Western Sydney gap: 6 to 30 hours.
- Return search covers Malaga on 4 August and all Spanish airports 5 to 13 August, arriving BNE, OOL, MCY or DUD. A DUD arrival uses the 1300 limit, the others 1100. The same 200 per overnight applies to both.
- Wellington dates are 17, 18, 21 and 22 February 2027. Evening is treated as 16:00 to 23:59 and afternoon as 12:00 to 23:59.

### Facts checked while setting this up

- Jetstar flies Dunedin to Gold Coast direct, three flights a week, with the schedule varying by date (flightconnections.com).
- Western Sydney International opened 25 October 2026 with IATA code WSI. Singapore Airlines flies daily to Singapore from 23 November 2026 (Singapore Airlines press release, Australian Aviation).

## Add another agent

1. Create `agents/my_agent.py`:

```python
from core.base import Agent, AgentResult, Alert

class MyAgent(Agent):
    key = "my_agent"      # must match the key in config.json
    title = "My agent"    # heading on the dashboard

    def run(self) -> AgentResult:
        # self.settings : this agent's block from config.json
        # self.state    : whatever this agent returned in `state` last time
        # self.ctx      : env (secrets), now (UTC), http_get(url, timeout)
        return AgentResult(
            summary="1 thing found",
            items=[{"title": "Something", "value": "42", "badge": "new", "tone": "good"}],
            alerts=[Alert(title="Something happened", message="Details here", priority=3)],
            state={"seen": 1},
            calls_used=0,
        )
```

2. Add `"my_agent": { "enabled": true }` under `agents` in `config.json`.
3. If it needs a secret, add it to the repository secrets and pass it in the `Run agents` step of `.github/workflows/run.yml`.

Item fields are described at the top of `core/base.py`. An agent that raises an error is shown with the error on the dashboard and does not stop the others.

## Test locally

```
python -m unittest discover -s tests
python run.py --plan
SERPAPI_API_KEY=your_key python run.py --dry-run
```

`--dry-run` prints results and sends and saves nothing. The workflow runs the tests before every scheduled run.

## Known limits

- Scheduled workflows in public repositories are disabled automatically after 60 days with no repository activity (GitHub Docs). Each run commits its results, but whether that counts as activity was not verified. If the `agents` workflow shows as disabled, press Enable workflow in the Actions tab.
- SerpApi returns Google Flights results as a scraper, not a live fare feed. One comparison reports they may lag 15 to 30 minutes. Prices are indicative.
- Tests use hand-written responses based on SerpApi's documented shape, not captured live responses. Two fields were not confirmed against a live response: `layovers[].overnight` and the `YYYY-MM-DD HH:MM` time format. If overnights never show up, that is the first place to look.
- Google Flights returns a limited list per query, sorted by price here. A cheap itinerary outside that list will not be seen.
- Two-ticket trips (Gold Coast, Western Sydney) have no protection if the first flight is late. Prices are added, not bundled.
- The ntfy.sh public server is used by default. Set the `NTFY_SERVER` environment variable to use another server.
