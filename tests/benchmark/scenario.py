"""Benchmark harness: scenarios S1–S4, mocked web, metrics."""

from __future__ import annotations

import asyncio
import json
import math
import re
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote

import httpx
import respx
import yaml

from leadscraper.crawler.website import normalize_website, registered_domain
from leadscraper.jobs.manager import JobManager
from leadscraper.observability import metrics
from leadscraper.schemas.scrape import ScrapeRequest
from leadscraper.services import scrape_service
from leadscraper.services.resolver import geo
from leadscraper.services.resolver.resolve import Resolver
from leadscraper.settings import load_settings
from leadscraper.sources import osm_overpass
from leadscraper.verification import lists

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "benchmark"
OVERPASS = "https://overpass.test/api/interpreter"      # custom URL → never any mirror
DDG_HOST = "html.duckduckgo.com"
SEARXNG_HOST = "searxng.test"
SEARCH_HOSTS = frozenset({DDG_HOST, SEARXNG_HOST})
DIRECTORY_DOMAINS = frozenset({"gelbeseiten.de"})
DIRECTORY = "https://www.gelbeseiten.de/gsbiz/"
PUBLIC_IP = "93.184.216.34"
HTML = "text/html; charset=utf-8"
POLL_S = 0.05
RUNS = 3
#: Same-domain gap tolerance. says "delay − 10 ms"; the Fetcher paces with ``time.monotonic`` and
#: asyncio timers may fire up to one clock tick early, so on a coarse monotonic clock (Windows
#: CPython ≤ 3.12: GetTickCount64, 15.6 ms) the tolerance is two ticks.
GAP_TOLERANCE_S = max(0.010, 2 * time.get_clock_info("monotonic").resolution)
_AREA_RE = re.compile(r'"ISO3166-2"="([A-Z]{2}-[A-Z0-9]+)"')

#: Config name → extra env.
CONFIGS: dict[str, dict[str, str]] = {"default": {}, "off": {"WEB_SEARCH_URL": "off"},
                                      "searxng": {"WEB_SEARCH_URL": f"http://{SEARXNG_HOST}"}}
RECOVERABLE_CONFIG = {"searxng": "default"}
DISCOVERY_SOURCES = ("osm", "web_search")      # candidates_discovered_total{source}
TIMING_KEYS = ("wall_s", "discovery_wall_s", "time_to_first_s", "time_to_10_s", "time_to_target_s")
IDENTITY_KEYS = ("count", "with_email", "with_website", "email_precision", "website_precision",
                 "recall", "decoys_accepted", "duplicate_records")
#: Not an identity key: when the target is below the recoverable pool, *which* sources fill it
#: depends on completion order (e.g.
RESOLVE_METHODS = ("osm_tag", "email_domain", "search", "guess", "web_discovery", "web_discovery_merged")


# --- scenario model ---------------------------------------------------------------------------
@dataclass(slots=True)
class Resp:
    status: int
    body: str = ""
    content_type: str = HTML


@dataclass
class Scenario:
    name: str
    request: dict[str, Any]
    env: dict[str, str]
    site_latency_s: float
    overpass_latency_s: float
    recoverable: dict[str, int]
    #: slice matcher (area code, industry keyword) → Overpass elements; ``None`` = every slice query
    overpass: list[tuple[tuple[str, str] | None, list[dict[str, Any]]]]
    pages: dict[tuple[str, str], Resp] = field(default_factory=dict)       # (host, path) → response
    dns: set[str] = field(default_factory=set)
    lookup_serp: dict[str, list[tuple[str, str]]] = field(default_factory=dict)  # name → [(url, title)]
    discovery_serp: list[tuple[str, str]] = field(default_factory=list)
    gazetteer: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    names: list[str] = field(default_factory=list)                          # every company name
    truth: dict[str, str | None] = field(default_factory=dict)             # website origin → email
    decoys: set[str] = field(default_factory=set)                          # decoy website origins
    #: ``WEB_SEARCH_MIN_INTERVAL_S`` override on the new code
    search_min_interval_s: float | None = None

    @property
    def max_output(self) -> int:
        return int(self.request["max_output"])

    @property
    def delay_s(self) -> float:
        return float(self.env.get("CRAWLER_PER_DOMAIN_DELAY_S", "2"))


