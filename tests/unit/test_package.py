import leadscraper


def test_package_importable() -> None:
    assert leadscraper.__version__ == "0.3.0"
