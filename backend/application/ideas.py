"""Batch-check a list of video ideas against the corpus (issue #3).

"Car wash, funeral home, laundromat, ..." -- twenty nouns pasted out of a
brainstorm, and the question for each is the same: has a competitor already
made this video, and did it work? Asking that one idea at a time through
search_outliers means re-reading the whole niche twenty times and eyeballing
twenty result lists; here the corpus is loaded once and every idea is scored
against it.

An idea is matched two ways, and both are reported (`matchedBy`):

  title      the phrase appears in the title, whole words -- exact, and the
             only thing that works when a video has no embedding yet
  semantic   cosine over the local embeddings -- catches "funeral home" in
             "what it costs to run a crematorium", which no substring will

The verdict itself is domain/idea_verdicts.py, so MCP, HTTP and the dashboard
cannot disagree about what "recent" means.

Zero YouTube quota: local database and local embeddings only.
"""
import re
import statistics as st

import infrastructure.postgres as db
from application import discovery as trends
from domain import idea_verdicts as V

MAX_IDEAS = 200
MAX_IDEA_LEN = 120
MIN_SIMILARITY = 0.55
EXAMPLES = 5

_WORDS = re.compile(r"[^\w]+", re.UNICODE)


def _normalise(text: str) -> str:
    """Lowercase, punctuation to spaces, single-spaced and padded.

    Padding lets a plain `in` test mean "whole words": " car wash " matches
    "Car Wash Business" but not "supercarwashing".
    """
    return " %s " % _WORDS.sub(" ", (text or "").lower()).strip()


def _clean(ideas) -> list:
    if isinstance(ideas, str):
        ideas = [ideas]
    if not isinstance(ideas, (list, tuple)) or not ideas:
        raise ValueError("ideas must be a non-empty list of strings")
    if len(ideas) > MAX_IDEAS:
        raise ValueError(f"at most {MAX_IDEAS} ideas per call, got {len(ideas)}")
    out, seen = [], set()
    for i, raw in enumerate(ideas):
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"ideas[{i}] must be a non-empty string")
        idea = raw.strip()
        if len(idea) > MAX_IDEA_LEN:
            raise ValueError(f"ideas[{i}] is longer than {MAX_IDEA_LEN} characters")
        key = _normalise(idea)
        if key in seen:      # the same idea twice would just duplicate a verdict
            continue
        seen.add(key)
        out.append(idea)
    return out


def _load_vectors(video_ids):
    """video_id -> embedding, for the videos that have one.

    A corpus with no embeddings at all is normal (collect_channel defaults to
    embed=False), and it must not drag the local model in: title matching
    still works, and the caller is told what it is missing.
    """
    import infrastructure.embeddings.fastembed_provider as emb

    vecs = {}
    conn = db.get_conn()
    ids = list(video_ids)
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        sql = ("SELECT video_id, embedding FROM videos WHERE video_id IN (%s)"
               % ",".join("?" * len(chunk)))
        for rec in conn.execute(sql, chunk).fetchall():
            if rec["embedding"]:
                vecs[rec["video_id"]] = emb.from_blob(rec["embedding"])
    conn.close()
    return vecs


def _example(r, similarity, matched_by) -> dict:
    return {
        "videoId": r["video_id"],
        "url": f"https://www.youtube.com/watch?v={r['video_id']}",
        "title": r["title"],
        "channelId": r["channel_id"],
        "channelTitle": r["channel_title"],
        "channelSubscribers": r["subs"],
        "publishedAt": r["published_at"],
        "ageDays": r["ageDays"],
        "views": r["view_count"],
        "outlierScore": r["outlierScore"],
        "similarity": similarity,
        "matchedBy": matched_by,
    }


def check_ideas(ideas, niche: str = None, period: str = "all",
                min_similarity: float = MIN_SIMILARITY,
                recent_days: float = V.RECENT_DAYS,
                proven_outlier: float = V.PROVEN_OUTLIER,
                fresh_days: float = V.FRESH_DAYS,
                exclude_shorts: bool = False, outlier_base: str = "rolling",
                examples: int = EXAMPLES, match_titles: bool = True) -> dict:
    """A verdict per idea: free / recent / proven / flopped.

    `period` defaults to "all" deliberately: the question is whether the idea
    was EVER covered, and a 30-day window would answer "free" for everything
    older than a month -- the exact mistake this tool exists to prevent.

    `niche` narrows the corpus to one niche's videos; without it every video
    in the database is a potential competitor, which is the right default for
    a brainstorm that has not been placed in a niche yet.
    """
    ideas = _clean(ideas)
    rows = trends.load_window(period=period, niche=niche,
                              exclude_shorts=exclude_shorts,
                              outlier_base=outlier_base)

    vecs = _load_vectors([r["video_id"] for r in rows]) if rows else {}
    sims = None
    if vecs:
        import infrastructure.embeddings.fastembed_provider as emb
        ids = list(vecs)
        matrix = [vecs[i] for i in ids]
        sims = {}
        for idea, row in zip(ideas, emb.cosine_matrix(emb.embed(ideas), matrix)):
            sims[idea] = dict(zip(ids, (round(float(x), 4) for x in row)))

    titles = {r["video_id"]: _normalise(r["title"]) for r in rows} if match_titles else {}

    results = []
    for idea in ideas:
        needle = _normalise(idea)
        by_sim = sims.get(idea, {}) if sims else {}
        matched = []
        for r in rows:
            vid = r["video_id"]
            similarity = by_sim.get(vid)
            in_title = match_titles and needle in titles.get(vid, "")
            semantic = similarity is not None and similarity >= min_similarity
            if not (in_title or semantic):
                continue
            matched.append((r, similarity,
                            "both" if in_title and semantic else
                            "title" if in_title else "semantic"))

        decision = V.decide(
            [{"video_id": r["video_id"], "age_days": r["ageDays"],
              "views": r["view_count"], "outlier": r["outlierScore"]}
             for r, _, _ in matched],
            recent_days=recent_days, proven_outlier=proven_outlier,
            fresh_days=fresh_days)

        # Newest first: recency is what the verdict turns on, so the first row
        # of the table is the one that produced it.
        matched.sort(key=lambda m: m[0]["published_at"] or "", reverse=True)
        views = [r["view_count"] or 0 for r, _, _ in matched]
        results.append({
            "idea": idea, **decision,
            "medianViews": int(st.median(views)) if views else None,
            "videos": [_example(r, s, how) for r, s, how in matched[:max(0, examples)]],
        })

    counts = {v: sum(1 for r in results if r["verdict"] == v) for v in V.VERDICTS}
    hint = None
    if not rows:
        hint = ("nothing collected for this filter -- every idea comes back 'free' "
                "because the corpus is empty, not because the field is")
    elif not vecs:
        hint = ("no video in this corpus has an embedding, so only exact title "
                "matches were checked -- run backfill_embeddings (free) to make "
                "the semantic half work")
    elif len(vecs) < len(rows) / 2:
        hint = (f"only {len(vecs)} of {len(rows)} videos are embedded -- the rest "
                "were checked by title alone; backfill_embeddings fills the gap")

    return {
        "ideas": results,
        "count": len(results),
        "counts": counts,
        "niche": niche,
        "period": period,
        "outlierBase": rows[0]["outlierBase"] if rows else outlier_base,
        "minSimilarity": min_similarity,
        "thresholds": {"recentDays": recent_days, "provenOutlier": proven_outlier,
                       "freshDays": fresh_days},
        "corpus": {"videos": len(rows), "embedded": len(vecs)},
        "hint": hint,
        "quota": 0,
    }
