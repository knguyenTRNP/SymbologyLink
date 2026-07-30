from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from symbologylink.cli import main
from symbologylink.engine import MatchEngine
from symbologylink.models import EntityMatchInput
from symbologylink.providers import LocalSecurityMasterProvider, TrustLevel
from symbologylink.relationship_master import (
    CustomerRelationshipMasterProvider,
    EntityRelationship,
    RelationshipType,
    RelationshipValidationError,
    validate_relationship_file,
)


class RelationshipMasterValidationTests(unittest.TestCase):
    def test_canonical_model_and_types(self):
        relationship = EntityRelationship(
            "r1", "child", "parent", RelationshipType.SUBSIDIARY_OF,
            "2020-01-01", None, 100, "customer", "source-1",
            TrustLevel.AUTHORITATIVE, True,
        )
        self.assertEqual(relationship.to_dict()["relationship_type"], "subsidiary_of")
        self.assertEqual(
            {item.value for item in RelationshipType},
            {"subsidiary_of", "owned_by", "brand_of", "division_of", "operated_by", "ultimate_parent_of", "minority_owned_by", "formerly_owned_by"},
        )

    def test_mapping_config_translates_customer_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            source = directory / "relationships.csv"
            source.write_text(
                "rel_id,subsidiary_id,owner_id,relation_type,start_date,end_date,ownership_pct\n"
                "rel-1,entity:child,entity:parent,owned_by,2020-01-01,,80\n",
                encoding="utf-8",
            )
            config = directory / "config.json"
            config.write_text(json.dumps({"relationship_master": {
                "path": source.name,
                "columns": {
                    "relationship_id": "rel_id", "child_entity_id": "subsidiary_id",
                    "parent_entity_id": "owner_id", "relationship_type": "relation_type",
                    "valid_from": "start_date", "valid_to": "end_date",
                    "ownership_percentage": "ownership_pct",
                },
                "trust_level": "authoritative",
            }}), encoding="utf-8")
            provider = CustomerRelationshipMasterProvider.from_config(config, entity_ids={"entity:child"})
        self.assertEqual(provider.relationships[0].parent_entity_id, "entity:parent")
        self.assertEqual(provider.relationships[0].ownership_percentage, 80)
        self.assertTrue(provider.validation_report.valid)
        self.assertEqual(provider.validation_report.issues[0].code, "unknown_entity_reference")
        self.assertEqual(provider.validation_report.issues[0].severity, "warning")
        self.assertEqual(provider.metadata()["validation_warnings"][0]["row_number"], 1)

    def test_validation_reports_all_rows_and_reasons(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "invalid.csv"
            source.write_text(
                "relationship_id,child_entity_id,parent_entity_id,relationship_type,valid_from,valid_to,ownership_percentage\n"
                "dup,A,A,minority_owned_by,2025-01-01,2020-01-01,120\n"
                "dup,B,C,owned_by,2020-01-01,,80\n"
                "cycle,C,B,owned_by,2020-01-01,,80\n"
                "overlap,B,D,subsidiary_of,2021-01-01,,100\n"
                "duplicate,B,D,subsidiary_of,2022-01-01,,100\n",
                encoding="utf-8",
            )
            report = validate_relationship_file(source)
            with self.assertRaises(RelationshipValidationError) as raised:
                CustomerRelationshipMasterProvider(source)
            output = io.StringIO()
            with patch("sys.argv", ["symbologylink", "relationships", "validate", "--input", str(source)]), redirect_stdout(output):
                exit_code = main()
        codes = {issue.code for issue in report.issues}
        self.assertFalse(report.valid)
        self.assertEqual(exit_code, 2)
        self.assertTrue({
            "duplicate_relationship_id", "self_reference", "invalid_period",
            "invalid_ownership_percentage", "parent_cycle", "overlapping_parent_periods",
            "duplicate_active_relationship",
        } <= codes)
        self.assertTrue(all(issue.row_number >= 1 for issue in report.issues if issue.code != "missing_mapping"))
        self.assertGreater(len(raised.exception.report.issues), 5)
        self.assertGreater(len(json.loads(output.getvalue())["errors"]), 5)

    def test_minority_relationship_never_retypes_the_child_as_subsidiary(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "minority.csv"
            source.write_text(
                "relationship_id,child_entity_id,parent_entity_id,relationship_type,ownership_percentage\n"
                "minority,entity:company,entity:investor,minority_owned_by,25\n",
                encoding="utf-8",
            )
            provider = CustomerRelationshipMasterProvider(source)
            relationship = provider.relationships[0]
        self.assertEqual(relationship.relationship_type, RelationshipType.MINORITY_OWNED_BY)
        self.assertNotEqual(relationship.relationship_type, RelationshipType.SUBSIDIARY_OF)


class RelationshipMasterResolutionTests(unittest.TestCase):
    @staticmethod
    def _files(directory: Path) -> tuple[Path, Path]:
        master = directory / "entities.csv"
        master.write_text(
            "internal_entity_id,canonical_name,entity_type,domain\n"
            "entity:wholefoods,Whole Foods Market Inc,subsidiary,wholefoodsmarket.com\n"
            "entity:amazon,Amazon.com Inc,issuer,amazon.com\n",
            encoding="utf-8",
        )
        relationships = directory / "relationships.csv"
        relationships.write_text(
            "relationship_id,child_entity_id,parent_entity_id,relationship_type,valid_from,valid_to,ownership_percentage,source,source_record_id,trust_level,is_direct\n"
            "wf-amazon,entity:wholefoods,entity:amazon,subsidiary_of,2017-08-28,,100,customer_master,deal-2017,authoritative,true\n",
            encoding="utf-8",
        )
        return master, relationships

    def test_pre_and_post_acquisition_parent_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            master, relationships = self._files(Path(directory))
            entities = LocalSecurityMasterProvider(master)
            relationship_provider = CustomerRelationshipMasterProvider(
                relationships, entity_ids={item.entity_id for item in entities.candidates},
            )
            engine = MatchEngine([entities, relationship_provider])
            before = engine.match(EntityMatchInput(
                "before", entityName="Whole Foods Market Inc", domain="wholefoodsmarket.com", observationDate="2016-01-01",
            ))
            after = engine.match(EntityMatchInput(
                "after", entityName="Whole Foods Market Inc", domain="wholefoodsmarket.com", observationDate="2018-01-01",
            ))
            inspected_before = relationship_provider.inspect("entity:wholefoods", "2016-01-01")
            inspected_after = relationship_provider.inspect("entity:wholefoods", "2018-01-01")

        self.assertIsNone(before.publicParent)
        self.assertEqual(before.parentStatus, "unknown")
        self.assertEqual(after.parentStatus, "verified")
        self.assertEqual(after.publicParent["entityId"], "entity:amazon")
        self.assertEqual(after.parentAlternatives[0]["sources"], ["customer_relationship_master"])
        edge = after.relationshipGraph["selectedEdges"][0]
        self.assertEqual(edge["relationshipType"], "subsidiary_of")
        self.assertEqual(edge["trustLevel"], "authoritative")
        self.assertEqual(edge["validFrom"], "2017-08-28")
        self.assertEqual(edge["provenance"][0]["sourceRecordId"], "deal-2017")
        verified_evidence = next(item for item in after.parentEvidence if item.type == "parent_verified")
        self.assertEqual(verified_evidence.candidate["trustLevel"], "authoritative")
        self.assertEqual(verified_evidence.candidate["relationshipTypes"], ["subsidiary_of"])
        self.assertEqual(verified_evidence.candidate["relationshipPeriods"][0]["validFrom"], "2017-08-28")
        self.assertEqual(verified_evidence.candidate["relationshipPeriods"][0]["source"], "customer_relationship_master")
        self.assertEqual(inspected_before["parents"], [])
        self.assertEqual(inspected_after["parents"][0]["parentEntityId"], "entity:amazon")

    def test_relationship_traversal_respects_max_depth(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "relationships.csv"
            source.write_text(
                "relationship_id,child_entity_id,parent_entity_id,relationship_type,valid_from,trust_level\n"
                "direct,entity:brand,entity:owner,brand_of,2020-01-01,authoritative\n"
                "ultimate,entity:owner,entity:group,subsidiary_of,2020-01-01,authoritative\n",
                encoding="utf-8",
            )
            provider = CustomerRelationshipMasterProvider(source)
            inspected = provider.inspect("entity:brand", "2024-01-01", max_depth=1)

        graph = inspected["graph"]
        self.assertFalse(graph["complete"])
        self.assertEqual(len(graph["edges"]), 1)
        self.assertEqual(graph["edges"][0]["toEntityId"], "entity:owner")
        self.assertEqual(graph["errors"], ["Customer relationship traversal stopped at max depth 1."])

    def test_cli_registration_and_schema_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            master, relationships = self._files(directory)
            records = directory / "records.csv"
            records.write_text("record_id,company_name,domain,observation_date\n1,Whole Foods Market Inc,wholefoodsmarket.com,2018-01-01\n", encoding="utf-8")
            mapping = directory / "mapping.json"
            mapping.write_text(json.dumps({"mapping": {
                "record_id": "recordId", "company_name": "entityName",
                "domain": "domain", "observation_date": "observationDate",
            }}), encoding="utf-8")
            relationship_config = directory / "relationship-config.json"
            relationship_config.write_text(json.dumps({"relationship_master": {
                "path": relationships.name,
                "columns": {field: field for field in (
                    "relationship_id", "child_entity_id", "parent_entity_id", "relationship_type",
                    "valid_from", "valid_to", "ownership_percentage", "source",
                    "source_record_id", "trust_level", "is_direct",
                )},
                "trust_level": "authoritative",
            }}), encoding="utf-8")
            output = directory / "results.jsonl"
            argv = [
                "symbologylink", "resolve", "--input", str(records), "--mapping", str(mapping),
                "--reference", str(master), "--providers", "customer_security_master,customer_relationship_master",
                "--relationship-config", str(relationship_config), "--cache", str(directory / "cache.sqlite3"),
                "--output", str(output),
            ]
            with patch("sys.argv", argv), redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 0)
            row = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
            inspect_output = io.StringIO()
            with patch("sys.argv", [
                "symbologylink", "relationships", "inspect", "--input", str(relationships),
                "--entity-id", "entity:wholefoods", "--date", "2018-01-01",
            ]), redirect_stdout(inspect_output):
                self.assertEqual(main(), 0)

        self.assertEqual(row["public_parent"]["status"], "verified")
        self.assertEqual(row["public_parent"]["canonical_id"], "entity:amazon")
        self.assertEqual(row["provider_metadata"]["customer_relationship_master"]["trust_level"], "authoritative")
        self.assertTrue(row["provider_metadata"]["customer_relationship_master"]["capabilities"]["relationship_effective_dates"])
        self.assertEqual(json.loads(inspect_output.getvalue())["parents"][0]["parentEntityId"], "entity:amazon")


if __name__ == "__main__":
    unittest.main()
