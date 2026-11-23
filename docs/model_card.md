# Model cards

Flight Ops has two delay models. Both are LightGBM quantile models. Each one predicts a middle guess (P50) and a range (P10 to P90) that should contain the real answer about 80% of the time.

| | Pre-departure | In-flight |
|---|---|---|
| Registered name (MLflow on DagsHub) | `predep-delay` | `inflight-landing` |
| Champion | version 4 | version 3 |
| Predicts | Minutes late at arrival, 1 hour before scheduled departure | Minutes until a plane in the air lands |
| Covers | US domestic flights of the airlines that report to BTS | Every tracked plane flying to a US airport in BTS |
| Typical error (P50 MAE) | **25.3 min** | **6.3 min** (2.9 in the last 15 min) |
| Range contains the answer | 79.8% | 80.0% |

Numbers were measured November 2026 on test periods the model never saw while training.

---

## Pre-departure: `predep-delay` v4

### What it does

Given a flight's carrier, route and scheduled times (entered by the user from their booking), it predicts the arrival delay in minutes (`arr_delay_min`; negative means early). The prediction moment is 1 hour before scheduled departure. Cancelled and diverted flights are excluded.

### Data

- **Labels:** BTS On-Time Performance (`ArrDelay`), Sep 2024 to Aug 2026.
- **Split by month, never random:**

| Part | Months | Flights |
|---|---|---|
| Train | Jan 2025 – Feb 2026 | 7.90M |
| Validate (early stopping, range calibration) | Mar – May 2026 | 1.79M |
| Test (final score) | Jun – Aug 2026 | 1.80M |

  Training starts in Jan 2025 because the delay-history inputs need 3 published months behind them.
- **Inputs (51), from `gold/training_set`:**

| Group | Examples |
|---|---|
| Flight | carrier, origin, destination, distance, scheduled hours, weekday, month, days to a federal holiday |
| Delay history | route, flight, carrier and airport-hour delays over the latest 1 and 3 published BTS months |
| Airport traffic | takeoffs at the origin and landings at the destination in the last 3 hours, vs normal for that hour |
| Today's lateness | how late takeoffs and landings are running today at both airports and for the carrier |
| Inbound plane | a guess of which flight brings the plane in, whether it has landed, and the turnaround buffer |
| Weather | latest METAR at both airports: flight category, thunderstorm, snow/ice, wind, gusts, change in 3 h |

- **Point-in-time correctness:** every input uses only what would have been known at the prediction moment. Delay history is lagged 3 months, because live BTS is about 3 months behind. A dbt test recomputes 300 sampled rows from scratch. The same test fails when the lag is deliberately broken, which shows it can catch a leak.

### How accurate it is (test months)

| Predictor | P50 error (min) | Within 5 min | Within 15 min | In range | Range width (min) | Pinball |
|---|---|---|---|---|---|---|
| Always 0 min late | 30.05 | 17.6% | 50.1% | 78.2% | 70.0 | 11.14 |
| Route + hour history | 29.19 | 24.0% | 57.4% | 76.7% | 63.1 | 10.65 |
| **LightGBM v4** | **25.29** | **23.4%** | **59.5%** | **79.8%** | **55.8** | **9.08** |

The model's range is narrower than the baselines' and catches the answer more often.

**Floor:** even knowing the *actual* departure delay gives an error of about 10 minutes, so a pre-departure error below ~10 is not reachable. Low 20s is a realistic goal.

### Where it is weak

- **Very late flights.** Flights 60+ minutes late are 10.5% of flights but about half of all error. The P50 is a median, so it stays cautious; the P90 is where those show up.
- **Inbound plane not seen.** When the guessed inbound plane hasn't been seen by prediction time (about 7% of flights), the error is about 49 min, against 66 for the baseline. That situation is itself a warning sign.
- **Thunderstorms.** About 3% of flights, error about 48 min.
- **Evening flights.** Error rises through the day (19 min in the morning, 35 in the evening) as delays pile up.
- **Training/serving skew still to measure.** Training builds the inbound guess and today's lateness from BTS. Live, they come from our own tracking, which finds about 90% of flights. Live accuracy will likely be somewhat worse than the test score.

### Common-sense rules

The model is forced to respect three directions (LightGBM monotone constraints on the P50):

| Rule | Test result (every sampled row) |
|---|---|
| A thunderstorm at the destination never makes a flight predicted earlier | 0 of 4,907 rows wrong; average +13.6 min |
| A thunderstorm at the origin never makes a flight predicted earlier | 0 of 4,934 rows wrong; average +22.7 min |
| An inbound plane 30 min later never makes a flight predicted earlier | 0 of 4,847 rows wrong; average +7.0 min |

Without these rules (v1), 7.7% of flights got earlier with a destination storm, and 8.7% got earlier with a later inbound plane (worst −24 min). The rules cost 0.06 min of P50 error.

**How they're enforced:** LightGBM can't constrain quantile objectives. So the P50 uses Huber loss (delta 10 min), which can be constrained. P10 and P90 stay quantile models, and each is pulled to the P50 if it crosses it, so the range never breaks the rule either.

---

## In-flight: `inflight-landing` v3

### What it does

Every tracked plane gets a prediction, every 15 minutes in training (every 5 minutes live): how many minutes until wheels-on. No schedule is needed. Adding the destination's usual taxi-in time and the user's scheduled arrival turns this into minutes late.

### Data

