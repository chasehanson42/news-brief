"""
Daily News Brief
----------------
Pulls the last day of headlines from NYT, WSJ, the Guardian, NPR and BBC,
removes duplicates, asks a free LLM API to write a summary report, then
saves it and (optionally) emails it.

The LLM step tries each provider in config.yaml in order (Gemini, then Groq,
then OpenRouter by default) and uses the first one that works, so a single
provider outage or rate limit does not stop the daily run.

Usage:
    python brief.py              # full run
    python brief.py --dry-run    # fetch and dedupe only, print what would be sent to the LLM
    python brief.py --no-email   # write the report but skip email

Environment variables (see .env.example):
    GEMINI_API_KEY      free key from aistudio.google.com (primary)
    GROQ_API_KEY        free key from console.groq.com (backup, optional)
    OPENROUTER_API_KEY  free key from openrouter.ai (backup, optional)
    NYT_API_KEY         optional, falls back to NYT RSS
    GUARDIAN_API_KEY    optional, Guardian is skipped without it
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, EMAIL_TO   for email delivery
"""

import argparse
import html
import os
import re
import smtplib
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import feedparser
import requests
import yaml

HERE = Path(__file__).parent
USER_AGENT = "daily-news-brief/1.0 (personal research project)"
TIMEOUT = 20


# ---------------------------------------------------------------- data model

@dataclass
class Article:
    source: str
    title: str
    summary: str
    url: str
    published: datetime | None


def clean_text(text: str) -> str:
    """Strip HTML tags and extra whitespace."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def parse_date(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------- fetchers

def fetch_nyt(sections: list[str], api_key: str) -> list[Article]:
    articles = []
    for section in sections:
        url = f"https://api.nytimes.com/svc/topstories/v2/{section}.json"
        try:
            r = requests.get(url, params={"api-key": api_key}, timeout=TIMEOUT)
            r.raise_for_status()
        except requests.RequestException as e:
            print(f"  [warn] NYT {section}: {e}", file=sys.stderr)
            continue
        for item in r.json().get("results", []):
            if not item.get("title"):
                continue
            articles.append(Article(
                source=f"New York Times - {section.title()}",
                title=item["title"],
                summary=clean_text(item.get("abstract", "")),
                url=item.get("url", ""),
                published=parse_date(item.get("published_date")),
            ))
    return articles


def fetch_guardian(sections: list[str], api_key: str, since: datetime) -> list[Article]:
    articles = []
    for section in sections:
        try:
            r = requests.get(
                "https://content.guardianapis.com/search",
                params={
                    "api-key": api_key,
                    "section": section,
                    "from-date": since.date().isoformat(),
                    "order-by": "newest",
                    "page-size": 20,
                    "show-fields": "trailText",
                },
                timeout=TIMEOUT,
            )
            r.raise_for_status()
        except requests.RequestException as e:
            print(f"  [warn] Guardian {section}: {e}", file=sys.stderr)
            continue
        for item in r.json().get("response", {}).get("results", []):
            articles.append(Article(
                source=f"The Guardian - {item.get('sectionName', section)}",
                title=item.get("webTitle", ""),
                summary=clean_text(item.get("fields", {}).get("trailText", "")),
                url=item.get("webUrl", ""),
                published=parse_date(item.get("webPublicationDate")),
            ))
    return articles


def parse_feed(name: str, content: bytes) -> list[Article]:
    feed = feedparser.parse(content)
    articles = []
    for e in feed.entries:
        published = None
        if getattr(e, "published_parsed", None):
            published = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
        elif getattr(e, "updated_parsed", None):
            published = datetime(*e.updated_parsed[:6], tzinfo=timezone.utc)
        articles.append(Article(
            source=name,
            title=clean_text(e.get("title", "")),
            summary=clean_text(e.get("summary", "")),
            url=e.get("link", ""),
            published=published,
        ))
    return articles


def fetch_rss(name: str, url: str) -> list[Article]:
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"  [warn] {name}: {e}", file=sys.stderr)
        return []
    return parse_feed(name, r.content)


def collect(cfg: dict) -> list[Article]:
    since = datetime.now(timezone.utc) - timedelta(hours=cfg.get("lookback_hours", 24))
    src = cfg["sources"]
    nyt_key = os.getenv("NYT_API_KEY")
    guardian_key = os.getenv("GUARDIAN_API_KEY")
    articles: list[Article] = []

    if src.get("nyt_api", {}).get("enabled") and nyt_key:
        print("Fetching NYT API...")
        articles += fetch_nyt(src["nyt_api"]["sections"], nyt_key)

    if src.get("guardian_api", {}).get("enabled") and guardian_key:
        print("Fetching Guardian API...")
        articles += fetch_guardian(src["guardian_api"]["sections"], guardian_key, since)

    for feed in src.get("rss", []):
        if feed.get("nyt_fallback") and nyt_key:
            continue  # NYT API already covers this
        print(f"Fetching {feed['name']}...")
        articles += fetch_rss(feed["name"], feed["url"])

    # Keep recent articles (undated ones are kept, since some feeds omit dates)
    articles = [a for a in articles if a.title and (a.published is None or a.published >= since)]
    return articles


# ---------------------------------------------------------------- filtering

def normalize(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", title.lower())


def dedupe(articles: list[Article], threshold: float = 0.8) -> list[Article]:
    """Drop exact URL repeats and near-identical headlines."""
    kept: list[Article] = []
    seen_urls = set()
    for a in articles:
        if a.url and a.url in seen_urls:
            continue
        norm = normalize(a.title)
        if any(SequenceMatcher(None, norm, normalize(k.title)).ratio() >= threshold for k in kept):
            continue
        seen_urls.add(a.url)
        kept.append(a)
    return kept


def topic_filter(articles: list[Article], topics: list[str]) -> list[Article]:
    if not topics:
        return articles
    topics = [t.lower() for t in topics]
    return [a for a in articles if any(t in f"{a.title} {a.summary}".lower() for t in topics)]


# ---------------------------------------------------------------- summary

SYSTEM_PROMPT = """You write a daily news brief from a list of headlines and short summaries.
Rules:
- Use only the information in the provided articles. Do not add outside facts.
- Group articles that cover the same event into one story.
- Cite sources with bracketed numbers that match the article list, e.g. [3][7].
- If sources disagree or a story is still developing, say so.
- Write in plain, direct sentences. Do not use em dashes.
- Output Markdown only."""

REPORT_TEMPLATE = """Write today's brief ({date}) using this structure:

