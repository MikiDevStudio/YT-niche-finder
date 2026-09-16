"""Hit rate of a topic tag: does this group of videos actually break out?

The formula is deliberately the same one domain/keywords.py uses for phrases:

  hitRate  share of the tag's videos that are outliers
  lift     hitRate / the same share across the whole sample

so that two numbers that look alike on one screen mean the same thing. What
differs is the unit: keywords derives its phrases from titles, here the tag
was put on the video by a human or a model (see application/tagging.py).

Videos younger than `fresh_days` are dropped from BOTH the numerator and the
denominator by default. They have not finished collecting views, so counting
them makes a freshly explored topic look like a failure; their number is
reported separately instead of being silently absorbed.

Pure: no database, no config, no clock -- callers pass rows that already
carry views, an outlier score and a freshness flag.
"""
import statistics as st

SORTS = {
    "hit_rate": lambda x: (x["hitRate"], x["videos"]),
    "lift": lambda x: (x["lift"] or 0, x["videos"]),
    "videos": lambda x: (x["videos"], x["hitRate"]),
    "median_views": lambda x: (x["medianViews"], x["videos"]),
    "median_outlier": lambda x: (x["medianOutlier"] or 0, x["videos"]),
}


def compute(rows, tags_by_video, outlier_threshold: float = 2.0,
            include_fresh: bool = False, min_videos: int = 1,
            sort_by: str = "hit_rate", top_n: int = 100) -> dict:
    """rows: dicts with video_id / title / views / outlier / fresh.
    tags_by_video: video_id -> iterable of tags of ONE group (a video may
    carry several, e.g. three triggers -- each counts once for its own tag).

    Returns the per-tag list plus the baseline the lift is measured against,
    so a caller can show "18% across the niche" next to "41% for this tag"
    instead of an unanchored multiplier.
    """
    fresh_total = 0
    sample = []
    for r in rows:
        if r.get("fresh"):
            fresh_total += 1
            if not include_fresh:
                continue
        sample.append(r)

    stats = {}
    total_hits = 0
    untagged = 0
    for r in sample:
        score = r.get("outlier")
        is_hit = score is not None and score >= outlier_threshold
        total_hits += 1 if is_hit else 0
        tags = {t for t in (tags_by_video.get(r["video_id"]) or ()) if t}
        if not tags:
            untagged += 1
            continue
        for tag in tags:
            s = stats.setdefault(tag, {"videos": 0, "hits": 0, "views": [],
                                       "outliers": [], "examples": []})
            s["videos"] += 1
            s["hits"] += 1 if is_hit else 0
            s["views"].append(r.get("views") or 0)
            if score is not None:
                s["outliers"].append(score)
            s["examples"].append({"videoId": r["video_id"], "title": r.get("title"),
                                  "views": r.get("views"), "outlierScore": score})

    total = len(sample)
    base_rate = (total_hits / total) if total else 0.0

    out = []
    for tag, s in stats.items():
        if s["videos"] < min_videos:
            continue
        hit_rate = s["hits"] / s["videos"]
        out.append({
            "tag": tag,
            "videos": s["videos"],
            "hits": s["hits"],
            "hitRate": round(hit_rate, 4),
            # A niche where nothing at all broke out has no baseline to
            # compare against: None, not a division by zero dressed up as 0.
            "lift": round(hit_rate / base_rate, 2) if base_rate > 0 else None,
            "medianViews": int(st.median(s["views"])) if s["views"] else 0,
            "medianOutlier": round(st.median(s["outliers"]), 2) if s["outliers"] else None,
            "examples": sorted(s["examples"],
                               key=lambda e: (e["outlierScore"] or 0, e["views"] or 0),
                               reverse=True)[:3],
        })

    out.sort(key=SORTS.get(sort_by, SORTS["hit_rate"]), reverse=True)

    return {
        "tags": out[:top_n],
        "videos": total,
        "hits": total_hits,
        "baseRate": round(base_rate, 4),
        "untagged": untagged,
        # how many young videos there are either way, and how many of them
        # this call actually left out -- the dashboard shows the first, the
        # hint explains the second.
        "fresh": fresh_total,
        "freshExcluded": 0 if include_fresh else fresh_total,
        "outlierThreshold": outlier_threshold,
    }
