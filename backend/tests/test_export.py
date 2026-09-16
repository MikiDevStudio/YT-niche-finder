"""Tests for application/exporting.py -- the niche TSV export (issue #5).

No Postgres and no network: search.niche_videos and the tag repository are
stubbed, the way test_tagging.py stubs the layer below it. What is under test
here is the file itself -- column order, escaping, empty cells -- because that
is the part YT-analyze parses; the real niche_videos is covered end to end in
test_mcp_tools.py, and the HTTP route in test_http_api.py.

Run: python3 tests/test_export.py (or pytest tests/test_export.py)
"""
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

TAG_ROWS = []


class _Conn:
    def close(self):
        pass


fake_db = types.ModuleType("infrastructure.postgres")
fake_db.get_conn = lambda: _Conn()
fake_db.video_tags_for = lambda conn, video_ids=None, niche=None, tag_group=None: [
    r for r in TAG_ROWS if video_ids is None or r["video_id"] in set(video_ids)]
sys.modules["infrastructure.postgres"] = fake_db

fake_search = types.ModuleType("application.search")
sys.modules["application.search"] = fake_search

from application import exporting as EX  # noqa: E402

VIDEO = {
    "videoId": "v1", "title": "Мойка\tсамообслуживания", "channelId": "UC1",
    "publishedAt": "2026-07-01T10:00:00Z", "ageDays": 60.0, "views": 55000,
    "likes": 900, "comments": 40, "lengthSeconds": 930, "isShort": False,
    "outlierScore": 2.5, "outlierScoreRolling": 2.4567, "outlierScorePeriod": None,
    "isOutlier": True, "isFresh": False,
}
CHANNEL = {"channelId": "UC1", "title": "Big Competitor", "handle": "@big",
           "subscribers": 74600, "videos": 1, "outliers": 1}


def _niche_videos(videos=None, channels=None, hint=None, found=True):
    payload = {"niche": "econ", "found": found, "period": "all",
               "outlierBase": "rolling", "outlierThreshold": 2.0, "freshDays": 30.0,
               "videoCount": len(videos or []), "channelCount": len(channels or []),
               "outlierCount": 0, "channels": channels or [], "videos": videos or [],
               "hint": hint}
    fake_search.niche_videos = lambda **kw: payload
    return payload


def _table(res):
    """The TSV back as a list of rows of cells -- no library, on purpose: a
    parser that splits on tabs is exactly what the file promises to survive."""
    return [line.split("\t") for line in res["tsv"].rstrip("\n").split("\n")]


def test_header_is_the_documented_column_order():
    _niche_videos()
    res = EX.niche_tsv("econ")
    assert _table(res)[0] == list(EX.COLUMNS)
    assert res["rows"] == 0
    assert res["tsv"].endswith("\n")


def test_row_carries_channel_video_and_both_baselines():
    _niche_videos([VIDEO], [CHANNEL])
    global TAG_ROWS
    TAG_ROWS = []
    row = _table(EX.niche_tsv("econ"))[1]
    cells = dict(zip(EX.COLUMNS, row))
    assert cells["channel"] == "Big Competitor"
    assert cells["handle"] == "@big"
    assert cells["subs"] == "74600"
    assert cells["video_id"] == "v1"
    assert cells["published_at"] == "2026-07-01T10:00:00Z"
    assert cells["views"] == "55000"
    assert cells["likes"] == "900"
    assert cells["comments"] == "40"
    assert cells["length_seconds"] == "930"
    assert cells["is_short"] == "false"
    assert cells["outlierScoreRolling"] == "2.457"
    # a missing baseline is an empty cell, not the string "None"
    assert cells["outlierScorePeriod"] == ""


def test_a_tab_in_a_title_does_not_shift_the_columns():
    _niche_videos([VIDEO], [CHANNEL])
    rows = _table(EX.niche_tsv("econ"))
    assert len(rows[1]) == len(EX.COLUMNS)
    assert dict(zip(EX.COLUMNS, rows[1]))["title"] == "Мойка самообслуживания"


def test_a_newline_in_a_title_does_not_add_a_row():
    _niche_videos([{**VIDEO, "title": "Первая строка\nвторая"}], [CHANNEL])
    rows = _table(EX.niche_tsv("econ"))
    assert len(rows) == 2
    assert dict(zip(EX.COLUMNS, rows[1]))["title"] == "Первая строка вторая"


def test_tags_of_a_video_are_grouped_into_one_cell():
    _niche_videos([VIDEO], [CHANNEL])
    global TAG_ROWS
    TAG_ROWS = [
        {"video_id": "v1", "tag_group": "trigger", "tag": "деньги"},
        {"video_id": "v1", "tag_group": "trigger", "tag": "автоматизация"},
        {"video_id": "v1", "tag_group": "topic_group_econ", "tag": "а"},
        {"video_id": "other", "tag_group": "trigger", "tag": "не наш"},
    ]
    res = EX.niche_tsv("econ")
    cells = dict(zip(EX.COLUMNS, _table(res)[1]))
    assert cells["tags"] == "topic_group_econ=а; trigger=автоматизация,деньги"
    assert res["taggedRows"] == 1
    TAG_ROWS = []


def test_untagged_video_gets_an_empty_tags_cell():
    _niche_videos([VIDEO], [CHANNEL])
    global TAG_ROWS
    TAG_ROWS = []
    row = _table(EX.niche_tsv("econ"))[1]
    assert dict(zip(EX.COLUMNS, row))["tags"] == ""


def test_hint_of_an_empty_niche_is_passed_through():
    _niche_videos(hint="nothing collected under this slug yet", found=False)
    res = EX.niche_tsv("econ")
    assert res["rows"] == 0
    assert res["found"] is False
    assert "nothing collected" in res["hint"]


def test_filename_is_the_export_date():
    _niche_videos()
    assert EX.niche_tsv("econ")["filename"] == EX.default_filename()
    assert EX.default_filename().startswith("videos_")
    assert EX.default_filename().endswith(".tsv")


def test_an_empty_niche_name_is_a_value_error():
    _niche_videos()
    for bad in ("", "   ", None):
        try:
            EX.niche_tsv(bad)
        except ValueError:
            continue
        raise AssertionError(f"niche={bad!r} should be rejected")


def _run_all():
    ns = dict(globals())
    tests = [(name, fn) for name, fn in ns.items() if name.startswith("test_")]
    passed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ok  {name}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {name}: {e}")
        except Exception as e:  # pragma: no cover
            print(f"ERROR {name}: {e!r}")
    print(f"\n{passed}/{len(tests)} прошло")
    sys.exit(0 if passed == len(tests) else 1)  # иначе CI зеленеет при упавших тестах


if __name__ == "__main__":
    _run_all()