- **Positions:** adsb.lol daily history (Oct 22 – Nov 20, 2026) and our own OpenSky collection (Nov 21), one point per flight every 15 minutes.
- **Labels:** the landing time our tracking detects. Checked against BTS in `docs/landing_validation.md`: 98.7% of seen landings within 2 minutes.
- **Split by takeoff day, never random:**

| Part | Days | Moments |
|---|---|---|
| Train | Oct 22 – Nov 10 | 3.49M |
| Validate | Nov 11 – 15 | 0.84M |
| Test | Nov 16 – 21 | 1.13M |

- **Inputs (17):**

| Group | Inputs |
|---|---|
| Flight | carrier, origin, expected destination, destination local hour and weekday |
| Position | altitude, ground speed, vertical rate, distance to destination, heading away from it, distance ÷ speed |
| Progress | minutes flown, whether takeoff was seen, the route's usual air time (BTS, lagged 3 months), its remaining part, how many flights land at the destination that hour |

- **Destination is a guess.** A plane doesn't broadcast where it's going. The destination is the one this callsign most often flew to from this origin over the previous 7 days, using only flights that had already landed. The guess is right 95.5% of the time (98% for US airlines). Flights with no guess get no prediction.
- **Point-in-time correctness:** a dbt test recomputes 300 sampled rows (destination guess, route air time, minutes flown). Deliberately breaking the BTS lag makes it fail on 209 of 300 rows.

### How accurate it is (test days)

| Predictor | P50 error (min) | Within 5 min | Within 15 min | In range | Range width (min) | Pinball |
|---|---|---|---|---|---|---|
| Distance ÷ speed | 34.00 | 15.4% | 63.2% | 79.3% | 46.1 | 14.26 |
| Route's usual air time − time flown | 17.44 | 39.2% | 79.1% | 80.0% | 32.0 | 7.12 |
| **LightGBM v3** | **6.33** | **69.7%** | **91.1%** | **80.0%** | **10.9** | **2.13** |

**By minutes left to landing:**

| Minutes left | P50 error (min) | Within 5 min | In range |
|---|---|---|---|
| 0 – 15 | 2.89 | 89.6% | 77.8% |
| 15 – 30 | 4.01 | 82.6% | 80.3% |
| 30 – 60 | 4.70 | 78.3% | 82.4% |
| 60 – 120 | 6.19 | 67.4% | 80.9% |
| 120 – 240 | 10.11 | 45.7% | 78.2% |
| 240 + | 18.45 | 25.0% | 76.5% |

**By feed:** adsb.lol 6.12 min, OpenSky 7.63 min. OpenSky positions are older (about 112 s vs 27 s) and only one test day came from it.

**Range calibration:** the raw range caught only 73% of answers. It is widened using shifts measured on the validation days, separately for each band of predicted minutes left (15/30/60/120/240). One shift for everything would over-cover short horizons and under-cover long ones.

### Where it is weak

- **Wrong destination guess** (about 4% of moments): error about 35 min, against 5 when the guess is right.
- **Long flights:** more than 4 hours out, the error is about 18 min. Winds, routing and oceanic coverage gaps matter more than anything the model sees.
- **Short history:** the first week of backfill had little destination history, so guesses there exist only for frequent callsigns.
- **No weather yet.** Destination weather will be added once November's weather archive is published.
- **Holding and diversions** aren't recognised yet. Those come with the anomaly work (Phase 6).

### Common-sense rules

None are forced yet. The rule "further from the airport means landing later" was tested (v2) and dropped. It made the last 15 minutes worse (2.82 → 4.12 min), because planes often fly away from the runway while lining up to land. A destination-storm rule will be added with destination weather.

### Tried and not used

Destination traffic (landings in the last hour vs normal, planes heading there) didn't help (P50 6.32 → 6.35). It stays in the training set for the airport-disruption anomaly rule.

---

## Promotion gates

A new version becomes champion only by passing `flows/evaluate_model.py`:

1. It reloads from the registry and reproduces its logged score (±0.01 min).
2. It beats both baselines.
3. Its range contains 75–85% of answers.
4. It beats the champion on pinball loss.
5. None of the 20 biggest airports or carriers is more than 2 min worse than with the champion.
6. Every common-sense rule holds on every sampled row.
7. Its inputs match what the live code sends (MLflow signature).
8. It scores 10,000 rows in under 60 seconds.

Both champions passed every gate (Nov 23, 2026). Pre-departure v3 failed rule 6: a later inbound plane made 9 of 4,847 flights earlier.

## Retraining and rollback

- **Pre-departure:** monthly, when a new BTS month is published. It is graded on the newest month.
- **In-flight:** challengers also run in shadow mode for 7 days (scored live, not shown) before they can replace the champion. *(Phase 5.)*
- **Rollback:** point the `champion` alias back at the previous version.

## Intended use and limits

- **Intended use:** a personal, educational tool. It shows an expected delay and a range, not a guarantee.
- **Not for:** operational or safety decisions, or anything commercial.
- **Coverage:** pre-departure covers only the 12 airlines BTS reports. In-flight covers only planes with ADS-B coverage that fly to US airports in BTS.
- **Privacy:** nothing published here identifies an aircraft. No callsigns, transponder codes or tail numbers appear in this repo's docs or in the public MLflow project.

## Data credits

- BTS On-Time Performance (US DOT)
- OpenSky Network (Schäfer et al., IPSN 2014), for research and education
- adsb.lol (ODbL)
- Iowa Environmental Mesonet (METAR archive)
- OurAirports and OpenFlights