# --- HTML templates -----------------------------------------------------------------------------
def _page(title: str, body: str, footer: str = "") -> str:
    foot = f"<footer><p>{footer}</p></footer>" if footer else ""
    return (f'<!doctype html><html lang="de"><head><meta charset="utf-8"><title>{title}</title>'
            f"</head><body>{body}{foot}</body></html>")


def _home(name: str, links: list[tuple[str, str]], text: str, footer: str = "") -> str:
    nav = " ".join(f'<a href="{href}">{label}</a>' for href, label in links)
    return _page(name, f"<header><nav>{nav}</nav></header><main><h1>{name}</h1><p>{text}</p></main>",
                 footer)


def _legal(name: str, address: list[str], phone: str, extra: str, footer: str = "", *,
           court_city: str = "Bremen", branches: str = "") -> str:
    """A legal-notice page."""
    lines = "".join(f"<p>{line}</p>" for line in address if line)
    branch = f"<p>{branches}</p>" if branches else ""
    return _page(f"Impressum | {name}",
                 f"<main><h1>Impressum</h1><p>{name}</p>{lines}<p>Telefon: {phone}</p>{extra}"
                 f"<p>Registergericht: Amtsgericht {court_city}</p>{branch}</main>", footer)


def _plain_page(title: str, text: str, footer: str = "") -> str:
    return _page(title, f"<main><h1>{title}</h1><p>{text}</p></main>", footer)


def cfemail(address: str, key: int = 0x5A) -> str:
    """Cloudflare email-protection encoding (first byte = XOR key)."""
    return f"{key:02x}" + "".join(f"{ord(ch) ^ key:02x}" for ch in address)


def email_html(variant: str, email: str, idx: int) -> str:
    if not email:
        return ""
    local, _, domain = email.partition("@")
    label, _, tld = domain.rpartition(".")
    if variant == "inline_b":
        return f"<p>E-Mail: <b>{local}</b>@{domain}</p>"
    if variant == "inline_span":
        return f"<p>E-Mail: {local}<span>@</span>{domain}</p>"
    if variant == "inline_split":
        return f"<p>E-Mail: <span>{local}</span><span>@{domain}</span></p>"
    if variant == "cf_attr":
        return (f'<p>E-Mail: <a href="/cdn-cgi/l/email-protection" class="__cf_email__" '
                f'data-cfemail="{cfemail(email)}">[email&#160;protected]</a></p>')
    if variant == "cf_href":
        return (f'<p>E-Mail: <a href="/cdn-cgi/l/email-protection#{cfemail(email)}">'
                f'<span class="__cf_email__">[email&#160;protected]</span></a></p>')
    if variant == "fullwidth":
        return f"<p>E-Mail: {local}＠{label}．{tld}</p>"
    if variant == "at":
        return f"<p>E-Mail: {local} [at] {label} [punkt] {tld}</p>"
    if variant == "placeholder":
        return (f'<form><label>Ihre E-Mail</label><input type="email" name="email" '
                f'placeholder="mustermann@{domain}"></form><p>E-Mail: {email}</p>')
    if variant in ("noemail", "kontakt_unlinked"):
        return ""                                   # no email / email on another page
    if idx % 2:                                     # plain and all other variants
        return f'<p>E-Mail: <a href="mailto:{email}">{email}</a></p>'
    return f"<p>E-Mail: {email}</p>"


