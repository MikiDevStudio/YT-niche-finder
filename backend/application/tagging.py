"""Topic tags on videos, and the hit rate of each tag (issue #2).

A niche breakdown normally lives in a chat message and dies there. Tagging
turns it into data: which topic group a video belongs to, which triggers it
pulls -- and then, mechanically, which of those groups actually break out.

Three sources share the table and are not equal:

  manual      typed in the dashboard by a human
  claude-mcp  put there by a model in a conversation, via tag_videos
  llm         written by the automatic tagger (issue #7)

`llm` may never overwrite the other two -- see PROTECTED_BY. That rule lives
here rather than in the caller so that MCP, HTTP and the future worker cannot
each have their own version of it.

The video rows themselves come from search.niche_videos, so the definition of
an outlier is the service-wide one and no second implementation appears here.
Zero YouTube quota: everything below reads and writes only the local database.
"""
import infrastructure.postgres as db
from application import search as q
from domain import tag_stats as TS

VALID_SOURCES = ("manual", "claude-mcp", "llm")

# source -> the sources it must not overwrite
PROTECTED_BY = {"llm": ("manual", "claude-mcp")}

MAX_ITEMS = 2000
MAX_LEN = 64


def _norm(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required and must be a non-empty string")
    out = value.strip().lower()
    if len(out) > MAX_LEN:
        raise ValueError(f"{field} is longer than {MAX_LEN} characters: {out[:20]}...")
    return out


def _clean(items) -> list:
    """Validate and normalise {video_id, tag_group, tag} triples.

    Tags and groups are lowercased: "Группа А" and "группа а" are the same
    tag, and a taxonomy that splits on capitalisation is worse than useless.
    video_id keeps its case -- YouTube ids are case-sensitive.
    """
    if not isinstance(items, (list, tuple)) or not items:
        raise ValueError("items must be a non-empty list of "
                         "{video_id, tag_group, tag} objects")
    if len(items) > MAX_ITEMS:
        raise ValueError(f"at most {MAX_ITEMS} items per call, got {len(items)}")
    out = []
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            raise ValueError(f"items[{i}] must be an object, got {type(it).__name__}")
        video_id = it.get("video_id") or it.get("videoId")
        group = it.get("tag_group") or it.get("group") or it.get("tagGroup")
        tag = it.get("tag")
        if not isinstance(video_id, str) or not video_id.strip():
            raise ValueError(f"items[{i}].video_id is required")
        out.append({"video_id": video_id.strip(),
                    "tag_group": _norm(group, f"items[{i}].tag_group"),
                    "tag": _norm(tag, f"items[{i}].tag")})
    return out


def _shape(r) -> dict:
    return {"videoId": r["video_id"], "group": r["tag_group"], "tag": r["tag"],
            "source": r["source"], "createdAt": r["created_at"]}


def tag_videos(items, source: str = "claude-mcp", replace: bool = False) -> dict:
    """Put tags on videos. Idempotent: the same call twice changes nothing.

    replace=True clears the other tags of the touched (video, group) pairs
    first, which is what single-valued groups ("topic group A or B, not both")
    need; multi-valued groups like triggers leave it off.
    """
    if source not in VALID_SOURCES:
        raise ValueError(f"source must be one of {VALID_SOURCES}, got {source!r}")
    rows = _clean(items)
    conn = db.get_conn()
    res = db.upsert_video_tags(conn, rows, source=source, replace=replace,
                               protect_sources=PROTECTED_BY.get(source, ()))
    conn.commit()
    conn.close()
    groups = sorted({r["tag_group"] for r in rows})
    return {**res, "videos": len({r["video_id"] for r in rows}),
            "groups": groups, "source": source, "replace": bool(replace),
            "quota": 0}


def untag_videos(items) -> dict:
    """Remove the listed {video_id, tag_group, tag} rows. Tags only -- videos
    and their stats are never touched."""
    rows = _clean(items)
    conn = db.get_conn()
    res = db.delete_video_tags(conn, rows)
    conn.commit()
    conn.close()
    return {**res, "quota": 0}


def list_video_tags(niche: str = None, video_id: str = None,
                    tag_group: str = None) -> dict:
    """Every tag of a niche, of one video, or of one group -- plus the group
    summary the dashboard's selector and the chips are built from."""
    conn = db.get_conn()
    rows = db.video_tags_for(conn, video_ids=[video_id] if video_id else None,
                             niche=niche, tag_group=tag_group)
    conn.close()

    counts, videos_per_group = {}, {}
    for r in rows:
        counts.setdefault(r["tag_group"], {}).setdefault(r["tag"], set()).add(r["video_id"])
        videos_per_group.setdefault(r["tag_group"], set()).add(r["video_id"])
    groups = [{
        "group": g,
        "videos": len(videos_per_group[g]),
        "tags": sorted(({"tag": t, "videos": len(v)} for t, v in tags.items()),
                       key=lambda x: (-x["videos"], x["tag"])),
    } for g, tags in sorted(counts.items())]

    return {"niche": niche, "videoId": video_id, "group": tag_group,
            "count": len(rows), "groups": groups,
            "items": [_shape(r) for r in rows]}


def tag_stats(niche: str, tag_group: str, outlier_base: str = "rolling",
              outlier_threshold: float = 2.0, period: str = "all",
              include_fresh: bool = False, min_videos: int = 1,
              sort_by: str = "hit_rate", top_n: int = 100) -> dict:
    """Hit rate per tag of one group: how often videos carrying it break out,
    against the same share across the whole niche (lift).

    Videos younger than 30 days are left out of both sides of the fraction by
    default -- they are still collecting views. include_fresh=True puts them
    back; `freshExcluded` says how many that was either way.
    """
    if not niche or not tag_group:
        raise ValueError("niche and tag_group are required")
    tag_group = _norm(tag_group, "tag_group")

    data = q.niche_videos(niche=niche, period=period, outlier_base=outlier_base,
                          outlier_threshold=outlier_threshold)
    videos = data["videos"]

    conn = db.get_conn()
    rows = db.video_tags_for(conn, video_ids=[v["videoId"] for v in videos],
                             tag_group=tag_group)
    conn.close()
    tags_by_video = {}
    for r in rows:
        tags_by_video.setdefault(r["video_id"], []).append(r["tag"])

    res = TS.compute(
        [{"video_id": v["videoId"], "title": v["title"], "views": v["views"],
          "outlier": v["outlierScore"], "fresh": v["isFresh"]} for v in videos],
        tags_by_video, outlier_threshold=outlier_threshold,
        include_fresh=include_fresh, min_videos=min_videos,
        sort_by=sort_by, top_n=top_n)

    hint = None
    if not videos:
        hint = data.get("hint")
    elif not rows:
        hint = (f"no video of '{niche}' carries a tag of group '{tag_group}' -- "
                "check the group name with list_video_tags, or tag the videos first")
    elif not res["tags"]:
        hint = (f"every tag of '{tag_group}' is carried by fewer than "
                f"min_videos={min_videos} videos"
                if min_videos > 1 else
                f"every tagged video is younger than 30 days ({res['freshExcluded']} "
                "excluded as fresh) -- pass include_fresh=True to count them anyway")

    return {"niche": niche, "group": tag_group, "period": period,
            "outlierBase": data["outlierBase"], **res,
            "includeFresh": bool(include_fresh), "hint": hint, "quota": 0}
