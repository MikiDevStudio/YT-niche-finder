"""Automatic topic tagging of new videos through OpenRouter (issue #7, stage 2).

Stage 1 (issue #2) made the breakdown of a niche into data a human types. This
is the part that keeps it up to date without a conversation: for videos that
carry no tag of a group yet, ask a cheap model which tags of the EXISTING
taxonomy apply, and write the answer with source="llm".

Three rules make that safe to run unattended:

  * the taxonomy is closed. The tags of a group are sent as a JSON-schema enum,
    so the model picks from what a human already established and cannot invent
    a synonym that splits the group in two. Anything it returns anyway is
    dropped here as well -- strict mode is enforced by the provider, and not
    every provider enforces it the same way.
  * it may add, never overwrite. tagging.tag_videos(source="llm") carries
    PROTECTED_BY, so a row written by a human or by a model in a conversation
    survives untouched. `replace` defaults to False for the same reason.
  * it is bounded. One run reads at most `limit` videos in batches of
    `batch_size`, and stops early on `max_cost_usd`. Every run reports tokens
    and dollars spent, because nothing else in this project costs money.

A group with fewer than `min_taxonomy` tagged videos is left alone: the point
of the examples in the prompt is to show how a human used the taxonomy, and
two examples show nothing. Tag by hand first, then turn this on.

Zero YouTube quota. Costs OpenRouter money -- see the `cost` field.
"""
import os

import infrastructure.postgres as db
from application import search as q
from application import tagging as TAG
from infrastructure.llm import openrouter as llm

BATCH_SIZE = 20
LIMIT = 200
MIN_TAXONOMY_VIDEOS = 5
MAX_TAGS_PER_VIDEO = 3
MAX_TOKENS = 4000
# Классификация по закрытому списку -- механическая работа, и на думающей
# модели она платит за размышления: у z-ai/glm-5.3-flash "low" выходит вдвое
# дешевле дефолта. "none" эта модель отвергает, рассуждения у неё обязательны.
REASONING_EFFORT = "low"

SYSTEM = (
    "You label YouTube videos with an existing topic taxonomy. "
    "You never invent a tag: you choose only from the list you are given. "
    "If none of the tags fits a video, return an empty list for it -- an empty "
    "list is a valid and useful answer, a wrong tag is not. "
    "Answer with JSON only."
)


def _env_flag(name: str, default: str = "0") -> bool:
    return (os.environ.get(name, default) or "").strip().lower() not in ("0", "", "false", "no")


def enabled() -> bool:
    """LLM_TAGGING is the master switch and is off by default: this is the only
    thing in the project that spends money, so it never starts by accident."""
    return _env_flag("LLM_TAGGING") and bool(llm.api_key())