# --- S1 / S3 from YAML ------------------------------------------------------------------------
def _node(el: dict[str, Any], lat: float = 53.08, lon: float = 8.80) -> dict[str, Any]:
    tags = {"name": el["name"], "addr:postcode": el["postcode"], "addr:city": el["city"]}
    if el.get("tag_website"):
        tags["website"] = el["tag_website"]
    if el.get("tag_email"):
        tags["email"] = el["tag_email"]
    return {"type": "node", "id": int(el["id"]), "lat": lat, "lon": lon, "tags": tags}


def _add_site(sc: Scenario, el: dict[str, Any], idx: int) -> None:
    host = el["site"]
    decoy = el.get("decoy") or {}
    variant = el.get("variant", "plain")
    email = el.get("email") or ""
    name = el.get("legal_name") or el["name"]
    footer = el.get("footer", "")
    postcode, city = decoy.get("postcode", el["postcode"]), decoy.get("city", el["city"])
    legal_kw = {"court_city": city, "branches": el.get("branches", "")}
    address = [decoy.get("street", f"Hafenstraße {idx}"), f"{postcode} {city}".strip()]
    if variant == "footer_only":
        address = []                                          # no own address on the legal page
    phone = f"0421 555{idx:03d}"
    industry = variant != "non_industry"
    text = ("Ihr Partner für Transport und Lagerung in Norddeutschland." if industry
            else "Frische Schnittblumen, Sträuße und Gestecke für jeden Anlass.")
    if el.get("slogan"):
        text += " " + el["slogan"]
    services = "Transporte und Touren." if industry else "Sträuße und Gestecke."
    sc.dns.add(host)
    links = [("/impressum", "Impressum"), ("/leistungen", "Leistungen")]
    pages: dict[str, Resp] = {"/leistungen": Resp(200, _plain_page("Leistungen", services, footer))}
    if variant == "kontakt_unlinked":
        pages["/impressum"] = Resp(200, _legal(name, address, phone, "", footer, **legal_kw))
        pages["/kontakt"] = Resp(200, _plain_page("Kontakt", f"Schreiben Sie uns: {email}", footer))
    elif variant == "sitemap":
        links = [("/leistungen", "Leistungen"), ("/fuhrpark", "Fuhrpark")]
        pages["/fuhrpark"] = Resp(200, _plain_page("Fuhrpark", "Unsere Lkw."))
        pages["/robots.txt"] = Resp(200, f"User-agent: *\nAllow: /\nSitemap: https://{host}/sitemap.xml\n",
                                    "text/plain")
        locs = "".join(f"<url><loc>https://{host}{p}</loc></url>"
                       for p in ("/", "/leistungen", "/fuhrpark", "/rechtliches/impressum.html"))
        pages["/sitemap.xml"] = Resp(200, '<?xml version="1.0" encoding="UTF-8"?><urlset '
                                          f'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{locs}'
                                          "</urlset>", "application/xml")
        pages["/rechtliches/impressum.html"] = Resp(200, _legal(name, address, phone,
                                                                email_html("plain", email, idx),
                                                                **legal_kw))
    else:
        pages["/impressum"] = Resp(200, _legal(name, address, phone, email_html(variant, email, idx),
                                               footer, **legal_kw))
    pages["/"] = Resp(200, _home(name, links, text, footer))
    for path, resp in pages.items():
        sc.pages[(host, path)] = resp


