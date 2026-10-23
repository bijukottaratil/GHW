# Global Hazard Watch

A live world map of earthquakes, tropical cyclones, wildfires, volcanoes and civil unrest, with Moody's RMS and Verisk event response, a perils-by-continent table, and an event history with economic and insured loss estimates.

It runs entirely on free GitHub services:

- **GitHub Pages** hosts the website.
- **GitHub Actions** runs `scripts/update.py` every hour. The script pulls the latest data and commits fresh `data.json` and `history.json`, and Pages republishes the site.

Open pages also re-check for new data every 10 minutes.

## What's in the folder

| File | What it does |
|---|---|
| `index.html` | The website |
| `data.json` | Live events (rebuilt every hour) |
| `history.json` | Permanent event archive with loss estimates (grows over time, never deletes) |
| `land.json` | World map outline |
| `scripts/update.py` | The hourly updater |
| `.github/workflows/update.yml` | The hourly schedule |
| `requirements.txt` | Python packages the updater needs |

## Setup (about 15 minutes, on a computer)

1. **Create a GitHub account** at github.com if you don't have one.
2. **Create a repository.** Click **New repository**, name it (for example `hazard-watch`), choose **Public**, and click **Create repository**. Free GitHub Pages needs a public repository unless you have a paid plan.
3. **Upload the files.** On the new repository page, click **uploading an existing file**. Drag in everything from this folder **except** the `.github` folder, then click **Commit changes**.
4. **Add the hourly schedule.** Many computers hide folders that start with a dot, so create this file by hand:
   - Click **Add file → Create new file**.
   - In the name box type exactly `.github/workflows/update.yml`.
   - Paste in the contents of `update.yml` from this folder, then click **Commit changes**.
5. **Let the schedule save changes.** Go to **Settings → Actions → General → Workflow permissions**, choose **Read and write permissions**, and click **Save**.
6. **Turn on the website.** Go to **Settings → Pages**. Under **Source** choose **Deploy from a branch**, pick **main** and **/ (root)**, and click **Save**. After a minute or two the page shows your site address, for example `https://yourname.github.io/hazard-watch/`.
7. **Run the first update.** Go to the **Actions** tab, click **Hourly hazard update → Run workflow**. After it finishes (green tick), refresh your site. From then on it runs every hour on its own.

## Optional: loss research with the Claude API

Without an API key everything above still works: hazard feeds, modeler status, the map, the continent table and the archive. What you lose is automatic research of **economic and insured loss estimates** and death tolls for new events, plus the more reliable reading of the Moody's RMS and Verisk pages.

To switch it on:

1. Create an API key at console.anthropic.com (this is billed separately from a Claude.ai subscription).
2. In your repository go to **Settings → Secrets and variables → Actions → New repository secret**. Name it `ANTHROPIC_API_KEY` and paste the key.

Built-in cost controls:

- Each run researches at most **4 events**.
- Each event is researched at most once every **12 hours**.
- Only notable events from the last 90 days are researched: orange or red alerts, M6.5+ earthquakes, named storms, and anything Moody's RMS or Verisk covers.

You can adjust these under **Settings → Secrets and variables → Actions → Variables**:

- `MAX_LOSS_RESEARCH`: events per run.
- `CLAUDE_MODEL`: defaults to `claude-sonnet-5-5`.

Check your spend in the Anthropic Console after the first day.

The script only records loss figures it found in a published source. Each estimate is stored with the estimator's name and a link.

## Sources

- **USGS:** M5+ earthquakes from the past week, plus significant quakes from the past 2 weeks.
- **GDACS (UN / EU JRC):** alert levels, tropical cyclones and satellite-detected wildfires.
- **NASA EONET:** tropical cyclones and large US wildfires.
- **Smithsonian Global Volcanism Program:** weekly volcanic activity report.
- **Moody's RMS Event Response:** public summaries only. Full reports need a Moody's login.
- **Verisk ALERT (formerly AIR):** event status and published loss estimates.
- **Nominatim (OpenStreetMap):** places modeler-only events on the map when no coordinates are available.

## Good to know

- **Bad hours are absorbed.** If a source is down for an hour, its previous data is kept, so the site never goes blank. The Actions tab shows a red cross only when nearly every source fails.
- **Modeler pages may need fixing after redesigns.** Moody's RMS and Verisk publish web pages, not data feeds. If either site changes its layout, their sections may stop updating until `fetch_rms` / `fetch_verisk` in `scripts/update.py` are adjusted. With the Claude API key set, page changes matter much less.
- **Scheduled runs can drift.** GitHub sometimes starts scheduled runs 5–15 minutes late at busy times.
- **Inactive repos pause.** GitHub pauses schedules in repositories with no activity for 60 days. The hourly data commits count as activity, so this shouldn't happen while the updater is working.
- **Continents are approximate.** The page assigns continents from coordinates. Central America counts as North America, Hawaiʻi is grouped with North America, and mid-ocean ridge quakes are "Open ocean".
- **Local warnings come first.** This is an information dashboard. For safety decisions follow official local warnings.
