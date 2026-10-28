"""Checks every Price List component in the rule table against the real API: each must match exactly one price.

Opt-in, since it needs AWS credentials (the Price List API itself is free):
    COSTAWARE_LIVE_PRICES=1 PYTHONPATH=src python3 -m unittest tests.test_price_specs_live
Run it when AWS renames a usage type, or after adding a component.
"""

import os
import shutil
import tempfile
import unittest

from costaware.classifier import load_rules
from costaware.pricing import _lookup, _queries

REGIONS = ("us-east-1", "eu-central-1")


def components():
    for name, service in load_rules()["services"].items():
        for rule in service.get("rules", []) + [service[k] for k in ("default_read", "default_write") if k in service]:
            if isinstance(rule.get("price"), list):
                for component in rule["price"]:
                    yield f"{name} {rule.get('op', 'default')} ({component['per']})", component


@unittest.skipUnless(os.environ.get("COSTAWARE_LIVE_PRICES") == "1", "set COSTAWARE_LIVE_PRICES=1 to query the Price List API")
class LivePriceSpecTest(unittest.TestCase):
    def test_every_component_has_one_price(self):
        aws = shutil.which("aws")
        with tempfile.TemporaryDirectory() as cache:
            env = {**os.environ, "COSTAWARE_CACHE_DIR": cache}
            for label, component in components():
                for region in REGIONS:
                    inputs = {"kind": "components", "components": [{**component, "quantity": 1}]}
                    service_code, filters, exclude = _queries(inputs, region)["component:0"]
                    with self.subTest(label, region=region):
                        self.assertIsNotNone(_lookup(service_code, filters, aws, env, exclude))


if __name__ == "__main__":
    unittest.main()