def load_yaml_scenario(path: Path) -> Scenario:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    sc = Scenario(name=raw["name"], request=raw["request"],
                  env={k: str(v) for k, v in raw["env"].items()},
                  site_latency_s=float(raw["site_latency_s"]),
                  overpass_latency_s=float(raw["overpass_latency_s"]),
                  recoverable={k: int(v) for k, v in raw["recoverable"].items()}, overpass=[])
    by_slice: dict[tuple[str, str] | None, list[dict[str, Any]]] = defaultdict(list)
    by_id: dict[int, dict[str, Any]] = {}
    for idx, el in enumerate(raw["elements"], start=1):
        el.setdefault("city", "Bremen")
        el.setdefault("postcode", str(28195 + 2 * (idx - 1)))
        by_id[int(el["id"])] = el
        sc.names.append(el["name"])
        if el["kind"] != "K":                                   # K = web-only, not in OSM
            matcher = tuple(el["slice"]) if el.get("slice") else None
            by_slice[matcher].append(_node(el))
        if el.get("site"):
            _add_site(sc, el, idx)
        if el.get("search"):                                    # mocked lookup SERP
            slug = el["name"].lower().replace(" ", "-")
            sc.lookup_serp[el["name"]] = [(DIRECTORY + quote(slug), f"{el['name']} - Gelbe Seiten"),
                                          (f"https://{el['site']}/", el.get("legal_name") or el["name"])]
        if el.get("expect_website"):
            sc.truth[normalize_website(el["expect_website"]).origin] = el.get("expect_email")
        if el.get("decoy_site"):
            sc.decoys.add(normalize_website(el["site"]).origin)
    for item in raw.get("discovery_serp") or []:
        if "element" in item:
            el = by_id[int(item["element"])]
            sc.discovery_serp.append((f"https://{el['site']}/", f"{el['name']} | {el['city']}"))
        else:
            sc.discovery_serp.append((item["url"], item["title"]))
    sc.gazetteer = {k: {"postcodes": [str(p) for p in v.get("postcodes", [])],
                        "places": list(v.get("places", []))}
                    for k, v in (raw.get("gazetteer") or {}).items()}
    assert len(sc.decoys) == int(raw.get("decoys", 0)), (sc.decoys, raw.get("decoys"))
    sc.overpass = list(by_slice.items())
    return sc


# --- S4 (generated) -----------------------------------------------------------------------------
def build_s4() -> Scenario:
    """200 website-tagged companies; 160 with a same-domain email on the linked Impressum; every
    home links Impressum, Kontakt, Über uns and Datenschutz."""
    sc = Scenario(name="S4", request={"country": "Germany", "regions": ["Bremen"],
                                      "industries": ["Logistik"],
                                      "information": ["company_name", "company_email", "website"],
                                      "max_output": 100},
                  env={"CRAWLER_PER_DOMAIN_DELAY_S": "0.2"}, site_latency_s=0.05,
                  overpass_latency_s=0.2, recoverable={"default": 160, "off": 160}, overpass=[])
    elements = []
    for i in range(1, 201):
        host = f"s4-firma-{i:03d}-example.de"
        name = f"Nordsee Logistik {i:03d} GmbH"
        postcode = str(28195 + i)
        email = f"info@{host}" if i % 5 else None                      # 160 of 200 have an email
        elements.append(_node({"id": 10_000 + i, "name": name, "postcode": postcode,
                               "city": "Bremen", "tag_website": f"https://{host}"}))
        sc.names.append(name)
        sc.dns.add(host)
        sc.truth[f"https://{host}"] = email
        links = [("/impressum", "Impressum"), ("/kontakt", "Kontakt"), ("/ueber-uns", "Über uns"),
                 ("/datenschutz", "Datenschutz")]
        sc.pages[(host, "/")] = Resp(200, _home(name, links, "Transport und Lagerung seit 1950."))
        sc.pages[(host, "/impressum")] = Resp(200, _legal(name, [f"Kaistraße {i}", f"{postcode} Bremen"],
                                                          f"0421 777{i:03d}",
                                                          email_html("plain", email or "", i)))
        sc.pages[(host, "/kontakt")] = Resp(200, _plain_page("Kontakt", f"Telefon 0421 777{i:03d}"))
        sc.pages[(host, "/ueber-uns")] = Resp(200, _plain_page("Über uns", "Seit 1950 in Bremen."))
        sc.pages[(host, "/datenschutz")] = Resp(200, _plain_page("Datenschutz", "Hinweise zum Datenschutz."))
    sc.overpass = [(None, elements)]
    return sc


