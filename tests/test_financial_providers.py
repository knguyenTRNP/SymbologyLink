import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from symbologylink.cache import SQLiteCache
from symbologylink.engine import MatchEngine
from symbologylink.models import EntityMatchInput
from symbologylink.providers import MatchProvider, OpenFIGIProvider, ProviderCandidate, SECProvider


class Response:
    def __init__(self, payload):
        self.payload = payload
        self.headers = {}

    def __enter__(self): return self
    def __exit__(self, *args): return None
    def read(self): return json.dumps(self.payload).encode()


class SECProviderTests(unittest.TestCase):
    INDEX = {"fields": ["cik", "name", "ticker", "exchange"], "data": [[789019, "MICROSOFT CORP", "MSFT", "Nasdaq"], [320193, "APPLE INC", "AAPL", "Nasdaq"]]}
    SUBMISSION = {"cik": "789019", "name": "MICROSOFT CORP", "tickers": ["MSFT"], "exchanges": ["Nasdaq"], "formerNames": [{"name": "MICROSOFT CORPORATION"}]}

    def test_requires_contact_user_agent(self):
        with self.assertRaises(ValueError):
            SECProvider("symbologylink")

    def test_index_submission_and_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = SECProvider("Symbology Link test@example.com", SQLiteCache(Path(directory) / "cache.sqlite3"), retries=0)
            with patch("symbologylink.providers.urlopen", side_effect=[Response(self.INDEX), Response(self.SUBMISSION)]) as request:
                first = provider.search(EntityMatchInput("1", ticker="MSFT"))
                second = provider.search(EntityMatchInput("2", entityName="Microsoft Corp"))
            self.assertEqual(request.call_count, 2)
            self.assertEqual(first[0].entity_id, "sec:0000789019")
            self.assertEqual(first[0].security["ticker"], "MSFT")
            self.assertIn("MICROSOFT CORPORATION", first[0].aliases)
            self.assertEqual(second[0].identifiers["cik"], "0000789019")


class OpenFIGIProviderTests(unittest.TestCase):
    @staticmethod
    def item(index):
        return {"data": [{"figi": f"BBG{index:09d}", "name": f"COMPANY {index} INC", "ticker": f"T{index}", "exchCode": "US", "securityType2": "Common Stock", "shareClassFIGI": f"CLASS{index}"}]}

    def test_anonymous_mapping_batches_and_cache(self):
        records = [EntityMatchInput(str(index), ticker=f"T{index}", exchange="US") for index in range(6)]
        first_payload = [self.item(index) for index in range(5)]
        second_payload = [self.item(5)]
        with tempfile.TemporaryDirectory() as directory:
            provider = OpenFIGIProvider(cache=SQLiteCache(Path(directory) / "cache.sqlite3"), retries=0)
            with patch.object(provider._mapping_rate, "wait"), patch("symbologylink.providers.urlopen", side_effect=[Response(first_payload), Response(second_payload)]) as request:
                first = provider.search_batch(records)
                second = provider.search_batch(records)
            self.assertEqual(request.call_count, 2)
            self.assertEqual(len(first), 6)
            self.assertEqual(first["0"][0].security["figi"], "BBG000000000")
            self.assertEqual(second["5"][0].provider, "openfigi")

    def test_common_us_exchange_names_translate_to_figi_code(self):
        self.assertEqual(OpenFIGIProvider._mapping_job(EntityMatchInput("1", ticker="MSFT", exchange="NASDAQ"))["exchCode"], "US")
        self.assertEqual(OpenFIGIProvider._mapping_job(EntityMatchInput("2", ticker="IBM", exchange="NYSE"))["exchCode"], "US")


class StaticProvider(MatchProvider):
    def __init__(self, name, candidate):
        self.name, self.candidate = name, candidate

    def search(self, input_record, limit=20):
        return [self.candidate]


class ReconciliationTests(unittest.TestCase):
    def test_provider_candidates_merge_into_customer_identity(self):
        local = ProviderCandidate("customer:microsoft", "Microsoft Corporation", "issuer", "customer_security_master", domain="microsoft.com", identifiers={"ticker": "MSFT", "exchange": "NASDAQ"}, security={"internal_security_id": "security:msft", "ticker": "MSFT"})
        sec = ProviderCandidate("sec:0000789019", "MICROSOFT CORP", "issuer", "sec", identifiers={"cik": "0000789019", "ticker": "MSFT", "exchange": "NASDAQ"}, security={"ticker": "MSFT", "exchange": "NASDAQ"})
        figi = ProviderCandidate("openfigi:class-msft", "MICROSOFT CORP", "issuer", "openfigi", identifiers={"figi": "BBG000BPH459", "ticker": "MSFT", "exchange": "US"}, security={"figi": "BBG000BPH459", "ticker": "MSFT"})
        engine = MatchEngine([StaticProvider("customer_security_master", local), StaticProvider("sec", sec), StaticProvider("openfigi", figi)])
        result = engine.match(EntityMatchInput("1", entityName="Microsoft Corp", domain="microsoft.com", ticker="MSFT", exchange="NASDAQ", country="US"))
        self.assertEqual(result.matchedEntity["entityId"], "customer:microsoft")
        self.assertEqual(result.securityDecisionStatus, "review_required")
        self.assertIsNone(result.matchedSecurity)
        self.assertEqual(result.securityAlternatives[0].security["figi"], "BBG000BPH459")
        agreement = [item for item in result.evidence if item.type == "provider_agreement"]
        self.assertEqual(len(agreement), 1)
        self.assertEqual(set(agreement[0].input), {"customer_security_master", "sec", "openfigi"})


if __name__ == "__main__":
    unittest.main()
