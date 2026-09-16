"""Маршруты HTTP API дашборда (interfaces/http/api.py) поверх того же use case,
что и MCP-тулы.

Тот же одноразовый-схемный сетап, что и в остальных тестах (tests/schema_scope.py):
сеть и ключ YouTube не нужны -- данные кладём прямо в базу.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import schema_scope  # noqa: F401,E402

from fastapi.testclient import TestClient      # noqa: E402

import infrastructure.postgres as db           # noqa: E402
import interfaces.http.api as api              # noqa: E402

client = TestClient(api.app)

NICHE = "http-preset-niche"
BIG_CHANNEL = "UC" + "httpbigchannel".ljust(22, "0")


def setup_module(_=None):
    """Крупный конкурент ниши: 74.6K подписчиков -- дефолтные фильтры раздела
    viral (max_subscribers=10000) его не пропускают."""
    conn = db.get_conn()
    db.upsert_channel(conn, {
        "channel_id": BIG_CHANNEL, "title": "Big Competitor", "description": "",
        "subscriber_count": 74600, "video_count": 120, "view_count": 9000000,
        "hidden_subs": 0,
    })
    db.upsert_video(conn, {
        "video_id": "vhttpbig1", "channel_id": BIG_CHANNEL, "title": "Car Wash",
        "description": "", "published_at": "2026-07-01T00:00:00Z",
        "duration_seconds": 900, "view_count": 55000, "like_count": 100,
        "comment_count": 10, "tags": "[]", "default_language": "en",
        "updated_at": "2026-07-02T00:00:00Z", "is_short": 0,
    })
    db.link_video_niche(conn, "vhttpbig1", NICHE)
    db.upsert_niche(conn, NICHE, None, NICHE)
    conn.commit()
    conn.close()


def _viral(**params):
    api._rate_hits.clear()
    return client.get("/api/viral", params={"period": "all", **params})


def test_viral_niche_all_preset_lifts_the_small_channel_filters():
    small = _viral(niche=NICHE)
    assert small.status_code == 200
    assert small.json()["matched"] == 0

    full = _viral(niche=NICHE, preset="niche_all")
    assert full.status_code == 200
    body = full.json()
    assert body["preset"] == "niche_all"
    assert [r["videoId"] for r in body["results"]] == ["vhttpbig1"]


def test_viral_niche_all_without_niche_is_a_400():
    resp = _viral(preset="niche_all")
    assert resp.status_code == 400
    assert "niche" in resp.json()["detail"]


def test_viral_unknown_preset_is_a_400():
    assert _viral(niche=NICHE, preset="everything").status_code == 400


def test_outlier_base_is_passed_through_and_validated():
    api._rate_hits.clear()
    resp = client.get("/api/search", params={"niche": NICHE, "outlier_base": "period"})
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert results and all(r["outlierBase"] == "period" for r in results)
    assert {"outlierScoreRolling", "outlierScorePeriod"} <= set(results[0])

    for path in ("/api/search", "/api/overview", f"/api/niches/{NICHE}",
                 f"/api/niches/{NICHE}/videos"):
        assert client.get(path, params={"outlier_base": "mean"}).status_code == 400, path


def test_niche_videos_route_returns_points_and_filters_by_channel():
    api._rate_hits.clear()
    body = client.get(f"/api/niches/{NICHE}/videos").json()
    assert body["found"] and body["videoCount"] == 1
    assert body["channels"][0]["channelId"] == BIG_CHANNEL
    point = body["videos"][0]
    assert {"publishedAt", "views", "lengthSeconds", "isOutlier", "isFresh"} <= set(point)

    other = client.get(f"/api/niches/{NICHE}/videos", params={"channels": "UCnobody, UCnoone"})
    assert other.json()["videoCount"] == 0


def test_overview_viral_counts_by_publication_date():
    """«Вирусные за 30 дней» — то, что вышло за 30 дней, а не ролики многолетней
    давности, впервые собранные на этой неделе."""
    api._rate_hits.clear()
    body = client.get("/api/overview", params={"period": "30d"}).json()
    assert body["viral"]["periodBy"] == "published"


def test_inspection_and_alerts_accept_the_outlier_base():
    api._rate_hits.clear()
    params = {"video_id": "vhttpbig1", "fetch": "false"}
    video = client.get("/api/inspect/video", params={**params, "outlier_base": "period"}).json()
    assert video["found"] and video["metrics"]["outlierBase"] == "period"
    assert "outlierVsPeriod" in video["metrics"]

    batch = client.post("/api/inspect/videos",
                        json={"ids": ["vhttpbig1"], "fetch": False, "outlier_base": "period"})
    assert batch.status_code == 200 and batch.json()["results"]["vhttpbig1"]["found"]

    assert client.post("/api/events/scan", params={"outlier_base": "period"}).status_code == 200
    assert client.post("/api/events/scan", params={"outlier_base": "mean"}).status_code == 400
    assert client.get("/api/inspect/video",
                      params={**params, "outlier_base": "mean"}).status_code == 400


def test_track_route_resolves_a_handle_to_the_channel_id():
    # Issue #14: the dashboard and the extension track through this route,
    # which used to store whatever channel_id it was given.
    api._rate_hits.clear()
    cid = "UC" + "httptrackhandle".ljust(22, "0")
    conn = db.get_conn()
    db.upsert_channel(conn, {"channel_id": cid, "title": "Tracked By Handle",
                             "custom_url": "@httptrack", "description": "", "hidden_subs": 0})
    conn.commit()
    conn.close()

    resp = client.post("/api/channels/track", json={"channel_id": "@httptrack"})
    assert resp.status_code == 200
    assert resp.json()["channelId"] == cid
    assert cid in {r["channel_id"] for r in client.get("/api/channels/tracked").json()["channels"]}

    key, api.API_KEY = api.API_KEY, ""
    try:
        missing = client.post("/api/channels/track", json={"channel_id": "@nobody-here"})
    finally:
        api.API_KEY = key
    assert missing.status_code == 404
    assert "YOUTUBE_API_KEY" in missing.json()["detail"]

    assert client.delete("/api/channels/tracked/@httptrack").json()["channelId"] == cid
    assert cid not in {r["channel_id"] for r in client.get("/api/channels/tracked").json()["channels"]}


def test_tag_routes_write_read_and_delete():
    """Дашборд правит теги руками, поэтому POST жёстко пишет source=manual."""
    api._rate_hits.clear()
    group = "topic_group_http"

    created = client.post("/api/tags", json={"items": [
        {"videoId": "vhttpbig1", "group": group, "tag": "A"}]})
    assert created.status_code == 200
    assert created.json()["written"] == 1 and created.json()["source"] == "manual"

    tags = client.get(f"/api/niches/{NICHE}/tags").json()
    assert tags["count"] == 1
    assert tags["items"][0] == {"videoId": "vhttpbig1", "group": group, "tag": "a",
                               "source": "manual",
                               "createdAt": tags["items"][0]["createdAt"]}
    assert client.get(f"/api/videos/vhttpbig1/tags").json()["count"] == 1
    assert client.get(f"/api/niches/{NICHE}/tags", params={"group": "nope"}).json()["count"] == 0

    stats = client.get(f"/api/niches/{NICHE}/tag-stats", params={"group": group})
    assert stats.status_code == 200
    body = stats.json()
    assert body["group"] == group and body["tags"][0]["tag"] == "a"
    assert body["tags"][0]["videos"] == 1

    assert client.delete(f"/api/videos/vhttpbig1/tags/{group}/a").json()["removed"] == 1
    assert client.get(f"/api/videos/vhttpbig1/tags").json()["count"] == 0


def test_check_ideas_route_returns_a_verdict_per_idea():
    """Проверка идей (#3): POST, потому что список приходит из textarea."""
    api._rate_hits.clear()
    resp = client.post("/api/ideas/check",
                       json={"ideas": ["car wash", "underwater basket weaving"],
                             "niche": NICHE})
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2 and body["quota"] == 0
    by_idea = {r["idea"]: r for r in body["ideas"]}
    # «Car Wash» — название ролика ниши, вышел 1 июля 2026, дальше 90 дней
    assert by_idea["car wash"]["matches"] == 1
    assert by_idea["car wash"]["videos"][0]["videoId"] == "vhttpbig1"
    assert by_idea["underwater basket weaving"]["verdict"] == "free"
    # корпус без эмбеддингов не должен молча притворяться, что искал по смыслу
    assert body["corpus"]["embedded"] == 0 and "backfill_embeddings" in body["hint"]


def test_check_ideas_route_rejects_bad_input_with_400():
    api._rate_hits.clear()
    assert client.post("/api/ideas/check", json={"ideas": []}).status_code == 400
    assert client.post("/api/ideas/check", json={}).status_code == 400
    assert client.post("/api/ideas/check",
                       json={"ideas": ["x"], "outlier_base": "mean"}).status_code == 400
    typo = client.post("/api/ideas/check", json={"ideas": ["x"], "recent": 10})
    assert typo.status_code == 400 and "recent" in typo.json()["detail"]


def test_export_route_returns_a_tsv_attachment_with_the_niche_videos():
    """Экспорт ниши (#5): тот же use case, что и у CLI, поверх реальной базы."""
    api._rate_hits.clear()
    client.post("/api/tags", json={"items": [
        {"videoId": "vhttpbig1", "group": "topic_group_export", "tag": "A"}]})

    try:
        _assert_export_row()
    finally:
        # чужой тег в общей нише сломал бы соседний тест -- убираем его даже
        # если проверка упала на первом же assert
        client.delete("/api/videos/vhttpbig1/tags/topic_group_export/a")


def _assert_export_row():
    resp = client.get(f"/api/niches/{NICHE}/export.tsv")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/tab-separated-values")
    assert "attachment" in resp.headers["content-disposition"]
    assert ".tsv" in resp.headers["content-disposition"]

    header, row = resp.text.rstrip("\n").split("\n")
    assert header.split("\t")[:4] == ["channel", "handle", "subs", "video_id"]
    cells = dict(zip(header.split("\t"), row.split("\t")))
    assert cells["video_id"] == "vhttpbig1"
    assert cells["channel"] == "Big Competitor"
    assert cells["subs"] == "74600"
    assert cells["views"] == "55000"
    assert cells["likes"] == "100" and cells["comments"] == "10"
    assert cells["published_at"] == "2026-07-01T00:00:00Z"
    assert cells["tags"] == "topic_group_export=a"


def test_export_route_validates_the_outlier_base():
    api._rate_hits.clear()
    bad = client.get(f"/api/niches/{NICHE}/export.tsv", params={"outlier_base": "mean"})
    assert bad.status_code == 400


def test_tag_routes_reject_bad_input_with_400():
    api._rate_hits.clear()
    assert client.post("/api/tags", json={"items": []}).status_code == 400
    assert client.post("/api/tags", json={"items": [{"tag": "a"}]}).status_code == 400
    bad_base = client.get(f"/api/niches/{NICHE}/tag-stats",
                          params={"group": "g", "outlier_base": "mean"})
    assert bad_base.status_code == 400


if __name__ == "__main__":
    setup_module()
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except Exception as e:
            failed += 1
            import traceback
            print(f"  FAIL  {fn.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)  # иначе CI зеленеет при упавших тестах
