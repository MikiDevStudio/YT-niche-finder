"""Export a niche as TSV, for the research notes in YT-analyze (issue #5).

One niche, one table: every video of it as a row, with the channel it came
from, both outlier baselines and whatever topic tags it carries. The rows are
the same ones the dashboard's scatter is drawn from (search.niche_videos), so
a number read off the chart and the same number in the spreadsheet cannot
disagree.

TSV rather than CSV because the destination is a `niches/<niche>/data/` file
that gets pasted into a spreadsheet and read as plain text in a diff: titles
are full of commas and quotes, and TSV needs no quoting rules for those. Tabs
and newlines inside a field would break that promise, so they are turned into
spaces -- see `_cell`.

Zero YouTube quota: everything below reads the local database only.
"""
from datetime import datetime, timezone

import infrastructure.postgres as db
from application import search as q

# Column order is the export's contract with YT-analyze; append, never reorder.
COLUMNS = ("channel", "handle", "subs", "video_id", "published_at", "views",
           "likes", "comments", "length_seconds", "is_short", "title",
           "outlierScoreRolling", "outlierScorePeriod", "tags")

SCORE_DIGITS = 3


def _cell(value) -> str:
    """One field, safe to sit between two tabs.

    None becomes an empty cell rather than "None": a spreadsheet reads the
    first as "not measured" and the second as text. Tabs and newlines inside
    a title (they happen) would otherwise shift every following column.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        value = round(value, SCORE_DIGITS)
    text = str(value)
    for ch in ("\t", "\r\n", "\r", "\n"):
        text = text.replace(ch, " ")
    return text.strip()


def _tags_cell(rows) -> str:
    """Every tag of one video in one cell: `group=tag1,tag2; other=tag3`.

    A column per group would change shape with the taxonomy, and the taxonomy
    is still moving; one text column survives that and stays greppable.
    """
    by_group = {}
    for r in rows:
        by_group.setdefault(r["tag_group"], []).append(r["tag"])
    return "; ".join(f"{g}={','.join(sorted(set(tags)))}"
                     for g, tags in sorted(by_group.items()))


def default_filename(ref=None) -> str:
    day = (ref or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    return f"videos_{day}.tsv"


def niche_tsv(niche: str, period: str = "all", channel_ids: list = None,
              exclude_shorts: bool = False, outlier_base: str = "rolling",
              outlier_threshold: float = 2.0) -> dict:
    """The niche as a TSV table, plus the filename it is meant to be saved as.

    `period` defaults to "all" on purpose: the export is the corpus of a
    niche, not a window into it, and a 30-day default would silently ship a
    third of the videos to a file that claims to be the niche.

    Both outlier baselines are always in the table (`outlier_base` only picks
    which one the row-level filtering thinks with), because the reader of the
    file has no way to recompute the missing one.
    """
    if not niche or not str(niche).strip():
        raise ValueError("niche is required")
    niche = str(niche).strip()

    data = q.niche_videos(niche=niche, period=period, channel_ids=channel_ids,
                          exclude_shorts=exclude_shorts,
                          outlier_base=outlier_base,
                          outlier_threshold=outlier_threshold)
    videos = data["videos"]
    channels = {c["channelId"]: c for c in data["channels"]}

    tags_by_video = {}
    if videos:
        conn = db.get_conn()
        for r in db.video_tags_for(conn, video_ids=[v["videoId"] for v in videos]):
            tags_by_video.setdefault(r["video_id"], []).append(r)
        conn.close()

    lines = ["\t".join(COLUMNS)]
    for v in videos:
        ch = channels.get(v["channelId"], {})
        lines.append("\t".join(_cell(x) for x in (
            ch.get("title"), ch.get("handle"), ch.get("subscribers"),
            v["videoId"], v["publishedAt"], v["views"], v["likes"],
            v["comments"], v["lengthSeconds"], v["isShort"], v["title"],
            v["outlierScoreRolling"], v["outlierScorePeriod"],
            _tags_cell(tags_by_video.get(v["videoId"], [])),
        )))

    return {
        "niche": niche, "found": data["found"], "period": period,
        "outlierBase": data["outlierBase"],
        "filename": default_filename(),
        "columns": list(COLUMNS),
        "rows": len(videos),
        "taggedRows": len(tags_by_video),
        "channels": data["channelCount"],
        "tsv": "\n".join(lines) + "\n",
        "hint": data["hint"],
        "quota": 0,
    }
