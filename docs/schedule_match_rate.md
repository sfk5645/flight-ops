# Schedule match rate

`dim_schedule` guesses each flight's timetable from past BTS data. This page records how often that guess is right. The numbers come from `gold/schedule_match_rate`, which rebuilds the timetable from older data only and checks it against the month that followed.

Measured November 2026 on March–August 2026 (about 3.7M flights).

## How it's graded

- **Matched:** the flight's carrier, number and route are in the timetable at all.
- **Within 5 / 15 min:** both scheduled departure and arrival are within 5 (or 15) minutes of the real schedule.
- **Lag 0:** timetable built from the 60 days right before the month. Best case.
- **Lag 2:** timetable built from 60 days ending two months earlier. This is what live tracking actually has, since BTS publishes about two months late.

## Results

| Staleness | Matched | Within 5 min | Within 15 min |
|---|---|---|---|
| Lag 0 | 71.3% | 45.4% | 55.2% |
| Lag 2 | 41.8% | 8.3% | 18.9% |

By month:

| Month | Lag 0 matched | Lag 0 within 5 | Lag 2 matched | Lag 2 within 5 |
|---|---|---|---|---|
| 2026-03 | 64.1% | 32.7% | 46.4% | 8.5% |
| 2026-04 | 59.0% | 38.5% | 42.0% | 6.6% |
| 2026-05 | 78.1% | 49.3% | 37.1% | 5.5% |
| 2026-06 | 72.6% | 49.5% | 35.4% | 5.7% |
| 2026-07 | 94.5% | 65.4% | 38.9% | 7.0% |
| 2026-08 | 58.6% | 36.1% | 50.8% | 16.7% |

By carrier (lag 0): United is the most predictable (66% within 5 min), followed by Alaska (62%). Southwest (27%) and Allegiant (18%) are the least.

## What we learned

- **Airlines retime flights constantly.** DL1184 left at 05:40, then 05:51 from June 7, then 05:49 from July 1. That's why the timetable uses the most recent time rather than the most common one.
- **Flight numbers don't stick to weekdays for every airline.** Southwest moves numbers between days, so keying on weekday left most of its flights without enough history. The timetable keys on carrier, number and route only.
- **Matching jumps around month to month.** Big schedule changes, such as the switch in early July, decide how much carries over.
- **A two-month-old timetable is rarely exact.** Only about 1 in 12 flights is within 5 minutes. A live "minutes late" estimate built on this timetable alone would be unreliable for most flights.

## What we decided

A two-month-old timetable is too unreliable to build live predictions on, and no rule fixes that, because the data is stale, not the method. So:

- **Scheduled times come from the user.** When looking up a flight, the user enters its scheduled departure and arrival from the booking. `dim_schedule` only pre-fills a suggestion.
- **The in-flight model predicts landing time, not minutes late.** It needs no schedule, so it works for every tracked plane. It's graded every day against the landings we observe. With the user's scheduled arrival, a predicted landing becomes minutes late.
- **The pre-departure model still predicts minutes late.** It's graded monthly against official BTS delays.

## Rules tried

Rules tried before settling on the current one (same months, % of all flights within 5 min, lag 0 / lag 2):

| Rule | Lag 0 | Lag 2 |
|---|---|---|
| Weekday + most common time (original plan) | 32.7% | 5.8% |
| Any day + most common time | 36.9% | 7.3% |
| Any day + most recent time (**current**) | 44.7% | 8.3% |
| Weekday if known, else any day, most recent time | 43.4% | 7.5% |