# Daily News Brief: {date}

## Top Stories
The 5 most important stories. For each: a bold one-line headline, then 2-3 sentences on what happened and why it matters, then citations.

## Markets and Economy
Bullet points for business, markets and economic news not covered above.

## World and Politics
Bullet points for other notable world and US news.

## What to Watch
2-4 bullets on upcoming events or developing stories mentioned in the articles.

Articles:
{articles}"""


def format_articles(articles: list[Article], summary_chars: int = 300) -> str:
    lines = []
    for i, a in enumerate(articles, 1):
        lines.append(f"[{i}] {a.source}: {a.title}\n    {a.summary[:summary_chars]}")
    return "\n".join(lines)


def call_provider(p: dict, articles: list[Article], date_str: str) -> str:
    """Send one request to an OpenAI-compatible chat endpoint and return the text."""
    from openai import OpenAI  # imported here so --dry-run works without it

    client = OpenAI(
        base_url=p["base_url"],
        api_key=os.environ[p["api_key_env"]],
        timeout=p.get("timeout", 120),
        max_retries=2,  # retries rate-limit (429) and server errors with backoff
    )
    prompt = REPORT_TEMPLATE.format(
        date=date_str,
        articles=format_articles(articles, p.get("summary_chars", 300)),
    )
    resp = client.chat.completions.create(
        model=p["model"],
        max_tokens=p.get("max_tokens", 4000),
        temperature=0.3,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        extra_body=p.get("extra_body") or None,
    )
    text = (resp.choices[0].message.content or "").strip()
    if len(text) < 200:
        raise RuntimeError("empty or very short response")
    return text


def summarize(articles: list[Article], cfg: dict, date_str: str) -> tuple[str, list[Article], str]:
    """Try each configured provider in order. Returns (report, articles used, provider label)."""
    errors = []
    for p in cfg["llm"]["providers"]:
        label = f"{p['name']} ({p['model']})"
        if not os.getenv(p["api_key_env"]):
            print(f"  [skip] {label}: {p['api_key_env']} not set")
            continue
        used = articles[: p.get("max_articles", 60)]
        print(f"Summarizing {len(used)} articles with {label}...")
        try:
            return call_provider(p, used, date_str), used, label
        except Exception as e:  # rate limit, outage, bad model ID, etc.
            print(f"  [warn] {label} failed: {e}", file=sys.stderr)
            errors.append(f"{label}: {e}")
    raise SystemExit("All LLM providers failed or have no API key set:\n" + "\n".join(errors))


def add_source_list(report: str, articles: list[Article], provider: str) -> str:
    refs = "\n".join(f"{i}. [{a.source}: {a.title}]({a.url})" for i, a in enumerate(articles, 1))
    return f"{report}\n\n---\n\n## Sources\n\n{refs}\n\n_Summary generated by {provider}._\n"


# ---------------------------------------------------------------- delivery

def send_email(subject: str, markdown_body: str) -> None:
    import markdown as md

    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    to = os.getenv("EMAIL_TO", user)

    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.attach(MIMEText(markdown_body, "plain"))
    msg.attach(MIMEText(md.markdown(markdown_body), "html"))

    with smtplib.SMTP(host, port) as s:
        s.starttls()
        s.login(user, password)
        s.sendmail(user, [x.strip() for x in to.split(",")], msg.as_string())


# ---------------------------------------------------------------- main

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a daily news brief with a free LLM API.")
    parser.add_argument("--config", default=HERE / "config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="fetch only, do not call the LLM")
    parser.add_argument("--no-email", action="store_true")
    args = parser.parse_args()

    try:  # load .env for local runs; GitHub Actions uses repository secrets instead
        from dotenv import load_dotenv
        load_dotenv(HERE / ".env")
    except ImportError:
        pass

    cfg = yaml.safe_load(Path(args.config).read_text())
    date_str = datetime.now().strftime("%B %d, %Y")

    articles = collect(cfg)
    print(f"Fetched {len(articles)} recent articles")
    articles = topic_filter(dedupe(articles), cfg.get("topics") or [])
    print(f"{len(articles)} articles after dedupe and topic filter")

    if not articles:
        sys.exit("No articles found. Check your API keys and network access.")

    if args.dry_run:
        print("\n" + format_articles(articles))
        return

    text, used, provider = summarize(articles, cfg, date_str)
    report = add_source_list(text, used, provider)

    out_dir = HERE / "reports"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / f"{datetime.now():%Y-%m-%d}.md"
    out_file.write_text(report)
    print(f"Saved report to {out_file}")

    if cfg.get("email", {}).get("enabled") and not args.no_email:
        if os.getenv("SMTP_USER") and os.getenv("SMTP_PASSWORD"):
            send_email(f"{cfg['email'].get('subject_prefix', 'Daily News Brief')}: {date_str}", report)
            print("Email sent")
        else:
            print("[warn] Email skipped: SMTP_USER / SMTP_PASSWORD not set", file=sys.stderr)


if __name__ == "__main__":
    main()
