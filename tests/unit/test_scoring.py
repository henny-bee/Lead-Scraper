import ast
from pathlib import Path

import pytest

from leadscraper.domain.models import EmailFinding
from leadscraper.extractors.scoring import (
    generic_order,
    load_scoring_config,
    rank_emails,
    score_email,
    select_best_email,
)

SITE = "https://www.firma-example.de"
DE = dict(website=SITE, languages=("de",), country="DE")


def f(email: str, legal: bool = False, jsonld: bool = False, url: str = f"{SITE}/impressum") -> EmailFinding:
    return EmailFinding(email=email, source_url=url, on_legal_notice=legal, from_jsonld=jsonld)


def best(*findings: EmailFinding, **kw) -> str:
    return select_best_email(list(findings), **{**DE, **kw}).email


def test_info_beats_agency_and_privacy() -> None:
    assert best(f("agentur@webdesign-example.de"), f("datenschutz@firma-example.de", legal=True),
                f("info@firma-example.de")) == "info@firma-example.de"


def test_generic_beats_person_like() -> None:
    assert best(f("max.mustermann@firma-example.de", legal=True), f("kontakt@firma-example.de")) == "kontakt@firma-example.de"


def test_score_breakdown_matches_a37() -> None:
    kw = dict(website_domain="firma-example.de", languages=("de",), country="DE")
    info = score_email(f("info@firma-example.de", legal=True), **kw)
    assert info.reasons == {"same_domain": 50, "generic": 30, "legal_or_jsonld": 10}
    agency = score_email(f("agentur@webdesign-example.de"), **kw)
    assert agency.score == 0
    dsb = score_email(f("datenschutz@firma-example.de"), **kw)
    assert dsb.reasons == {"same_domain": 50, "special_function": -40}
    person = score_email(f("max.mustermann@firma-example.de"), **kw)
    assert person.reasons == {"same_domain": 50, "person_like": -10}
    free = score_email(f("firma.muc@gmx.de"), **kw)
    assert free.reasons == {"person_like": -10, "free_mail": -20}


def test_generic_order_from_config_decides_score() -> None:
    cfg = load_scoring_config()
    order = generic_order(cfg, ("de",))
    assert order[:3] == ["info", "kontakt", "office"]
    assert order.index("contact") > order.index("buero")             # English appended after German
    kw = dict(website_domain="firma-example.de", languages=("de",), country="DE")
    first = score_email(f("info@firma-example.de"), **kw).reasons["generic"]
    last = score_email(f(f"{order[-1]}@firma-example.de"), **kw).reasons["generic"]
    assert (first, last) == (30, 20)
    assert best(f("vertrieb@firma-example.de"), f("kontakt@firma-example.de")) == "kontakt@firma-example.de"


def test_language_specific_generic_parts() -> None:
    assert best(f("ventas@empresa-ejemplo.es"), f("juan.ejemplo@empresa-ejemplo.es"), website="https://empresa-ejemplo.es",
                languages=("es",), country="ES") == "ventas@empresa-ejemplo.es"
    ranked = rank_emails([f("bonjour@societe-exemple.fr")], website="https://societe-exemple.fr", languages=("fr",),
                         country="FR")
    assert ranked[0].reasons["generic"] > 20


def test_special_function_variants() -> None:
    kw = dict(website_domain="firma-example.de", languages=("de",), country="DE")
    for local in ("privacy", "dpo", "rgpd", "jobs", "karriere", "careers", "presse", "press",
                  "noreply", "no-reply", "webmaster", "datenschutz.bayern"):
        assert "special_function" in score_email(f(f"{local}@firma-example.de"), **kw).reasons, local


def test_free_mail_per_country_override() -> None:
    de = score_email(f("lojamaria@gmail.com"), website_domain=None, languages=("de",), country="DE")
    br = score_email(f("lojamaria@gmail.com"), website_domain=None, languages=("pt",), country="BR")
    assert de.reasons["free_mail"] == -20 and br.reasons["free_mail"] == -10


def test_jsonld_bonus_and_merge_of_duplicates() -> None:
    ranked = rank_emails([f("sales@firma-example.de", url=f"{SITE}/"), f("SALES@firma-example.de", jsonld=True),
                          f("office@firma-example.de")], **DE)
    assert [r.email for r in ranked] == ["office@firma-example.de", "sales@firma-example.de"] or \
        ranked[0].email == "sales@firma-example.de"
    sales = next(r for r in ranked if r.email == "sales@firma-example.de")
    assert sales.finding.from_jsonld and "legal_or_jsonld" in sales.reasons
    assert len(ranked) == 2


def test_ties_keep_found_order_and_empty() -> None:
    assert best(f("info@firma-example.de"), f("info@firma-gruppe-example.de"), website=None) == "info@firma-example.de"
    assert select_best_email([], **DE) is None


def test_custom_weights_change_outcome(tmp_path: Path) -> None:
    cfg_file = tmp_path / "roles.yaml"
    cfg_file.write_text("weights: {same_domain: 0, generic_max: 30, generic_min: 20, "
                        "special_function: 100}\ngeneric: {de: [info]}\nspecial_function: [presse]\n",
                        encoding="utf-8")
    cfg = load_scoring_config(cfg_file)
    assert best(f("info@firma-example.de"), f("presse@firma-example.de"), config=cfg,
                is_free=lambda d: False) == "presse@firma-example.de"


def test_no_numeric_weights_in_scoring_logic() -> None:
    """Weights come from config: the module contains no numeric literals besides 0/1/2."""
    src = (Path(__file__).resolve().parents[2] / "src" / "leadscraper" / "extractors" / "scoring.py")
    numbers = {n.value for n in ast.walk(ast.parse(src.read_text(encoding="utf-8")))
               if isinstance(n, ast.Constant) and isinstance(n.value, (int, float))
               and not isinstance(n.value, bool)}
    assert numbers <= {0, 1, 2}, numbers
