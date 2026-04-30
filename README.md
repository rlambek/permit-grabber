# campflare-bot

Two scripts that pair together:

1. **`scan_alerts.py`** — polls Gmail for Campflare permit-found alerts, parses them, and emits a JSON payload.
2. **`book_permit.py`** — takes that payload, drives recreation.gov to the cart for the right permit/date, and stops before payment so a human commits.

## Disclaimer

Not affiliated with or endorsed by recreation.gov, Campflare, or the National Park Service. Recreation.gov's terms of service likely prohibit automated booking, and operators monitor traffic — running this aggressively (parallel instances, tight polling loops, headless-without-pacing) **can get your account terminated**. This project intentionally:

- Stops at the cart so the final commit (payment) is a human action.
- Runs one instance at a time per alert.
- Pre-checks availability via the public read-only API before launching a browser, so a stale alert doesn't trigger a session.

Use at your own risk for personal, occasional use.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
playwright install chromium
```

### Gmail credentials

1. Go to <https://console.cloud.google.com/>, create a project, enable the **Gmail API**.
2. Create an **OAuth client ID** (Desktop app), download the JSON, save it as `credentials.json` in this folder.
3. First run of `scan_alerts.py` opens a browser to authorize and writes `token.json`.

### Recreation.gov credentials

Set as environment variables — the script never logs them:

```bash
set RECGOV_EMAIL=you@example.com
set RECGOV_PASSWORD=...
```

### Permit config (optional but recommended)

Campflare's "found" alert email may not include the recreation.gov permit URL or your group size. Pre-populate them by permit name in `permit_config.json`:

```json
{
  "Dinosaur Green And Yampa River Permit": {
    "permit_url": "https://www.recreation.gov/permits/250014",
    "group_size": 4
  }
}
```

The scanner falls back to these values when the email lacks them.

## Usage

One-shot scan, pipe to booking script:

```bash
python scan_alerts.py | python book_permit.py --alert-stdin
```

Watch mode (poll every 60s) — emits one JSON line per new alert; pipe to `xargs` or your runner:

```bash
python scan_alerts.py --watch 60
```

Manual booking with a hand-crafted payload:

```bash
python book_permit.py --alert "{\"permit_name\":\"Half Dome\",\"permit_url\":\"https://www.recreation.gov/permits/234652\",\"date\":\"2026-07-15\",\"group_size\":4,\"alert_received_at\":\"2026-04-29T10:30:00Z\"}"
```

## Known guesses (verify against a real alert + a real permit page)

`scan_alerts.py`:
- Assumes the "found" alert from `no-reply@campflare.com` follows the same `Permit:` / date-list shape as the confirmation email. Only the confirmation format has been observed.
- Recreation.gov URL extraction is opportunistic — falls back to `permit_config.json` if absent.

`book_permit.py` (every selector marked `# TODO: verify selector`):
- Login modal field labels (`Email Address`, `Password`).
- Date picker trigger button name and day-cell aria-label format.
- Group-size input label.
- The Book/Reserve button label sequence.

Run once headed (`without --headless`) against a real permit page and tighten any selector that misses.

## Processed-alert tracking

The scanner adds the Gmail label `campflare-processed` to alerts it emits, so the same alert isn't booked twice. Remove the label manually to re-process.