def load_scenario(name: str) -> Scenario:
    if name == "S1":
        return load_yaml_scenario(FIXTURES / "scenario_s1.yaml")
    if name == "S2":
        s1 = load_yaml_scenario(FIXTURES / "scenario_s1.yaml")
        return replace(s1, name="S2", request={**s1.request, "max_output": 60}, search_min_interval_s=0.05)
    if name == "S3":
        return load_yaml_scenario(FIXTURES / "scenario_s3.yaml")
    if name == "S4":
        return build_s4()
    raise KeyError(name)


# --- recording + mocked web -----------------------------------------------------------------------
@dataclass(slots=True)
class Hit:
    domain: str
    host: str
    path: str
    start: float
    end: float
    robots: bool


@dataclass
class Recorder:
    t0: float = 0.0
    hits: list[Hit] = field(default_factory=list)
    inflight: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    max_concurrent_per_domain: int = 0
    overpass_inflight: int = 0
    max_overpass_in_flight: int = 0
    slice_responses: list[float] = field(default_factory=list)     # slice queries only
    gazetteer_requests: int = 0
    search_requests: int = 0
    directory_requests: int = 0


def _slice_matches(matcher: tuple[str, str] | None, query: str) -> bool:
    if matcher is None:
        return True
    area, keyword = matcher
    return f'"ISO3166-2"="{area}"' in query and keyword.lower() in query.lower()


def _gazetteer_elements(sc: Scenario, query: str) -> list[dict[str, Any]]:
    m = _AREA_RE.search(query)
    data = sc.gazetteer.get(m.group(1) if m else "", {})
    out: list[dict[str, Any]] = [{"type": "relation", "id": 900_000 + i,
                                  "tags": {"boundary": "postal_code", "postal_code": pc}}
                                 for i, pc in enumerate(data.get("postcodes", []))]
    out += [{"type": "node", "id": 950_000 + i, "tags": {"place": "suburb" if i else "city", "name": n}}
            for i, n in enumerate(data.get("places", []))]
    return out


def _ddg_html(results: list[tuple[str, str]]) -> str:
    items = "".join(
        f'<div class="result results_links web-result"><div class="links_main result__body">'
        f'<h2 class="result__title"><a rel="nofollow" class="result__a" '
        f'href="//duckduckgo.com/l/?uddg={quote(url, safe="")}&amp;rut=x">{title}</a></h2>'
        f'<a class="result__snippet" href="//duckduckgo.com/l/?uddg={quote(url, safe="")}">{title}</a>'
        f"</div></div>" for url, title in results)
    return f'<html><body><div id="links" class="results">{items}</div></body></html>'


def serp_for(sc: Scenario, q: str) -> list[tuple[str, str]]:
    """Company-name lookups get that company's SERP (or none); every other query is a slice
    discovery query and gets the scenario's discovery SERP."""
    low = q.lower()
    for name in sc.names:
        if name.lower() in low:
            return sc.lookup_serp.get(name, [])
    if '"' in q:                                   # a quoted name we do not know → nothing
        return []
    return list(sc.discovery_serp)


