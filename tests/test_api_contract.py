"""همه‌ی مسیرها و فیلدهای کلاینت Stard با مشخصات رسمی API (openapi.json منتشرشده‌ی Stard) مطابقت دارند."""
import json
import re
from pathlib import Path

import bot.stard_api as stard_api

SPEC = json.loads((Path(__file__).parent / "stard_openapi.json").read_text(encoding="utf-8"))


def _spec_paths():
    out = set()
    for p, methods in SPEC["paths"].items():
        norm = re.sub(r"\{[^}]+\}", "{}", p.removeprefix("/api/v1"))
        for m in methods:
            out.add((m.upper(), norm))
    return out


def test_client_paths_exist_in_spec():
    src = Path(stard_api.__file__).read_text(encoding="utf-8")
    calls = re.findall(r'_request\(\s*"(GET|POST|DELETE)",\s*f?"([^"]+)"', src)
    assert len(calls) >= 15
    spec = _spec_paths()
    for method, path in calls:
        norm = re.sub(r"\{[^}]+\}", "{}", path)
        if norm == "/openapi.json":
            continue
        assert (method, norm) in spec, f"{method} {path} در API رسمی نیست"


def test_order_bodies_use_spec_fields():
    schemas = SPEC["components"]["schemas"]
    assert set(schemas["OrderIn"]["properties"]) >= {"type", "product_id", "quantity", "recipient", "gift_message",
                                                     "quote_id", "pay_currency", "metadata"}
    assert set(schemas["BoostOrderIn"]["required"]) == {"recipient", "quantity", "duration"}
    assert set(schemas["QuoteIn"]["properties"]) >= {"type", "quantity", "product_id", "duration"}
    assert "outcome" in schemas["SimulateIn"]["required"]
