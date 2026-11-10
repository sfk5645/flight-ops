# One-time setup

These steps need a human: creating accounts and clicking through dashboards.
Cloudflare is the only account that takes a card. If any other service asks for one, stop and find
a free alternative.

## Accounts, by the phase that needs them

| Service | Used for | Needed in phase | Card? |
|---|---|---|---|
| GitHub | Code, CI, scheduled jobs (Actions) | 0 | No |
| Cloudflare | R2 data lake | 0 | Yes (R2 free tier: 10 GB) |
| Prefect Cloud | Flow run history | 3 | No |
| OpenSky | Live positions | 3 | No. Read OpenSky's terms of use first |
| cron-job.org | Starts the 5-minute and daily workflows on time | 3 | No |
| DagsHub | MLflow tracking and registry | 4 | No |
| Grafana Cloud | Pipeline dashboards and alerts | 7 | No |

## Phase 0: Cloudflare R2

### 1. Enable R2

Sign up at Cloudflare, open **R2 Object Storage**, and enable it (asks for a card).
Note your **account ID** (R2 overview page, right side).

### 2. Create the bucket

**R2 > Create bucket**:
- Name: `flight-ops-lake`
- Location: Automatic, or a location hint of Eastern North America (close to GitHub's runners)
- Storage class: Standard

### 3. Add the 3-day expiry rule

Raw 5-minute position snapshots add about 110 MB a day. The daily job turns each day into small
per-flight results the evening after (once every flight that took off that day has landed), so
the raw files are only needed for a couple of days. This rule deletes them after 3 days: one day
of slack, so a failed daily run can be retried the next evening. That keeps them at ~330 MB,
well under the 10 GB free tier. Everything else is
trimmed to a rolling window by `flightops.flows.prune_lake` (see `src/flightops/retention.py`).

**flight-ops-lake > Settings > Object lifecycle rules > Add rule**:
- Rule name: `expire-raw-positions`
- Applies to objects with prefix: `bronze/opensky_states/`
- Action: delete objects **3 days** after upload

### 4. Create an API token for the code

**R2 > Manage API tokens > Create API token**:
- Permissions: **Object Read & Write**
- Specify bucket: `flight-ops-lake` only

Copy the **Access Key ID** and **Secret Access Key** right away. The secret is shown only once.

### 5. Store the credentials

- **Local:** `cp .env.example .env` and fill in `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`,
  `R2_SECRET_ACCESS_KEY`.
- **GitHub Actions:** add the same three under Settings > Secrets and variables > Actions > Secrets.
  Nothing uses them until Phase 1.

## Phase 3: 5-minute timer

GitHub's built-in `schedule:` trigger is best-effort: when we tried it, the 5-minute snapshot
workflow ran once in 16 hours. So a free external timer, cron-job.org, starts the workflow
through GitHub's API instead.

### 1. Create a fine-grained token (GitHub, as the repo owner)

**Settings > Developer settings > Personal access tokens > Fine-grained tokens > Generate new token**:
- Name: `cron-job-snapshot`
- Repository access: **Only select repositories** > `flight-ops`
- Permissions: **Actions: Read and write** (Metadata: Read-only is added automatically)

The token can only start, cancel and re-run workflows in this repo. Copy it right away; it is
shown only once.

### 2. Create the job (cron-job.org)

**Cronjobs > Create cronjob**:
- URL: `https://api.github.com/repos/sfk5645/flight-ops/actions/workflows/opensky-snapshot.yml/dispatches`
- Schedule: every 5 minutes
- Advanced > Request method: `POST`
- Advanced > Headers:
  - `Authorization`: `Bearer <token>`
  - `Accept`: `application/vnd.github+json`
  - `X-GitHub-Api-Version`: `2022-11-28`
  - `Content-Type`: `application/json`
- Advanced > Request body: `{"ref":"main"}`
- Notify on failure: on (an expired or deleted token shows up as failures)

A successful call returns `204`, and a `workflow_dispatch` run appears in the Actions tab.

### 3. Create the daily job (cron-job.org)

Same token and headers as step 2, with:
- URL: `https://api.github.com/repos/sfk5645/flight-ops/actions/workflows/daily.yml/dispatches`
- Schedule: every day at **20:00 UTC** (set the job's time zone to UTC)
- Request body: `{"ref":"main"}`

It builds the previous UTC day's `silver/tracked_flights` file once every flight that took off
that day has landed, then runs `prune_lake`. To rebuild a specific day, run the `daily`
workflow by hand from the Actions tab and enter the date.
