# Landing validation

The in-flight model is trained and graded on the takeoffs and landings our tracking detects. This page records how well that detection agrees with BTS, the official record of US airline flights. This is Phase 3's pass test.

Measured November 2026 on three days in August 2026: Friday the 14th, Saturday the 15th and Tuesday the 18th. That's 38,579 BTS flights.

## How it's tested

Our OpenSky collection started in November, and BTS publishes about two months late. So the test uses an older month that BTS already has, with positions from [adsb.lol](https://adsb.lol)'s daily history archive instead of OpenSky:

1. **Download a day of adsb.lol history.** The archive is about 4 GB and is streamed, not stored.
2. **Thin it to one position per plane every 5 minutes.** That's what our OpenSky snapshots give us, so the test grades the feed we actually run on.
3. **Run the same tracking code as the daily job.** That covers legs, airports and flight numbers.
4. **Compare with BTS.** BTS has no wheels-off or wheels-on columns, but they follow from what it has. Wheels-off is the actual departure plus taxi-out. Wheels-on is the actual arrival minus taxi-in. Both are in whole minutes.

Only flights that took off between 02:00 and 20:00 UTC are graded. The positions cover one UTC day, so flights near midnight are cut off. BTS dates flights by local departure date, so BTS flights from the day before and the day after are included too.

To re-run a day:

```
uv run --env-file .env python -m flightops.flows.validate_landings --date 2026-08-18
```

## Results

| Pass check | Target | Result |
|---|---|---|
| BTS flights found | ≥ 85% | **90.3%** |
| Median landing time gap | ≤ 3 min | **0.4 min** |
| Seen landings within 5 min | ≥ 90% | **99.9%** |

### Found: BTS flights we tracked

A BTS flight counts as found when we tracked a flight with the same carrier, flight number, origin and destination, taking off within an hour of BTS's wheels-off time.

| Day | BTS flights | Found |
|---|---|---|
| Fri 2026-08-14 | 13,461 | 90.3% |
| Sat 2026-08-15 | 12,361 | 90.1% |
| Tue 2026-08-18 | 12,757 | 90.5% |
| **All** | **38,579** | **90.3%** |

By carrier (all three days):

| Carrier | BTS flights | Found |
|---|---|---|
| Southwest (WN) | 7,430 | 95.6% |
| Delta (DL) | 5,996 | 96.3% |
| American (AA) | 5,393 | 92.8% |
| United (UA) | 4,626 | 91.4% |
| SkyWest (OO) | 4,557 | 74.0% |
| Republic (YX) | 2,056 | 91.1% |
| Alaska (AS) | 1,920 | 80.9% |
| Envoy (MQ) | 1,832 | 81.6% |
| JetBlue (B6) | 1,522 | 91.8% |
| PSA (OH) | 1,340 | 93.9% |
| Frontier (F9) | 1,272 | 96.5% |
| Allegiant (G4) | 635 | 86.0% |

### Confirmed: our flights that are in BTS

The check also runs the other way. It covers our flights for a BTS carrier, between two airports BTS serves, with both ends known. **98.4%** of them (35,381 flights) are in BTS with the same carrier, number and route. The rate is the same whether we saw the airport or inferred it: 97–99% for every combination with more than 100 flights.

### Timing: our times vs BTS

The gap is our time minus BTS's, in minutes. BTS times are whole minutes, so about ±0.5 minutes of the gap is rounding.

| Event | How we know it | Flights | Within 2 min | Within 5 min |
|---|---|---|---|---|
| Landing | Seen on the ground | 25,738 | 98.7% | 99.9% |
| Landing | Inferred (signal lost low near the airport) | 9,083 | 99.1% | 99.8% |
| Takeoff | Seen on the ground | 23,533 | 94.5% | 99.6% |
| Takeoff | Previous leg's landing airport | 3,605 | 99.6% | 99.8% |

The median gap is 0.0–0.2 minutes for landings and 0.5–0.7 minutes for takeoffs on every day. For takeoffs, 5% to 95% of gaps fall between −1.4 and +1.8 minutes.

### Misses

Why the 9.7% of BTS flights weren't found (all three days):

| Reason | Flights |
|---|---|
| Takeoff airport unknown (first seen already high, no earlier landing to borrow) | 1,577 |
| Landing airport unknown (signal lost too high to infer an airport) | 1,363 |
| No tracked flight with that flight number | 745 |
| Different airport | 30 |
| Neither airport known | 29 |

## What we learned

- **When we name an airport, it's right.** Only 30 of 38,579 flights had a wrong airport, and 98.4% of our flights are confirmed by BTS. Almost every miss is an airport we couldn't name at all, not a wrong guess.
- **Misses are coverage, not logic.** SkyWest (74%), Alaska (81%) and Envoy (82%) fly the most small regional airports: mountain towns, northern Michigan, Alaska. Few receivers there hear planes close to the ground, so takeoffs and landings happen out of view.
- **Working backward from the descent makes landing times exact.** The plane's last airborne position is up to 5 minutes before touchdown. Height above the runway divided by descent rate covers that gap. Without it, seen landings would read 0–5 minutes late.
- **BTS dates flights by local time.** A flight leaving at 03:00 UTC is dated the day before in BTS. Joining by UTC date alone made 20% of our flights look missing from BTS on the first try.
- **"No tracked flight with that flight number"** includes regional flights flown under callsigns with a letter suffix (like a SkyWest number ending in W). Callsign mapping skips these on purpose.

## Caveats

- **adsb.lol is a bit kinder than OpenSky.** At snapshot time its positions are about 27 seconds old on average, against about 2 minutes for OpenSky. Because landing time is worked out from the descent, this affects timing little. It may raise the found rate slightly, since a fresher last position is more often low enough to infer an airport.
- **Three days, one month.** Winter weather, holidays and schedule changes aren't covered. The in-flight model's daily grading will keep checking landings on our own data.
- **US airlines only.** BTS covers 12 carriers. Detection for other airlines, business jets and private flights uses the same code but isn't graded here.

Positions: [adsb.lol](https://adsb.lol), Open Database License. Flights: [BTS On-Time Performance](https://www.transtats.bts.gov/).