def install_routes(mock: respx.MockRouter, sc: Scenario, rec: Recorder) -> None:
    async def overpass(request: httpx.Request) -> httpx.Response:
        query = parse_qs(request.content.decode())["data"][0]
        gazetteer = 'boundary"="postal_code"' in query
        rec.overpass_inflight += 1
        rec.max_overpass_in_flight = max(rec.max_overpass_in_flight, rec.overpass_inflight)
        try:
            await asyncio.sleep(sc.overpass_latency_s)
            if gazetteer:
                rec.gazetteer_requests += 1
                return httpx.Response(200, json={"elements": _gazetteer_elements(sc, query)})
            elements = [e for matcher, els in sc.overpass if _slice_matches(matcher, query) for e in els]
            return httpx.Response(200, json={"elements": elements})
        finally:
            rec.overpass_inflight -= 1
            if not gazetteer:
                rec.slice_responses.append(time.perf_counter())

    async def search(request: httpx.Request) -> httpx.Response:
        rec.search_requests += 1
        url = request.url
        if url.path == "/robots.txt":
            if url.host == DDG_HOST:
                return httpx.Response(200, text="User-agent: *\nAllow: /\n")
            return httpx.Response(404, text="")
        params = parse_qs(url.query.decode()) if url.query else {}
        if request.content:
            params.update(parse_qs(request.content.decode()))
        hits = serp_for(sc, (params.get("q") or [""])[0])
        if url.host == DDG_HOST:
            return httpx.Response(200, headers={"content-type": HTML}, text=_ddg_html(hits))
        return httpx.Response(200, json={"results": [{"url": u, "title": t, "content": t}
                                                     for u, t in hits]})

    async def site(request: httpx.Request) -> httpx.Response:
        url = request.url
        domain = registered_domain(f"https://{url.host}") or url.host
        if domain in DIRECTORY_DOMAINS:
            rec.directory_requests += 1
        start = time.perf_counter()
        rec.inflight[domain] += 1
        rec.max_concurrent_per_domain = max(rec.max_concurrent_per_domain, rec.inflight[domain])
        try:
            await asyncio.sleep(sc.site_latency_s)
            resp = sc.pages.get((url.host, url.path))
            if resp is None and url.path == "/robots.txt":
                return httpx.Response(404, headers={"content-type": "text/plain"}, text="")
            if resp is None:
                return httpx.Response(404, headers={"content-type": HTML}, text="<html>404</html>")
            return httpx.Response(resp.status, headers={"content-type": resp.content_type}, text=resp.body)
        finally:
            rec.inflight[domain] -= 1
            rec.hits.append(Hit(domain, url.host, url.path, start, time.perf_counter(),
                                url.path == "/robots.txt"))

    mock.post(OVERPASS).mock(side_effect=overpass)
    mock.route(host__in=tuple(sorted(SEARCH_HOSTS))).mock(side_effect=search)
    mock.get(url__regex=r"https?://.*").mock(side_effect=site)


# --- one run ------------------------------------------------------------------------------------
def is_new_code() -> bool:
    return hasattr(scrape_service.PipelineDeps, "search_backend")


def make_gate(settings: Any) -> Any:
    """Fast Overpass gate: the equivalent ``OverpassGatePool`` on the new code."""
    pool = getattr(osm_overpass, "OverpassGatePool", None)
    if pool is not None:
        return pool.for_settings(settings, per_minute=600_000)
    return osm_overpass.OverpassGate(per_minute=600_000)


def search_deps(sc: Scenario, settings: Any) -> dict[str, Any]:
    """New code only: the backend built from the settings plus a fresh ``SearchGate``."""
    if not is_new_code():
        return {}
    from leadscraper.sources import web_search              # noqa: PLC0415 (new code only)
    backend = web_search.build_search_backend(settings)
    if backend is None:
        return {}
    kwargs = {} if sc.search_min_interval_s is None else {"min_interval_s": sc.search_min_interval_s}
    return {"search_backend": backend, "search_gate": web_search.SearchGate(**kwargs)}


def _no_verifier(_settings: Any) -> Any:
    raise AssertionError("email verification is not part of the benchmark")


def make_deps(sc: Scenario, settings: Any, tmp: Path) -> Any:
    supp = tmp / "suppression.txt"
    supp.write_text("", encoding="utf-8")

    async def fake_dns(host: str) -> list[str]:
        if host.lower() in sc.dns:
            return [PUBLIC_IP]
        raise OSError(f"fake DNS: {host} does not resolve")

    return scrape_service.PipelineDeps(
        settings=settings, resolver=Resolver(settings), gate=make_gate(settings),
        verifier_factory=_no_verifier, dns_resolve=fake_dns,
        suppression=lists.SuppressionList(path=supp), **search_deps(sc, settings))


def _discovered_samples() -> dict[str, float]:
    """``candidates_discovered_total`` summed over countries, per source."""
    out = {s: 0.0 for s in DISCOVERY_SOURCES}
    for family in metrics.REGISTRY.collect():
        for sample in family.samples:
            if sample.name == "candidates_discovered_total" and sample.labels.get("source") in out:
                out[sample.labels["source"]] += sample.value
    return out