def _schema(tags):
    return {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "description": "one entry per video you were given",
                "items": {
                    "type": "object",
                    "properties": {
                        "video_id": {"type": "string",
                                     "description": "the id exactly as given"},
                        "tags": {
                            "type": "array",
                            "description": "tags from the taxonomy that apply; "
                                           "empty if none does",
                            "items": {"type": "string", "enum": list(tags)},
                        },
                    },
                    "required": ["video_id", "tags"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["items"],
        "additionalProperties": False,
    }


def _prompt(group, taxonomy, examples, batch, max_tags):
    lines = [f'Taxonomy of group "{group}" (choose only from these tags):']
    lines += [f'  - {t["tag"]}  ({t["videos"]} videos already carry it)' for t in taxonomy]
    if examples:
        lines.append("")
        lines.append("How a human used it on this niche:")
        lines += [f'  "{e["title"]}" -> {", ".join(e["tags"])}' for e in examples]
    lines.append("")
    lines.append(f"Label these videos. At most {max_tags} tags each, "
                 "empty list when nothing fits:")
    lines += [f'  {v["videoId"]}  "{v["title"]}"' for v in batch]
    # The shape is spelled out here as well as in the JSON schema: the schema
    # turned out to be a hint rather than a rule on this provider (see
    # openrouter.parse_json), and a prompt that shows the shape costs ~30 tokens.
    lines.append("")
    lines.append('Answer exactly like this, with one entry per video and no '
                 'other text:')
    lines.append('{"items": [{"video_id": "abc123", "tags": ["tag-from-the-list"]}]}')
    return "\n".join(lines)


def _entries(data):
    """The model's answer as [{video_id, tags}], whatever shape it arrived in.

    The declared schema asks for {"items": [...]}, and a provider that enforces
    it returns exactly that. z-ai/glm-5.3-flash answered with a flat
    {video_id: [tags]} map instead, which is a perfectly reasonable reading of
    the task and useless to refuse over -- the tags are validated against the
    taxonomy either way.
    """
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return [e for e in data["items"] if isinstance(e, dict)]
    if isinstance(data, list):
        return [e for e in data if isinstance(e, dict)]
    if isinstance(data, dict):
        return [{"video_id": k, "tags": v if isinstance(v, list) else [v]}
                for k, v in data.items()]
    return []


def _taxonomy(conn, niche, tag_group):
    """Existing tags of one group in one niche, and which video carries what."""
    rows = db.video_tags_for(conn, niche=niche, tag_group=tag_group)
    by_tag, by_video = {}, {}
    for r in rows:
        by_tag.setdefault(r["tag"], set()).add(r["video_id"])
        by_video.setdefault(r["video_id"], []).append(r["tag"])
    taxonomy = sorted(({"tag": t, "videos": len(v)} for t, v in by_tag.items()),
                      key=lambda x: (-x["videos"], x["tag"]))
    return taxonomy, by_video


def tag_new_videos(niche: str, tag_group: str, limit: int = LIMIT,
                   batch_size: int = BATCH_SIZE, model: str = None,
                   max_tags_per_video: int = MAX_TAGS_PER_VIDEO,
                   min_taxonomy: int = MIN_TAXONOMY_VIDEOS,
                   max_cost_usd: float = None, examples: int = 8,
                   dry_run: bool = False, replace: bool = False,
                   reasoning_effort: str = REASONING_EFFORT) -> dict:
    """Tag the videos of `niche` that carry no tag of `tag_group` yet.

    Newest first: the worker's job is the videos that arrived since last time,
    and a run that is cut short by `limit` should have spent its budget on
    those rather than on the back catalogue.
    """
    if not niche or not tag_group:
        raise ValueError("niche and tag_group are required")
    tag_group = tag_group.strip().lower()

    conn = db.get_conn()
    taxonomy, tagged_by_video = _taxonomy(conn, niche, tag_group)
    conn.close()

    tagged_videos = len(tagged_by_video)
    base = {"niche": niche, "group": tag_group, "model": model or llm.model_name(),
            "taxonomy": [t["tag"] for t in taxonomy], "taggedAlready": tagged_videos,
            "candidates": 0, "labelled": 0, "written": 0, "skipped": 0,
            "emptyAnswers": 0, "batches": 0, "dryRun": bool(dry_run),
            "usage": {"promptTokens": 0, "completionTokens": 0}, "costUsd": 0.0,
            "quota": 0}

    if tagged_videos < min_taxonomy:
        return {**base, "hint": (
            f"group '{tag_group}' has only {tagged_videos} tagged videos in "
            f"'{niche}' -- tag at least {min_taxonomy} by hand first, otherwise "
            "there is nothing for the model to imitate")}

    data = q.niche_videos(niche=niche, period="all")
    if not data["videos"]:
        return {**base, "hint": data.get("hint") or "no videos in this niche"}

    candidates = [v for v in data["videos"] if v["videoId"] not in tagged_by_video]
    candidates.sort(key=lambda v: v["publishedAt"] or "", reverse=True)
    candidates = candidates[:max(0, limit)]
    base["candidates"] = len(candidates)
    if not candidates:
        return {**base, "hint": f"every video of '{niche}' already carries a tag "
                                f"of group '{tag_group}'"}

    by_title = {v["videoId"]: v for v in data["videos"]}
    shots = [{"title": by_title[vid]["title"], "tags": tags}
             for vid, tags in list(tagged_by_video.items())[:max(0, examples)]
             if vid in by_title]

    allowed = {t["tag"] for t in taxonomy}
    schema = _schema(allowed)
    items, stopped = [], None

    for start in range(0, len(candidates), max(1, batch_size)):
        batch = candidates[start:start + max(1, batch_size)]
        res = llm.chat_json(
            [{"role": "system", "content": SYSTEM},
             {"role": "user", "content": _prompt(tag_group, taxonomy, shots, batch,
                                                 max_tags_per_video)}],
            schema, schema_name="video_tags", model=model, max_tokens=MAX_TOKENS,
            reasoning_effort=reasoning_effort)
        base["batches"] += 1
        base["costUsd"] = round(base["costUsd"] + res["costUsd"], 6)
        for field, key in (("promptTokens", "promptTokens"),
                           ("completionTokens", "completionTokens")):
            base["usage"][field] += res["usage"].get(key) or 0

        batch_ids = {v["videoId"] for v in batch}
        for entry in _entries(res["data"]):
            video_id = entry.get("video_id")
            if video_id not in batch_ids:
                continue          # a video it was not asked about
            tags = [t for t in (entry.get("tags") or []) if t in allowed]
            if not tags:
                base["emptyAnswers"] += 1
                continue
            for tag in tags[:max_tags_per_video]:
                items.append({"video_id": video_id, "tag_group": tag_group, "tag": tag})

        if max_cost_usd is not None and base["costUsd"] >= max_cost_usd:
            stopped = (f"stopped after {base['batches']} batches: spent "
                       f"${base['costUsd']:.4f} of the ${max_cost_usd:.4f} budget")
            break

    base["labelled"] = len({i["video_id"] for i in items})
    if items and not dry_run:
        res = TAG.tag_videos(items, source="llm", replace=replace)
        base["written"], base["skipped"] = res["written"], res["skipped"]

    hint = stopped
    if not items and not hint:
        hint = ("the model matched none of the candidates to a tag -- either the "
                "taxonomy does not cover them, or the group is too narrow")
    return {**base, "hint": hint}


def tag_niche(niche: str, tag_group: str = None, **kwargs) -> dict:
    """Every group of a niche at once, or just one.

    Without `tag_group` this walks the groups that already exist in the niche:
    the model can extend a taxonomy, never start one.
    """
    if tag_group:
        return {"niche": niche, "groups": [tag_new_videos(niche, tag_group, **kwargs)]}

    conn = db.get_conn()
    groups = sorted({r["tag_group"] for r in db.video_tags_for(conn, niche=niche)})
    conn.close()
    if not groups:
        return {"niche": niche, "groups": [],
                "hint": f"no tag group exists in '{niche}' yet -- the automatic "
                        "tagger extends a taxonomy, it does not invent one"}
    return {"niche": niche,
            "groups": [tag_new_videos(niche, g, **kwargs) for g in groups]}
