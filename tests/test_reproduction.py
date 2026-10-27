"""Offline preparation safety/contracts; fixtures are not production assets."""
import argparse
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


r = module("reproduction")
w = module("prepare_workloads")


def archive(path, entries):
    with tarfile.open(path, "w:gz") as tar:
        for name, data in entries:
            item = tarfile.TarInfo(name)
            if data is None:
                item.type = tarfile.SYMTYPE
                item.linkname = "/etc/passwd"
                tar.addfile(item)
            else:
                item.size = len(data)
                tar.addfile(item, io.BytesIO(data))


class ReproductionTests(unittest.TestCase):
    def test_catalog_exact_source_versions_and_no_credentials(self):
        values = r.specs()
        for name, spec in values.items():
            if spec["kind"] == "source":
                self.assertRegex(spec["commit"], r"^[0-9a-f]{40}$")
            self.assertNotIn("@", spec["url"])
            self.assertFalse(Path(spec["destination"]).is_absolute())
        self.assertEqual(values["pg_hint_plan"]["commit"],
                         "dc21d9ba98924d80753194ca4bd8c00aafbdec6e")
        self.assertIn("tpcds-kit", values)

    def test_relative_paths_traversal_and_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "link").symlink_to("/tmp", target_is_directory=True)
            for name in ("../outside", "/tmp/outside", "a/../b", "a//b", "a\\b", "link/file"):
                with self.assertRaises(ValueError):
                    r.beneath(base, name)

    def test_nonempty_unmarked_cache_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "private"
            path.write_text("must remain")
            with self.assertRaisesRegex(ValueError, "nonempty"):
                r.load(Path(tmp), create=True)
            self.assertEqual(path.read_text(), "must remain")

    def test_archive_refuses_traversal_and_links_before_writing(self):
        for name, payload in (("root/../escape", b"x"), ("root/link", None)):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                tar, target = Path(tmp) / "source.tar.gz", Path(tmp) / "target"
                target.mkdir()
                archive(tar, [("root/valid", b"code"), (name, payload)])
                with self.assertRaises(ValueError):
                    r.extract_source(tar, target)
                self.assertEqual(list(target.iterdir()), [])

    def test_archive_extracts_source_without_git_or_upstream_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            tar, target = Path(tmp) / "source.tar.gz", Path(tmp) / "target"
            target.mkdir()
            archive(tar, [("upstream/module.py", b"print('real source')\n")])
            r.extract_source(tar, target)
            self.assertEqual((target / "module.py").read_bytes(), b"print('real source')\n")

    def test_cache_hash_corruption_and_unsealed_secrets_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            manifest = r.load(base, create=True)
            artifact = base / "wheelhouses/dev/test.whl"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"unit test file, not a wheel")
            r.record(base, manifest, "dev/test.whl", "wheel", artifact)
            r.verify(base, manifest)
            (base / "auth.json").write_text("not a credential, must still be refused")
            with self.assertRaisesRegex(ValueError, "Unsealed"):
                r.verify(base, manifest)
            (base / "auth.json").unlink()
            artifact.write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "corrupt"):
                r.verify(base, manifest)

    def test_empty_cache_does_not_claim_full_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = r.load(Path(tmp), create=True)
            with self.assertRaisesRegex(ValueError, "Incomplete"):
                r.verify(Path(tmp), manifest, require_complete=True)

    def test_lock_mismatch_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = r.load(Path(tmp), create=True)
            manifest["locks"]["external-sources.lock"] = "changed"
            r.save(Path(tmp), manifest)
            with self.assertRaisesRegex(ValueError, "lock identity"):
                r.load(Path(tmp))

    def test_models_import_only_exact_manifest_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, cache = Path(tmp) / "input", Path(tmp) / "cache"
            source.mkdir()
            payload = b"test fixture; NOT real model weights"
            (source / "model.pt").write_bytes(payload)
            (source / "auth.json").write_text("never copied")
            items = [{"path": "models/model.pt", "bytes": len(payload),
                      "sha256": hashlib.sha256(payload).hexdigest()}]
            args = argparse.Namespace(directory=str(cache), from_dir=str(source))
            with patch.object(r, "models", return_value=items):
                r.add_models(args)
                manifest = r.load(cache)
                r.verify(cache, manifest)
            self.assertFalse(any(p.name == "auth.json" for p in cache.rglob("*")))

    def test_bert_downloader_requests_only_official_hash_pinned_five_files(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(r, "download_file") as download:
            r.download_bert(argparse.Namespace(output_dir=tmp))
        expected = [item for item in r.models() if "/bert-base-uncased/" in item["path"]]
        self.assertEqual(len(expected), 5)
        self.assertEqual(download.call_count, 5)
        for call, item in zip(download.call_args_list, expected):
            self.assertTrue(call.args[0].startswith(
                "https://huggingface.co/google-bert/bert-base-uncased/resolve/"))
            self.assertEqual(call.args[2], item["sha256"])

    def test_existing_candidate_file_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, target = Path(tmp) / "src", Path(tmp) / "target"
            source.write_text("source")
            target.write_text("operator file")
            with self.assertRaisesRegex(ValueError, "Refusing"):
                r.copy_verified(source, target)
            self.assertEqual(target.read_text(), "operator file")

    def test_import_archive_checks_root_and_locked_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "source.tar.gz"
            archive(source, [("pinned-root/module.py", b"print('source')")])
            spec = {"kind": "source", "path": "sources/example.tar.gz",
                    "archiveRoot": "pinned-root", "sha256": "0" * 64}
            args = argparse.Namespace(directory=str(base / "cache"),
                                      name="example", from_file=str(source))
            with patch.object(r, "specs", return_value={"example": spec}):
                with self.assertRaisesRegex(ValueError, "SHA256"):
                    r.import_archive(args)
                spec["sha256"] = r.digest(source)
                spec["archiveRoot"] = "different-commit-root"
                with self.assertRaisesRegex(ValueError, "root differs"):
                    r.import_archive(args)

    def test_offline_source_install_idempotent_and_unknown_directory_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cache, target = base / "cache", base / "checkout"
            target.mkdir()
            manifest = r.load(cache, create=True)
            source = cache / "sources/example.tar.gz"
            source.parent.mkdir()
            archive(source, [("upstream/module.py", b"print('actual source')")])
            r.record(cache, manifest, "example", "source", source)
            spec = {"kind": "source", "path": "sources/example.tar.gz",
                    "destination": "third_party/example", "commit": "a" * 40}
            args = argparse.Namespace(directory=str(cache), target_root=str(target), only=None)
            with patch.object(r, "specs", return_value={"example": spec}):
                r.install_sources(args)
                r.install_sources(args)
                marker = target / "third_party/example/.keeninsight-offline-source.json"
                marker.unlink()
                with self.assertRaisesRegex(ValueError, "unmarked"):
                    r.install_sources(args)
            self.assertEqual((target / "third_party/example/module.py").read_bytes(),
                             b"print('actual source')")

    def test_offline_check_resolves_all_groups_with_no_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            r.load(base, create=True)
            args = argparse.Namespace(directory=str(base), python="chosen-target-python")
            with patch.object(r, "verify") as verify, patch.object(r.subprocess, "run") as run:
                r.offline_check(args)
            self.assertTrue(verify.call_args.kwargs["require_complete"])
            self.assertEqual(run.call_count, 3)
            for call in run.call_args_list:
                command = call.args[0]
                self.assertEqual(command[0], "chosen-target-python")
                self.assertIn("--no-index", command)
                self.assertIn("--ignore-installed", command)
                self.assertEqual(call.kwargs["env"]["PIP_NO_INDEX"], "1")

    def test_offline_monitoring_never_attempts_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "KEENINSIGHT_OFFLINE": "1",
                   "MONITORING_DOWNLOAD_DIR": tmp + "/archives",
                   "MONITORING_BIN_DIR": tmp + "/bin", "GRAFANA_HOME": tmp + "/grafana"}
            result = subprocess.run(["bash", str(ROOT / "monitoring/install_open_source.sh")],
                                    env=env, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("offline mode: missing or corrupt", result.stderr)
            self.assertNotIn("downloading", result.stderr)


class WorkloadTests(unittest.TestCase):
    def test_tp_plan_makes_no_database_call(self):
        args = argparse.Namespace(database="new_demo", warehouses=5, seed=42, apply=False)
        with patch.object(w.subprocess, "run") as run, patch.object(w.subprocess, "check_output") as read:
            w.tp(args)
            run.assert_not_called()
            read.assert_not_called()

    def test_tp_refuses_incompatible_warehouse_range(self):
        with self.assertRaisesRegex(ValueError, "5..50"):
            w.tp(argparse.Namespace(database="demo", warehouses=1, seed=42, apply=False))

    def test_database_confirmation_and_existing_schema_are_mandatory(self):
        args = argparse.Namespace(database="demo", confirm_database="different", apply=True)
        with patch.object(w.subprocess, "check_output") as request:
            with self.assertRaisesRegex(ValueError, "exactly match"):
                w.check_database(args, "keeninsight_tpcc")
            request.assert_not_called()
        args.confirm_database = "demo"
        with patch.object(w.subprocess, "check_output", return_value="1\n"):
            with self.assertRaisesRegex(ValueError, "existing schema"):
                w.check_database(args, "keeninsight_tpcc")

    def test_database_uri_cannot_bypass_confirmation(self):
        for value in ("postgresql://user@host/other", "dbname=other", "x; DROP DATABASE y"):
            with self.assertRaises(ValueError):
                w.psql(value)

    def test_tp_sql_has_no_destructive_reinitialization(self):
        sql = (ROOT / "scripts/tpcc_schema.sql").read_text()
        without_comments = "\n".join(line for line in sql.splitlines() if not line.startswith("--"))
        self.assertNotRegex(without_comments.upper(), r"\b(DROP|TRUNCATE|ALTER SYSTEM)\b")
        self.assertIn("CREATE SCHEMA keeninsight_tpcc;", without_comments)
        self.assertIn("d_next_o_id", without_comments)
        self.assertIn("generate_series(1,3000)", without_comments)

    def test_tpcds_copy_preserves_empty_last_column_and_backslashes(self):
        self.assertEqual(w.dat_record(b"a||\n"), b"a|\n")
        self.assertEqual(w.dat_record(b'1|a"b|C:\\path|\n'), b'1|a"b|C:\\path\n')
        with self.assertRaises(ValueError):
            w.dat_record(b"1|missing-trailing-delimiter\n")

    def test_schema_table_parser_does_not_use_filename_as_sql(self):
        self.assertEqual(w.table_names("create table a (\nx int\n);\nCREATE TABLE b (n int);"),
                         ["a", "b"])
        with self.assertRaises(ValueError):
            w.table_names("create table a (x int);\ncreate table a (x int);")


if __name__ == "__main__":
    unittest.main()
