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
