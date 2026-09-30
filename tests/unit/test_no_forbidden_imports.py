"""Guards: no forbidden imports in src/ and runtime deps ⊆."""

import ast
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "leadscraper"

FORBIDDEN_MODULES = {
    "redis", "sqlalchemy", "psycopg", "psycopg2", "asyncpg", "alembic", "geoalchemy2", "playwright",
    "taskiq", "taskiq_redis", "openai", "anthropic", "crawl4ai", "bs4", "openpyxl", "shapely",
    "pydantic_settings", "ulid", "testcontainers", "pyosmium", "osmium", "litellm",
}
#: runtime list (pyyaml yes; shapely, pydantic-settings, lxml unused)
ALLOWED_RUNTIME = {
    "fastapi", "uvicorn", "pydantic", "httpx", "tenacity", "aiolimiter", "protego", "selectolax",
    "lxml", "pycountry", "babel", "rapidfuzz", "google-i18n-address", "tldextract", "phonenumbers",
    "python-stdnum", "email-validator", "dnspython", "structlog", "prometheus-client", "pyyaml",
}
ALLOWED_DEV = {"pytest", "respx", "aiosmtpd"}


def _imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.add(node.module.split(".")[0])
    return out


def _name(req: str) -> str:
    return re.split(r"[<>=!~\[; ]", req, maxsplit=1)[0].strip().lower().replace("_", "-")


def test_no_forbidden_imports_in_src() -> None:
    offenders = {str(p.relative_to(ROOT)): sorted(_imports(p) & FORBIDDEN_MODULES)
                 for p in SRC.rglob("*.py") if _imports(p) & FORBIDDEN_MODULES}
    assert offenders == {}


def test_runtime_dependencies_are_subset_of_plan_21() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    runtime = {_name(r) for r in project["project"]["dependencies"]}
    assert runtime <= ALLOWED_RUNTIME, runtime - ALLOWED_RUNTIME
    dev = {_name(r) for r in project["dependency-groups"]["dev"]}
    assert dev <= ALLOWED_DEV, dev - ALLOWED_DEV
    assert "shapely" not in runtime and "pydantic-settings" not in runtime


def test_lockfile_has_no_forbidden_packages() -> None:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    names = {p["name"].lower().replace("_", "-") for p in lock.get("package", [])}
    forbidden = {m.replace("_", "-") for m in FORBIDDEN_MODULES} | {"psycopg2-binary", "beautifulsoup4",
                                                                  "python-ulid", "pydantic-settings"}
    assert names & forbidden == set()


def test_no_database_or_redis_env_vars_read() -> None:
    for p in SRC.rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        assert "DATABASE_URL" not in text and "REDIS_URL" not in text, p.name
