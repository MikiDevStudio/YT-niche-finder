"""One-off (2026-09-29): put back the search queries niches were built from.

niches.query kept only the first query of a niche; the rest are known from the
session that collected them. Idempotent: re-running adds nothing new.

Run inside the web container: docker compose exec web python restore_niche_queries.py
"""
import infrastructure.postgres as db

KNOWN = {
    "gardening": ("en", ["gardening tips for beginners", "houseplant care guide", "flower garden ideas",
                         "vegetable garden grow your own food", "plant propagation",
                         "garden transformation timelapse", "rare plants collection"]),
    "garden-ru": ("ru", ["неприхотливые многолетники для ленивого сада",
                         "обзор садового центра цены на растения", "топ почвопокровных растений",
                         "что посеять в марте однолетники",
                         "самые красивые многолетники цветущие все лето",
                         "прогулка по ботаническому саду", "лианы для сада клематисы сорта"]),
    "garden-walks": ("en", ["garden walking tour 4k", "botanical garden walk relaxing",
                            "flower garden walk peaceful music", "japanese garden walk 4k",
                            "english garden tour relaxing"]),
    "garden-en": ("en", ["low maintenance perennials plant once", "garden center tour plant prices",
                         "best groundcover plants top", "perennials that bloom all summer",
                         "best clematis varieties", "annual flowers to sow in march",
                         "plant nursery tour rare plants", "garden center tour japan"]),
}


def main():
    conn = db.get_conn()
    added = 0
    for slug, (lang, queries) in KNOWN.items():
        for q in queries:
            db.add_niche_query(conn, slug, q, lang)
            added += 1
    for r in conn.execute("SELECT slug, query FROM niches "
                          "WHERE query IS NOT NULL AND query <> ''").fetchall():
        if not r["slug"].startswith("trending-"):
            db.add_niche_query(conn, r["slug"], r["query"])
            added += 1
    conn.commit()
    conn.close()
    print(f"queries offered: {added}")


if __name__ == "__main__":
    main()
