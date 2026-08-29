#!/usr/bin/env python3
"""Headline gathering via RSS — free, keyless, no quota.

Exists because Gemini's free tier has no Google Search grounding quota (429
RESOURCE_EXHAUSTED on every grounded call), so the model cannot see today's news
by itself. Fetching headlines here and passing them in restores the intended
split anyway: deterministic gathering, model judgment.

Runs on GitHub Actions, which has open outbound internet. The Claude container
cannot reach these hosts, so this is verified by the workflow, not locally.
"""
import html
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 20
GNEWS = ("https://news.google.com/rss/search?q=%s&hl=en-US&gl=US&ceid=US:en")


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


def _strip(s):
    """Order matters twice here.

    CDATA is unwrapped first: `<![CDATA[title]]>` has no `>` until the very end,
    so a tag-stripping pass would eat the entire title and silently drop the
    item. Google News wraps most titles this way.

    Then unescape before stripping tags, because feeds often carry
    entity-encoded markup (&lt;b&gt;) that survives as literal angle brackets if
    stripped first.
    """
    text = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", s or "", flags=re.S)
    text = re.sub(r"<[^>]+>", "", html.unescape(text))
    return re.sub(r"\s+", " ", text).strip()


def parse_rss(xml):
    """Minimal RSS reader. Regex rather than a parser because feeds are often
    slightly malformed and a strict parser throws away the whole document."""
    out = []
    for block in re.findall(r"<item>(.*?)</item>", xml, re.S):
        def field(tag):
            m = re.search(r"<%s[^>]*>(.*?)</%s>" % (tag, tag), block, re.S)
            return _strip(m.group(1)) if m else ""
        title = field("title")
        if not title:
            continue
        out.append({
            "title": title,
            "source": field("source") or field("author") or "",
            "url": field("link"),
            "published": field("pubDate"),
        })
    return out


def _query(q):
    try:
        return parse_rss(_get(GNEWS % urllib.parse.quote(q)))
    except Exception as e:                                        # noqa: BLE001
        print("  feed failed (%s): %s" % (q[:40], type(e).__name__))
        return []


def gather(focus_tickers, extra_queries=None, per_query=12):
    """Headlines across the focus names plus market-wide queries."""
    names = " OR ".join(focus_tickers[:12])
    queries = [
        "%s stock when:2d" % names,
        "Nvidia OR AMD OR Micron OR Intel earnings guidance when:2d",
        "chip export restrictions OR semiconductor policy when:2d",
        "stock market selloff OR rally OR Fed OR CPI OR jobs report when:1d",
        "upgrade OR downgrade price target megacap tech when:2d",
    ]
    queries += extra_queries or []
    with ThreadPoolExecutor(max_workers=5) as ex:
        batches = list(ex.map(_query, queries))

    seen, items = set(), []
    for batch in batches:
        for it in batch[:per_query]:
            key = re.sub(r"[^a-z0-9]+", "", it["title"].lower())[:70]
            if key and key not in seen:
                seen.add(key)
                items.append(it)
    print("gathered %d unique headlines from %d feeds"
          % (len(items), len(queries)))
    return items


def as_prompt_block(items, limit=70):
    lines = []
    for i, it in enumerate(items[:limit], 1):
        src = (" [%s]" % it["source"]) if it["source"] else ""
        lines.append("%d. %s%s\n   %s" % (i, it["title"], src, it["url"]))
    return "\n".join(lines)
