"""Regression checks for data loss, duplicate papers and incorrect DOI assignment."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError

MODULE = Path(__file__).resolve().parents[1] / "scripts/update_publications.py"
spec = importlib.util.spec_from_file_location("updater", MODULE)
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


def work(code=123, kind="journal-article"):
    return {"put-code": code, "visibility": "public", "type": kind,
            "title": {"title": {"value": "Bionic perception via a magnetized spring-like sensor"}},
            "publication-date": {"year": {"value": "2026"}},
            "journal-title": {"value": "ACS Nano"}, "external-ids": {"external-id": None},
            "contributors": {"contributor": [{"credit-name": {"value": "Liang, Yuanzhe"}}, {"credit-name": {"value": "Lei, Ming"}}]},
            "citation": {"citation-type": "bibtex", "citation-value": "@article{key, title={Sensor}, author={Liang, Yuanzhe and Lei, Ming}, volume={20}, number={2}, pages={12--21}, year={2026}}"}}


def publication():
    return updater.publication_from_work(work())


class PublicationSyncTests(unittest.TestCase):
    def test_existing_link_anchor_and_manual_corrections_survive_metadata_update(self):
        old = publication()
        old.update(id="existing-link", title="Bionic Perception via Magnetized Spring-like Sensor", year=2025,
                   doi="10.1234/retained", url="https://doi.org/10.1234/retained", orcid_put_codes=[],
                   overrides={"journal": "ACS Nano", "year": 2025})
        merged = updater.merge_publications([old], [publication()])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["id"], "existing-link")
        self.assertEqual(merged[0]["doi"], "10.1234/retained")
        self.assertEqual(merged[0]["year"], 2026)
        self.assertEqual(updater.effective(merged[0])["year"], 2025)

    def test_null_external_ids_and_bibtex_accents(self):
        raw = work()
        raw["title"]["title"]["value"] = r"Fabry--P{\'e}rot sensor"
        p = updater.publication_from_work(raw)
        self.assertEqual(p["title"], "Fabry–Pérot sensor")
        self.assertEqual(p["authors"], ["Yuanzhe Liang", "Ming Lei"])
        self.assertEqual((p["volume"], p["issue"], p["pages"]), ("20", "2", "12–21"))

    def test_parent_work_doi_is_not_used_for_the_paper(self):
        raw = work()
        raw["external-ids"]["external-id"] = [{"external-id-type": "doi", "external-id-value": "10.1234/book", "external-id-relationship": "part-of"}]
        self.assertEqual(updater.publication_from_work(raw)["doi"], "")

    def test_preferred_orcid_source_and_all_group_identifiers_are_retained(self):
        group = {"group": [{"work-summary": [
            {"put-code": 122, "visibility": "public", "display-index": "0"},
            {"put-code": 123, "visibility": "public", "display-index": "1"}]}]}
        with patch.object(updater, "get_json", side_effect=[group, {"bulk": [{"work": work()}]}]):
            result = updater.read_public_works("0009-0000-9869-4474")
        self.assertEqual(result[0]["_group_codes"], [122, 123])

    def test_partial_orcid_response_is_rejected(self):
        group = {"group": [{"work-summary": [{"put-code": 123, "visibility": "public"}]}]}
        with patch.object(updater, "get_json", side_effect=[group, {"bulk": [{"error": "unavailable"}]}]):
            with self.assertRaises(ValueError):
                updater.read_public_works("0009-0000-9869-4474")

    def test_empty_orcid_response_is_rejected(self):
        with patch.object(updater, "get_json", return_value={"group": []}):
            with self.assertRaises(ValueError):
                updater.read_public_works("0009-0000-9869-4474")

    def test_doi_lookup_rejects_wrong_papers_and_ambiguous_results(self):
        p = publication()
        good = {"title": [p["title"]], "author": [{"given": "Yuanzhe", "family": "Liang"}],
                "published": {"date-parts": [[2026]]}, "DOI": "10.1234/right"}
        self.assertEqual(updater.crossref_match(p, [good]), "10.1234/right")
        for key, value in [("title", ["A different sensor"]), ("author", [{"given": "Another", "family": "Author"}]),
                           ("published", {"date-parts": [[2010]]})]:
            bad = copy.deepcopy(good)
            bad[key] = value
            self.assertEqual(updater.crossref_match(p, [bad]), "")
        duplicate = dict(good, DOI="10.1234/other-version")
        self.assertEqual(updater.crossref_match(p, [good, duplicate]), "")

    def test_imported_text_and_non_http_links_cannot_inject_markup(self):
        p = publication()
        p.update(title='<script>alert(1)</script>', authors=['<img src=x onerror=alert(1)>', 'Ming Lei'], url='javascript:alert(1)')
        result = updater.render([p], "0009-0000-9869-4474", "Ming Lei")
        self.assertNotIn("<script>", result)
        self.assertNotIn("<img", result)
        self.assertNotIn("javascript:", result)
        self.assertIn("ORCID记录", result)
        self.assertIn("<strong>Ming Lei</strong>", result)

    def test_missing_sync_markers_are_rejected(self):
        with self.assertRaises(ValueError):
            updater.update_html("<html>original</html>", "new")

    def fixture_root(self, root):
        (root / "data").mkdir()
        data = {"schema_version": 1, "orcid": "0009-0000-9869-4474", "owner_name": "Ming Lei",
                "publications": [publication()], "include_types": ["journal-article", "conference-paper"]}
        (root / "data/publications.json").write_text(json.dumps(data), encoding="utf-8")
        (root / "publications.html").write_text("<html>UNCHANGED_HEADER" + updater.START + "old" + updater.END + "UNCHANGED_SCRIPT</html>", encoding="utf-8")

    def test_network_failure_preserves_both_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture_root(root)
            files = [root / "data/publications.json", root / "publications.html"]
            before = [p.read_bytes() for p in files]
            with patch.object(updater, "read_public_works", side_effect=URLError("offline")):
                with self.assertRaises(URLError):
                    updater.run(root)
            self.assertEqual(before, [p.read_bytes() for p in files])

    def test_thesis_is_excluded_and_unmatched_existing_papers_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture_root(root)
            new = work(124)
            new["title"]["title"]["value"] = "A newly published paper"
            with patch.object(updater, "read_public_works", return_value=[new, work(125, "dissertation-thesis")]):
                data = updater.run(root, enrich=False)
            self.assertEqual({p["id"] for p in data["publications"]}, {"orcid-123", "orcid-124"})

    def test_offline_render_is_repeatable_and_preserves_surrounding_page(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture_root(root)
            updater.run(root, offline=True)
            before = (root / "publications.html").read_bytes()
            with patch.object(updater, "get_json", side_effect=AssertionError("No network permitted")):
                updater.run(root, offline=True)
            self.assertEqual(before, (root / "publications.html").read_bytes())
            self.assertIn(b"UNCHANGED_HEADER", before)
            self.assertIn(b"UNCHANGED_SCRIPT", before)


if __name__ == "__main__":
    unittest.main()
