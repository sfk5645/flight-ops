# One-time setup

These steps need a human: creating accounts and clicking through dashboards.
Cloudflare is the only account that takes a card. If any other service asks for one, stop and find
a free alternative.

## Accounts, by the phase that needs them

| Service | Used for | Needed in phase | Card? |
|---|---|---|---|
| GitHub | Code, CI, scheduled jobs (Actions) | 0 | No |
| Cloudflare | R2 data lake | 0 | Yes (R2 free tier: 10 GB) |
| Prefect Cloud | Flow run history | 1 | No |
| MotherDuck | Warehouse for dbt | 1 | No |
| OpenSky | Live positions | 3 | No. Read OpenSky's terms of use first |
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

### 3. Add the 30-day expiry rule

Raw 5-minute position snapshots grow by ~100+ MB/day. This rule deletes them after 30 days so the
bucket stays under the 10 GB free tier. Compacted data in `silver/` is kept.

**flight-ops-lake > Settings > Object lifecycle rules > Add rule**:
- Rule name: `expire-raw-positions`
- Applies to objects with prefix: `bronze/opensky_states/`
- Action: delete objects **30 days** after upload

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