def _resolved_samples() -> dict[str, float] | None:
    if not hasattr(metrics, "WEBSITES_RESOLVED"):
        return None                                           # old code: no such metric
    return {m: metrics.REGISTRY.get_sample_value("websites_resolved_total", {"method": m}) or 0.0
            for m in RESOLVE_METHODS}


_WARMED: set[str] = set()


async def warm_up(sc: Scenario, settings: Any) -> None:
    """Build the resolver's lazy indexes once per process, so the first measured run is not charged
    for it."""
    if sc.name in _WARMED:
        return
    await asyncio.to_thread(geo.warm_up)
    await Resolver(settings).resolve(ScrapeRequest.model_validate(sc.request))
    _WARMED.add(sc.name)


async def run_once(sc: Scenario, config: str, tmp: Path) -> dict[str, Any]:
    tmp.mkdir(parents=True, exist_ok=True)
    settings = load_settings({"TEMP_DIR": str(tmp / "jobs"), "OVERPASS_URL": OVERPASS,
                              **sc.env, **CONFIGS[config]})
    await warm_up(sc, settings)
    deps = make_deps(sc, settings, tmp)
    jobs = JobManager(settings.temp_dir)
    rec = Recorder()
    marks: dict[str, float | None] = {"time_to_first_s": None, "time_to_10_s": None,
                                      "time_to_target_s": None}
    thresholds = {"time_to_first_s": 1, "time_to_10_s": 10, "time_to_target_s": sc.max_output}

    def mark(count: int, now: float) -> None:
        for key, n in thresholds.items():
            if marks[key] is None and count >= n:
                marks[key] = round(now, 3)

    before, found_before = _resolved_samples(), _discovered_samples()
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        install_routes(mock, sc, rec)
        job, _ = jobs.create("scrape", dict(sc.request), target=sc.max_output)
        rec.t0 = time.perf_counter()
        task = jobs.start(job, scrape_service.runner_for(deps, jobs))
        while not task.done():
            await asyncio.wait({task}, timeout=POLL_S)
            mark(job.count, time.perf_counter() - rec.t0)
        wall = time.perf_counter() - rec.t0
        await task
    assert job.status.value == "success", (job.status, job.error)
    companies = list(job.result or [])
    mark(len(companies), wall)
    progress = dict(job.progress)
    after, found_after = _resolved_samples(), _discovered_samples()
    await jobs.close()
    if before is None or after is None:
        by_source = {"osm_tag": len(companies)}               # old code: every record is osm_tag
    else:
        by_source = {m: int(after[m] - before[m]) for m in RESOLVE_METHODS if after[m] - before[m]}
    candidates_by_source = {s: int(found_after[s] - found_before[s]) for s in DISCOVERY_SOURCES}
    return {**_metrics(sc, config, companies, progress, rec, wall), **marks, "by_source": by_source,
            "candidates_by_source": candidates_by_source}


