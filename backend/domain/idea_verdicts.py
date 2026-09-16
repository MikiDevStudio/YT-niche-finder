"""The verdict on one video idea: has a competitor already covered it, and how
did that go? (issue #3)

Four answers, in the order the rule checks them:

  free      nobody in the corpus covered it -- the idea is open
  recent    covered within `recent_days` -- skip it, that is a head-on collision
  proven    covered long ago AND it broke out -- demand is proven, saturation
            is the risk, not indifference
  flopped   covered long ago and it did not break out

`recent` comes before the performance question on purpose: a video that came
out last month competes with yours whether it flopped or not, and its own
numbers are not final yet anyway.

Performance is judged only on videos older than `fresh_days` -- the same rule
domain/tag_stats.py uses for hit rate, and for the same reason: a video still
collecting views cannot be called a flop yet.

There is no fifth verdict for "covered, did okay". The threshold between
proven and flopped is one parameter, `proven_outlier`, and every number the
call was made on travels with the answer, so a reader who wants a middle band
can draw it themselves.

Pure: no database, no clock, no config -- the caller passes rows that already
carry views, an outlier score and an age.
"""
VERDICTS = ("free", "recent", "proven", "flopped")

# Defaults live here rather than in the use case so that MCP, HTTP and the
# dashboard cannot each drift to their own idea of "recent".
RECENT_DAYS = 90.0
PROVEN_OUTLIER = 2.0
FRESH_DAYS = 30.0


def decide(matches, recent_days: float = RECENT_DAYS,
           proven_outlier: float = PROVEN_OUTLIER,
           fresh_days: float = FRESH_DAYS) -> dict:
    """matches: dicts with `age_days`, `views` and `outlier` (may be None).

    Returns the verdict, the numbers behind it and a one-line `why`, which is
    what a conversation quotes back and the dashboard puts under the table.
    """
    if not matches:
        return {"verdict": "free", "why": "no video in the corpus covers this idea",
                "matches": 0, "daysSinceLastCoverage": None,
                "bestOutlier": None, "bestOutlierVideoId": None,
                "maxViews": None, "freshMatches": 0}

    ages = [m["age_days"] for m in matches if m.get("age_days") is not None]
    days_since_last = min(ages) if ages else None
    fresh = [m for m in matches if (m.get("age_days") or 0) < fresh_days]

    # Only settled videos may earn "proven" or "flopped"; a fresh one is still
    # counting views. If every match is fresh, the recency branch below owns
    # the answer anyway -- unless the caller pushed recent_days under
    # fresh_days, and then judging on what exists beats refusing to answer.
    mature = [m for m in matches if (m.get("age_days") or 0) >= fresh_days] or matches
    scored = [m for m in mature if m.get("outlier") is not None]
    best = max(scored, key=lambda m: m["outlier"]) if scored else None
    max_views = max((m.get("views") or 0) for m in matches)

    out = {
        "matches": len(matches),
        "daysSinceLastCoverage": round(days_since_last, 1) if days_since_last is not None else None,
        "bestOutlier": round(best["outlier"], 2) if best else None,
        "bestOutlierVideoId": best.get("video_id") if best else None,
        "maxViews": max_views,
        "freshMatches": len(fresh),
    }

    if days_since_last is not None and days_since_last <= recent_days:
        return {**out, "verdict": "recent",
                "why": f"covered {out['daysSinceLastCoverage']} days ago, "
                       f"inside the {recent_days:g}-day window"}
    if best and best["outlier"] >= proven_outlier:
        return {**out, "verdict": "proven",
                "why": f"covered {out['daysSinceLastCoverage']} days ago and it broke out "
                       f"({out['bestOutlier']}x, threshold {proven_outlier:g}x)"}
    if not scored:
        # Covered long ago, but no baseline to judge by -- a channel with too
        # few uploads to have a median. Not a flop, just unmeasured.
        return {**out, "verdict": "flopped",
                "why": "covered long ago; no outlier score on those videos, "
                       "so nothing says it worked"}
    return {**out, "verdict": "flopped",
            "why": f"covered {out['daysSinceLastCoverage']} days ago and the best of them "
                   f"reached only {out['bestOutlier']}x (threshold {proven_outlier:g}x)"}
