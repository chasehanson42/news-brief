# Daily News Brief

This project pulls the last 24 hours of headlines from the New York Times, Wall Street Journal, the Guardian, NPR and BBC, removes duplicate stories, and uses a free LLM API to write a short summary report. GitHub Actions runs it every morning in the cloud and emails the report. Your computer does not need to be on, and the whole thing costs $0.

## How it works

1. Fetch: the NYT Top Stories API, the Guardian Open Platform API, and free RSS feeds (WSJ, NPR, BBC).
2. Filter: drop articles older than `lookback_hours`, remove repeated URLs and near-identical headlines, and optionally keep only articles that match `topics`.
3. Summarize: send the numbered article list to a free LLM, which writes a brief with citations like [3]. The script tries Gemini first, then Groq, then OpenRouter, and uses the first one that works.
4. Deliver: save the report to `reports/YYYY-MM-DD.md` and email it.

## Setup

### 1. Get free API keys

| Key | Where | Needed? |
|---|---|---|
| `GEMINI_API_KEY` | aistudio.google.com > Get API key | Yes (primary LLM) |
| `GROQ_API_KEY` | console.groq.com > API Keys | Optional backup |
| `OPENROUTER_API_KEY` | openrouter.ai > Keys | Optional backup |
| `NYT_API_KEY` | developer.nytimes.com (enable the Top Stories API) | Optional |
| `GUARDIAN_API_KEY` | open-platform.theguardian.com (developer key) | Optional |
| Gmail App Password | Google Account > Security > App passwords | For email |

None of these need a credit card. Adding the two backup LLM keys is recommended, since free tiers sometimes hit rate limits or change models.

### 2. Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then fill in your keys
python brief.py --dry-run   # check the sources without calling the LLM
python brief.py --no-email  # write a report to reports/
python brief.py             # full run with email
```

### 3. Run every day on GitHub

1. Push this folder to a GitHub repository. A private repo is recommended.
2. Go to Settings > Secrets and variables > Actions and add each value from `.env.example` as a repository secret.
3. Open the Actions tab, pick "Daily News Brief", and click "Run workflow" to test it.
4. After that it runs every day at 11:00 UTC (7 AM Eastern in summer, 6 AM in winter). Change the `cron` line in `.github/workflows/daily-brief.yml` to change the time.

Note: GitHub turns off scheduled workflows in a repo after 60 days with no commits. Push a small change now and then, or re-enable the workflow from the Actions tab.

## Customizing

All settings are in `config.yaml`:

- `llm.providers`: the order, models and limits for each free LLM. Any OpenAI-compatible API can be added with a `base_url`, `model` and `api_key_env`.
- `topics`: keywords to focus the brief (for example `federal reserve`, `earnings`).
- `sources.rss`: add or remove feeds. Any RSS URL works.

To change the report layout or writing style, edit `SYSTEM_PROMPT` and `REPORT_TEMPLATE` in `brief.py`.

Free model names change over time. If a run log shows a "model not found" error, check the provider's model list and update `model` in `config.yaml`.

## Content, privacy and licensing notes

- RSS feeds and the NYT API return headlines and short summaries, not full articles. The brief is based on those summaries.
- WSJ has no free API. Full-text WSJ access needs a paid Dow Jones/Factiva license.
- Gemini's free tier may use prompts to improve Google's products. That is fine for public news headlines, but do not send private or company data through it.
- NPR and BBC feeds are licensed for personal use. Keep reports private and do not republish them.
- If this code is published as open source, keep keys in secrets, keep `reports/` out of the repo (it is in `.gitignore`), and check with your employer about who owns the code first.