def _metrics(sc: Scenario, config: str, companies: list[dict[str, Any]], progress: dict[str, int],
             rec: Recorder, wall: float) -> dict[str, Any]:
    with_email = [c for c in companies if c.get("company_email")]
    with_website = [c for c in companies if c.get("website")]
    right_site = [c for c in with_website if c["website"] in sc.truth]
    right_email = [c for c in with_email if c.get("website") in sc.truth
                   and sc.truth[c["website"]] == c["company_email"]]
    per_domain: dict[str, list[Hit]] = defaultdict(list)
    page_counts: dict[str, int] = defaultdict(int)
    for h in rec.hits:
        per_domain[h.domain].append(h)
        if not h.robots:
            page_counts[h.domain] += 1
    gaps: list[float] = []
    for hits in per_domain.values():
        hits.sort(key=lambda h: h.start)
        gaps += [b.start - a.end for a, b in zip(hits, hits[1:])]
    recoverable = sc.recoverable.get(RECOVERABLE_CONFIG.get(config, config))
    crawled = progress.get("crawled", 0)
    return {
        "scenario": sc.name, "config": config, "new_code": is_new_code(), "wall_s": round(wall, 3),
        "count": len(companies), "target": sc.max_output,
        "fill_rate": round(len(companies) / sc.max_output, 4),
        "with_email": len(with_email), "with_website": len(with_website),
        "candidates": progress.get("candidates", 0), "crawled": crawled,
        "email_yield": round(progress.get("with_email", 0) / crawled, 4) if crawled else None,
        "email_precision": round(len(right_email) / len(with_email), 4) if with_email else None,
        "website_precision": round(len(right_site) / len(with_website), 4) if with_website else None,
        "recall": round(len(right_email) / recoverable, 4) if recoverable else None,
        "correct": len(right_email), "recoverable": recoverable,
        "decoys_accepted": sum(1 for c in companies if c.get("website") in sc.decoys),
        # a second record for the same site (the discovery SERP's merge cases)
        "duplicate_records": len(with_website) - len({c["website"] for c in with_website}),
        "requests_total": len(rec.hits),
        "max_requests_per_domain": max(page_counts.values(), default=0),
        "min_same_domain_gap_s": round(min(gaps), 4) if gaps else None,
        "max_concurrent_per_domain": rec.max_concurrent_per_domain,
        "max_overpass_in_flight": rec.max_overpass_in_flight,
        "discovery_wall_s": (round(max(rec.slice_responses) - rec.t0, 3)
                             if rec.slice_responses else None),
        "gazetteer_requests": rec.gazetteer_requests,
        "search_requests": rec.search_requests,
        "directory_requests": rec.directory_requests,
    }


# --- politeness + aggregation -----------------------------------------------------------------------
def assert_politeness(run: dict[str, Any], sc: Scenario, max_pages: int = 5) -> None:
    """Asserted in every run: ≤ 5 page requests per domain, same-domain gap ≥ delay −
    ``GAP_TOLERANCE_S`` (10 ms, or two ticks of a coarse monotonic clock), one concurrent request
    per domain; directory results are never crawled."""
    assert run["max_requests_per_domain"] <= max_pages, run
    if run["min_same_domain_gap_s"] is not None:
        assert run["min_same_domain_gap_s"] >= sc.delay_s - GAP_TOLERANCE_S, run
    assert run["max_concurrent_per_domain"] <= 1, run
    assert run["directory_requests"] == 0, run


def _median(values: list[float | None]) -> float | None:
    """Median with "never reached" (None) treated as +inf → None if the median is not reached."""
    med = statistics.median([math.inf if v is None else v for v in values])
    return None if math.isinf(med) else round(med, 3)


def summarize(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Timing keys = median of the runs; count/precision keys must be identical."""
    for key in IDENTITY_KEYS:
        values = {json.dumps(r[key], sort_keys=True) for r in runs}
        assert len(values) == 1, f"{key} differs across runs: {[r[key] for r in runs]}"
    out = dict(runs[0])
    for key in TIMING_KEYS:
        out[key] = _median([r[key] for r in runs])
    for key in ("email_yield", "crawled", "requests_total", "search_requests", "gazetteer_requests"):
        vals = [r[key] for r in runs if r[key] is not None]
        out[key] = round(statistics.median(vals), 4) if vals else None
    out["runs"] = len(runs)
    out["wall_s_runs"] = [r["wall_s"] for r in runs]
    out["by_source_runs"] = [r["by_source"] for r in runs]
    count = out["count"]
    out["by_source_share"] = ({m: round(n / count, 4) for m, n in out["by_source"].items()}
                              if count else {})
    return out


def bench_line(summary: dict[str, Any], prefix: str = "BENCH") -> str:
    return f"{prefix} {json.dumps(summary, ensure_ascii=False)}"
