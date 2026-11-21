import html
import random
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import click
import feedparser
import httpx
from feedgen.feed import FeedGenerator

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; PersonalFeedAggregator/1.0)",
    "Accept": "application/atom+xml, application/xml, text/xml",
}


def get_feed_url(channel_id: str, exclude_shorts: bool) -> str:
    target_id = channel_id.strip()
    if exclude_shorts and target_id.startswith("UC"):
        target_id = f"UULF{target_id[2:]}"

    param = "playlist_id" if target_id.startswith(("UU", "PL")) else "channel_id"
    return f"https://www.youtube.com/feeds/videos.xml?{param}={target_id}"


def fetch_with_retry(client: httpx.Client, url: str, retries: int = 3) -> str | None:
    for attempt in range(1, retries + 1):
        try:
            resp = client.get(url)
            if resp.status_code == 200:
                return resp.text
            if resp.status_code in {404, 429, 500, 502, 503, 504}:
                wait = attempt * random.uniform(3.0, 6.0)  # nosec
                click.echo(
                    f"HTTP {resp.status_code} for {url} "
                    f"(attempt {attempt}/{retries}), retrying in {wait:.1f}s"
                )
                time.sleep(wait)
                continue
            click.echo(f"Non-retryable HTTP {resp.status_code} for {url}", err=True)
            return None
        except httpx.RequestError as exc:
            wait = attempt * 3.0
            click.echo(
                f"Request error for {url}: {exc} "
                f"(attempt {attempt}/{retries}), retrying in {wait:.1f}s"
            )
            time.sleep(wait)
    return None


def parse_entries(xml_text: str, fallback_name: str | None = None) -> list[dict]:
    parsed = feedparser.parse(xml_text)
    channel_title = parsed.feed.get("title") or fallback_name or "Unknown Channel"
    items = []

    for entry in parsed.entries:
        video_id = entry.get("yt_videoid") or entry.get("id", "")
        published_parsed = entry.get("published_parsed")
        if published_parsed:
            dt = datetime(*published_parsed[:6], tzinfo=UTC)
        else:
            dt = datetime.now(UTC)

        thumbnail = ""
        if "media_thumbnail" in entry and entry.media_thumbnail:
            thumbnail = entry.media_thumbnail[0].get("url", "")

        items.append(
            {
                "id": video_id,
                "channel": channel_title,
                "title": f"[{channel_title}] {entry.get('title', 'Untitled')}",
                "link": entry.get("link", ""),
                "published": dt.isoformat(),
                "summary": entry.get("summary", ""),
                "thumbnail": thumbnail,
            }
        )
    return items


def build_rss(all_items: list[dict], feed_meta: dict, output_path: Path) -> None:
    max_items = feed_meta.get("max_items", 25)

    fg = FeedGenerator()
    fg.id(feed_meta.get("id", "https://github.com/your-username/youtube-rss"))
    fg.title(feed_meta.get("title", "Favourite YouTube Channels"))
    fg.link(href="https://www.youtube.com", rel="alternate")
    fg.description(feed_meta.get("description", "Aggregated YouTube channel feed"))
    fg.language(feed_meta.get("language", "en"))

    sorted_items = sorted(all_items, key=lambda x: x["published"])[:max_items]

    for item in sorted_items:
        fe = fg.add_entry()
        fe.id(item["link"] or item["id"])
        fe.title(item["title"])
        fe.link(href=item["link"])
        fe.published(datetime.fromisoformat(item["published"]))

        description_parts = []
        video_id = item.get("id", "")

        if video_id:
            embed_url = (
                f"https://www.youtube-nocookie.com/embed/{html.escape(video_id)}"
            )
            description_parts.append(
                f'<p><iframe width="560" height="315" src="{embed_url}" '
                f'title="{html.escape(item["title"])}" frameborder="0" '
                f'allow="accelerometer; autoplay; clipboard-write; encrypted-media; '
                f'gyroscope; picture-in-picture; web-share" '
                f'referrerpolicy="strict-origin-when-cross-origin" allowfullscreen></iframe></p>'
            )
        elif item["thumbnail"]:
            description_parts.append(
                f'<p><a href="{html.escape(item["link"])}">'
                f'<img src="{html.escape(item["thumbnail"])}" alt="Thumbnail" /></a></p>'
            )

        if item["summary"]:
            escaped_summary = html.escape(item["summary"]).replace("\n", "<br />\n")
            description_parts.append(f"<p>{escaped_summary}</p>")

        html_body = "\n".join(description_parts)
        fe.description(html_body)
        fe.content(html_body, type="CDATA")

    fg.rss_file(str(output_path), pretty=True)


@click.command()
@click.argument(
    "config",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to the TOML channel configuration file.",
)
@click.argument(
    "output",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Output path for the generated RSS XML file.",
)
def main(config: Path, output: Path) -> None:
    """Aggregate YouTube channel feeds defined in a TOML file into a single RSS feed."""
    config = tomllib.loads(config.read_text(encoding="utf-8"))
    feed_meta = config.get("feed", {})
    channels = config.get("channels", [])

    all_items: list[dict] = []

    with httpx.Client(headers=HEADERS, timeout=15.0, follow_redirects=True) as client:
        for channel in channels:
            channel_id = channel["id"]
            label = channel.get("name", channel_id)
            exclude_shorts = channel.get("exclude_shorts", True)

            time.sleep(random.uniform(1.5, 3.0))  # nosec

            url = get_feed_url(channel_id, exclude_shorts)
            xml_text = fetch_with_retry(client, url)
            if xml_text:
                entries = parse_entries(xml_text, fallback_name=label)
                all_items.extend(entries)
                click.echo(f"Fetched {len(entries)} entries for {label}")
            else:
                click.echo(f"Failed to fetch entries for {label}", err=True)

    if not all_items:
        raise click.ClickException(
            "No entries fetched from any channel; aborting write."
        )

    build_rss(all_items, feed_meta, output)
    click.echo(
        f"Wrote {output} with {min(len(all_items), feed_meta.get('max_items', 50))} items."
    )


if __name__ == "__main__":
    main()
