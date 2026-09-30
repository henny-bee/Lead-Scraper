"""Sitemap parsing: regex over <loc>, bounded, entities decoded, no XML parser."""

from leadscraper import constants as C
from leadscraper.crawler.sitemap import is_gzip, is_index, parse_locs, pick_children


def test_parse_locs_bounds() -> None:
    many = "<urlset>" + "".join(f"<url><loc>https://a-example.de/p{i}</loc></url>" for i in range(10_000)) + "</urlset>"
    locs = parse_locs(many)
    assert len(locs) == C.SITEMAP_MAX_LOCS and locs[0] == "https://a-example.de/p0"
    assert parse_locs(many, limit=3) == ["https://a-example.de/p0", "https://a-example.de/p1",
                                         "https://a-example.de/p2"]
    decoded = parse_locs("<loc> https://a-example.de/impressum?x=1&amp;y=2 </loc>")
    assert decoded == ["https://a-example.de/impressum?x=1&y=2"]
    malformed = "<urlset><url><loc>https://a-example.de/ok</loc><loc>unterminated <loc></loc>&lt;"
    assert parse_locs(malformed) == ["https://a-example.de/ok"]
    assert parse_locs("") == [] and parse_locs("not xml at all") == []


def test_index_detection_and_children() -> None:
    idx = '<?xml version="1.0"?><sitemapindex xmlns="x"><sitemap><loc>https://a-example.de/post-sitemap.xml</loc>'
    assert is_index(idx) and not is_index("<urlset></urlset>")
    locs = ["https://a-example.de/post-sitemap.xml", "https://a-example.de/page-sitemap.xml.gz",
            "https://a-example.de/page-sitemap.xml"]
    assert pick_children(locs) == ["https://a-example.de/page-sitemap.xml"]      # "page" first, .gz skipped
    assert pick_children(locs, limit=2) == ["https://a-example.de/page-sitemap.xml",
                                            "https://a-example.de/post-sitemap.xml"]
    assert is_gzip("https://a-example.de/sitemap.xml.gz?x=1") and not is_gzip("https://a-example.de/s.xml")
