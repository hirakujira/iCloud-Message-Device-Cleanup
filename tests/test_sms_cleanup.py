import ast
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import sms_cleanup as tool


PREFIX = "~/Library/SMS/Attachments/"


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.db = self.base / "SMS" / "sms.db"
        self.db.parent.mkdir()
        self.attachments = self.base / "SMS-Attachment" / "Attachments"
        self.attachments.mkdir(parents=True)
        with contextlib.closing(sqlite3.connect(self.db)) as c, c:
            c.executescript("""
                CREATE TABLE message (
                    ROWID INTEGER PRIMARY KEY, text TEXT, cache_has_attachments INTEGER,
                    error INTEGER DEFAULT 0
                );
                CREATE TABLE attachment (
                    ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT UNIQUE NOT NULL,
                    original_guid TEXT, filename TEXT, transfer_name TEXT, total_bytes INTEGER,
                    transfer_state INTEGER DEFAULT 0, ck_record_id TEXT,
                    preview_generation_state INTEGER DEFAULT 3, user_info BLOB
                );
                CREATE TABLE message_attachment_join (
                    message_id INTEGER REFERENCES message(ROWID) ON DELETE CASCADE,
                    attachment_id INTEGER REFERENCES attachment(ROWID) ON DELETE CASCADE,
                    UNIQUE(message_id, attachment_id)
                );
                CREATE TABLE sync_deleted_attachments (guid TEXT, recordID TEXT);
                CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, properties BLOB);
                CREATE TRIGGER before_delete_on_attachment BEFORE DELETE ON attachment
                BEGIN SELECT before_delete_attachment_path(OLD.ROWID,OLD.guid); END;
                CREATE TRIGGER after_delete_on_attachment AFTER DELETE ON attachment
                BEGIN SELECT delete_attachment_path(OLD.filename); END;
                CREATE TRIGGER add_to_sync_deleted_attachments AFTER DELETE ON attachment
                BEGIN INSERT INTO sync_deleted_attachments VALUES(OLD.guid,OLD.ck_record_id); END;
                CREATE TRIGGER after_delete_on_message_attachment_join
                AFTER DELETE ON message_attachment_join
                BEGIN
                    DELETE FROM attachment WHERE ROWID=OLD.attachment_id AND NOT EXISTS(
                        SELECT 1 FROM message_attachment_join WHERE attachment_id=OLD.attachment_id
                    );
                END;
            """)
            c.executemany("INSERT INTO message VALUES(?,?,1,0)", [
                (1, "synthetic-private-message-1"), (2, "synthetic-private-message-2"),
                (3, "synthetic-private-message-3"),
            ])
            rows = [
                (1, "at-existing", PREFIX + "aa/bb/at-existing/existing.png", "existing.png", 8, 5),
                (2, "at-photo", None, "PHOTO.JPG", 5, 0),
                (3, "at-live", None, "live.HEIC", 10, 0),
                (4, "at-missing", None, "missing.jpg", 4, 0),
                (5, "at-unlinked", None, "missing.png", 4, 0),
            ]
            for i, guid, filename, name, size, state in rows:
                c.execute(
                    "INSERT INTO attachment(ROWID,guid,original_guid,filename,transfer_name,"
                    "total_bytes,transfer_state,ck_record_id,user_info) VALUES(?,?,?,?,?,?,?,?,?)",
                    (i, guid, guid, filename, name, size, state, "synthetic-record-" + str(i),
                     b"synthetic-secret-blob"),
                )
            c.executemany("INSERT INTO message_attachment_join VALUES(?,?)", [(1, 1), (1, 2), (2, 3), (3, 4)])
            c.execute("INSERT INTO sync_deleted_attachments VALUES('at-orphan','synthetic-old-record')")
        self.file("at-existing", "existing.png", b"existing")
        self.file("at-photo", "photo.jpg", b"photo")
        self.file("at-live", "live.HEIC", b"heic")
        self.file("at-live", "live.MOV", b"movie!")
        self.orphan = self.file("at-orphan", "orphan.jpg", b"orphan")
        (self.attachments / ".DS_Store").write_bytes(b"finder-metadata")
        (self.attachments / "._metadata").write_bytes(b"apple-double")

    def file(self, guid, name, content):
        path = self.attachments / "aa" / "bb" / guid / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def run_tool(self, **kwargs):
        return tool.run(self.db, self.attachments, **kwargs)

    def execute(self, sql, params=()):
        with contextlib.closing(sqlite3.connect(self.db)) as c, c:
            c.create_function("delete_attachment_path", 1, lambda filename: None)
            c.create_function("before_delete_attachment_path", 2, lambda rowid, guid: None)
            c.execute(sql, params)

    def manifests(self):
        return list((self.db.parent / "sms-cleanup-backups").glob("*/manifest.json"))

    def test_audit_does_not_change_source_or_create_backup(self):
        before_hash = tool.sha256(self.db)
        before_files = tool.inventory(self.attachments)
        result = self.run_tool()
        self.assertFalse(result["applied"])
        self.assertEqual(result["plan"]["blockers"], [])
        summary = result["plan"]["summary"]
        self.assertEqual(summary["missing_attachment_rows"], 2)
        self.assertEqual(summary["orphan_files"], 1)
        self.assertEqual(summary["filename_updates"], 2)
        self.assertEqual(summary["transfer_state_updates"], 2)
        self.assertEqual(summary["size_mismatches"], 0)
        self.assertEqual(tool.sha256(self.db), before_hash)
        self.assertEqual(tool.inventory(self.attachments), before_files)
        self.assertFalse((self.db.parent / "sms-cleanup-backups").exists())

    def test_apply_preserves_messages_and_sync_tracking(self):
        before_hash = tool.sha256(self.db)
        result = self.run_tool(apply=True)
        self.assertTrue(result["applied"])
        backup = Path(result["backup_directory"])
        self.assertEqual(tool.sha256(backup / "sms.db.before"), before_hash)
        self.assertEqual((backup / "orphan-files" / self.orphan.relative_to(self.attachments)).read_bytes(), b"orphan")
        self.assertFalse(self.orphan.exists())
        self.assertTrue((self.attachments / ".DS_Store").exists())
        self.assertTrue((self.attachments / "._metadata").exists())
        with tool.read_database(self.db) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM message").fetchone()[0], 3)
            self.assertEqual(c.execute("SELECT count(*) FROM attachment").fetchone()[0], 3)
            self.assertEqual([r[0] for r in c.execute("SELECT transfer_state FROM attachment")], [5, 5, 5])
            self.assertEqual(c.execute("SELECT count(*) FROM sync_deleted_attachments").fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT filename FROM attachment WHERE ROWID=1").fetchone()[0], PREFIX + "aa/bb/at-existing/existing.png")
            self.assertEqual(c.execute("SELECT filename FROM attachment WHERE ROWID=2").fetchone()[0], PREFIX + "aa/bb/at-photo/photo.jpg")
            self.assertEqual(c.execute("SELECT filename FROM attachment WHERE ROWID=3").fetchone()[0], PREFIX + "aa/bb/at-live/live.HEIC")
            self.assertEqual(c.execute("SELECT cache_has_attachments FROM message WHERE ROWID=3").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT text FROM message WHERE ROWID=3").fetchone()[0], "synthetic-private-message-3")
            self.assertEqual([r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='trigger'")], [
                "before_delete_on_attachment", "after_delete_on_attachment",
                "after_delete_on_message_attachment_join", "add_to_sync_deleted_attachments",
            ])
            tool.check_database(c)
        manifest = json.loads((backup / "manifest.json").read_text())
        self.assertEqual(manifest["status"], "completed")
        self.assertEqual(manifest["original_db_sha256"], before_hash)
        self.assertEqual(manifest["updated_db_sha256"], tool.sha256(self.db))
        if os.name == "posix":
            self.assertEqual((backup.stat().st_mode & 0o777), 0o700)
            self.assertEqual(((backup / "sms.db.before").stat().st_mode & 0o777), 0o600)

    def test_second_apply_is_noop(self):
        self.run_tool(apply=True)
        before_hash = tool.sha256(self.db)
        count = len(self.manifests())
        result = self.run_tool(apply=True)
        self.assertFalse(result["applied"])
        self.assertEqual(tool.sha256(self.db), before_hash)
        self.assertEqual(len(self.manifests()), count)

    def test_backup_can_restore_all_original_data(self):
        before_hash = tool.sha256(self.db)
        original_files = {p.relative_to(self.attachments): p.read_bytes() for p in self.attachments.rglob("*") if p.is_file()}
        backup = Path(self.run_tool(apply=True)["backup_directory"])
        shutil.copyfile(backup / "sms.db.before", self.db)
        shutil.copytree(backup / "orphan-files", self.attachments, dirs_exist_ok=True)
        self.assertEqual(tool.sha256(self.db), before_hash)
        self.assertEqual({p.relative_to(self.attachments): p.read_bytes() for p in self.attachments.rglob("*") if p.is_file()}, original_files)

    def test_ambiguous_primary_file_blocks_apply(self):
        self.execute("UPDATE attachment SET transfer_name='unknown' WHERE ROWID=3")
        before_hash = tool.sha256(self.db)
        self.assertTrue(self.run_tool()["plan"]["blockers"])
        with self.assertRaises(tool.CleanupError):
            self.run_tool(apply=True)
        self.assertEqual(tool.sha256(self.db), before_hash)
        self.assertTrue(self.orphan.exists())

    def test_original_guid_alias_supported(self):
        self.execute("UPDATE attachment SET original_guid='at-photo',guid='at-renamed' WHERE ROWID=2")
        self.run_tool(apply=True)
        with tool.read_database(self.db) as c:
            self.assertTrue(c.execute("SELECT filename FROM attachment WHERE ROWID=2").fetchone()[0].endswith("/at-photo/photo.jpg"))

    def test_alias_collision_blocks_apply(self):
        self.execute("UPDATE attachment SET original_guid='at-photo' WHERE ROWID=3")
        with self.assertRaises(tool.CleanupError):
            self.run_tool(apply=True)

    def test_same_basename_in_other_guid_is_not_confused(self):
        self.file("at-orphan", "photo.jpg", b"another")
        result = self.run_tool(apply=True)
        self.assertEqual(result["plan"]["summary"]["orphan_files"], 2)
        self.assertEqual((self.attachments / "aa/bb/at-photo/photo.jpg").read_bytes(), b"photo")

    def test_existing_broken_filename_is_not_silently_overwritten(self):
        self.execute("UPDATE attachment SET filename=? WHERE ROWID=2", (PREFIX + "aa/bb/at-photo/not-here.jpg",))
        with self.assertRaises(tool.CleanupError):
            self.run_tool(apply=True)

    def test_unsupported_filename_and_path_traversal_block_apply(self):
        for name in ["/other/Attachments/aa/bb/at-photo/photo.jpg", PREFIX + "../photo.jpg"]:
            with self.subTest(name=name):
                self.execute("UPDATE attachment SET filename=? WHERE ROWID=2", (name,))
                with self.assertRaises(tool.CleanupError):
                    self.run_tool(apply=True)

    def test_missing_group_photo_reference_blocks_apply(self):
        self.execute("INSERT INTO chat VALUES(1,?)", (b"synthetic at-missing reference",))
        with self.assertRaises(tool.CleanupError):
            self.run_tool(apply=True)

    def test_unknown_transfer_state_preserved(self):
        self.execute("UPDATE attachment SET transfer_state=2 WHERE ROWID=2")
        result = self.run_tool(apply=True)
        self.assertTrue(result["plan"]["warnings"])
        with tool.read_database(self.db) as c:
            self.assertEqual(c.execute("SELECT transfer_state FROM attachment WHERE ROWID=2").fetchone()[0], 2)

    def test_size_mismatch_reported_not_rewritten(self):
        self.execute("UPDATE attachment SET total_bytes=999 WHERE ROWID=2")
        result = self.run_tool(apply=True)
        self.assertEqual(result["plan"]["summary"]["size_mismatches"], 1)
        with tool.read_database(self.db) as c:
            self.assertEqual(c.execute("SELECT total_bytes FROM attachment WHERE ROWID=2").fetchone()[0], 999)

    def test_backup_input_does_not_probe_sqlite_sidecars(self):
        sidecars = {str(self.db) + suffix for suffix in ("-wal", "-shm", "-journal")}
        original_lexists = os.path.lexists

        def lexists(path):
            self.assertNotIn(str(path), sidecars)
            return original_lexists(path)

        with mock.patch.object(tool.os.path, "lexists", side_effect=lexists):
            self.assertEqual(self.run_tool()["plan"]["blockers"], [])
            self.assertTrue(self.run_tool(apply=True)["applied"])

    @unittest.skipUnless(hasattr(os, "symlink"), "Requires symlink support")
    def test_symlink_file_and_directory_refused(self):
        for directory in (False, True):
            source = self.base / ("outside-dir" if directory else "outside-file")
            source.mkdir() if directory else source.write_bytes(b"outside")
            link = self.attachments / "link"
            link.symlink_to(source, target_is_directory=directory)
            with self.assertRaises(tool.CleanupError):
                self.run_tool(apply=True)
            link.unlink()

    def test_unreadable_directory_not_treated_as_missing(self):
        def walk(*args, **kwargs):
            kwargs["onerror"](PermissionError("synthetic permission denial"))
            return iter(())
        with mock.patch.object(tool.os, "walk", side_effect=walk):
            with self.assertRaises(tool.CleanupError):
                self.run_tool(apply=True)

    def test_wrong_root_and_all_missing_block_apply(self):
        with self.assertRaises(tool.CleanupError):
            tool.run(self.db, self.attachments.parent, apply=True)
        empty = self.base / "empty"
        empty.mkdir()
        with self.assertRaises(tool.CleanupError):
            tool.run(self.db, empty, apply=True)

    def test_backup_and_report_cannot_be_inside_attachments(self):
        with self.assertRaises(tool.CleanupError):
            self.run_tool(apply=True, backup_dir=self.attachments / "backup")
        with self.assertRaises(tool.CleanupError):
            self.run_tool(report=self.attachments / "report.json")

    def test_report_contains_no_message_text_or_secret_blobs(self):
        path = self.base / "audit.json"
        self.run_tool(report=path)
        text = path.read_text()
        self.assertNotIn("synthetic-private-message", text)
        self.assertNotIn("synthetic-secret-blob", text)
        self.assertFalse(json.loads(text)["applied"])
        with self.assertRaises(tool.CleanupError):
            self.run_tool(report=path, apply=True)

    def test_unknown_schema_and_dangling_joins_refused(self):
        self.execute("INSERT INTO message_attachment_join VALUES(999,1)")
        with self.assertRaises(tool.CleanupError):
            self.run_tool(apply=True)
        self.execute("DELETE FROM message_attachment_join WHERE message_id=999")
        self.execute("ALTER TABLE attachment RENAME TO unrelated")
        with self.assertRaises(tool.CleanupError):
            self.run_tool(apply=True)

    def test_unexpected_trigger_side_effects_never_reach_source(self):
        self.execute("CREATE TRIGGER unexpected AFTER UPDATE OF transfer_state ON attachment BEGIN UPDATE message SET text='changed'; END")
        before_hash = tool.sha256(self.db)
        with self.assertRaises(tool.CleanupError):
            self.run_tool(apply=True)
        self.assertEqual(tool.sha256(self.db), before_hash)
        self.assertTrue(self.orphan.exists())
        self.assertEqual(json.loads(self.manifests()[0].read_text())["status"], "failed_before_commit")

    def test_failure_after_database_replace_rolls_back(self):
        before_hash = tool.sha256(self.db)
        original_unlink = Path.unlink
        def fail_orphan(path, *args, **kwargs):
            if path == self.orphan:
                raise OSError("synthetic unlink failure")
            return original_unlink(path, *args, **kwargs)
        with mock.patch.object(Path, "unlink", new=fail_orphan):
            with self.assertRaises(OSError):
                self.run_tool(apply=True)
        self.assertEqual(tool.sha256(self.db), before_hash)
        self.assertEqual(self.orphan.read_bytes(), b"orphan")
        self.assertEqual(json.loads(self.manifests()[0].read_text())["status"], "rolled_back")

    def test_partial_file_deletion_rolls_back_all_data(self):
        second = self.file("at-orphan", "zz.jpg", b"second")
        before_hash = tool.sha256(self.db)
        original_unlink = Path.unlink
        def fail_second(path, *args, **kwargs):
            if path == second:
                raise OSError("synthetic second unlink failure")
            return original_unlink(path, *args, **kwargs)
        with mock.patch.object(Path, "unlink", new=fail_second):
            with self.assertRaises(OSError):
                self.run_tool(apply=True)
        self.assertEqual(tool.sha256(self.db), before_hash)
        self.assertEqual(self.orphan.read_bytes(), b"orphan")
        self.assertEqual(second.read_bytes(), b"second")

    def test_wal_mode_database_without_sidecars_supported(self):
        with contextlib.closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(c.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        self.run_tool(apply=True)
        for suffix in ("-wal", "-shm", "-journal"):
            self.assertFalse(Path(str(self.db) + suffix).exists())
        with tool.read_database(self.db) as c:
            tool.check_database(c)

    def test_rollback_preserves_deleted_file_permissions(self):
        second = self.file("at-orphan", "zz.jpg", b"second")
        os.chmod(self.orphan, 0o600)
        original_unlink = Path.unlink
        def fail_second(path, *args, **kwargs):
            if path == second:
                raise OSError("synthetic unlink failure")
            return original_unlink(path, *args, **kwargs)
        with mock.patch.object(Path, "unlink", new=fail_second):
            with self.assertRaises(OSError):
                self.run_tool(apply=True)
        if os.name == "posix":
            self.assertEqual(self.orphan.stat().st_mode & 0o777, 0o600)

    def test_absent_report_parent_refused_before_apply(self):
        before_hash = tool.sha256(self.db)
        with self.assertRaises(tool.CleanupError):
            self.run_tool(apply=True, report=self.base / "no-directory/report.json")
        self.assertEqual(tool.sha256(self.db), before_hash)
        self.assertFalse(self.manifests())

    def test_source_database_is_not_hashed_for_change_detection(self):
        original_sha256 = tool.sha256

        def sha256(path):
            self.assertNotEqual(path, self.db)
            return original_sha256(path)

        with mock.patch.object(tool, "sha256", side_effect=sha256):
            self.assertEqual(self.run_tool()["plan"]["blockers"], [])
            self.assertTrue(self.run_tool(apply=True)["applied"])

    def test_database_can_be_read_by_another_connection(self):
        with tool.read_database(self.db) as reader:
            self.assertEqual(reader.execute("SELECT count(*) FROM message").fetchone()[0], 3)
            self.assertTrue(self.run_tool(apply=True)["applied"])

    def test_missing_prefix_and_empty_filename_supported(self):
        self.execute("UPDATE attachment SET filename=NULL WHERE ROWID=1")
        self.execute("UPDATE attachment SET filename='' WHERE ROWID=2")
        result = self.run_tool(apply=True)
        self.assertEqual(result["plan"]["filename_prefix"], PREFIX)
        self.assertEqual(result["plan"]["summary"]["filename_updates"], 3)
        self.assertEqual(result["after"]["filename_updates"], 0)

    def test_mixed_prefix_requires_override_for_new_filenames(self):
        self.execute("UPDATE attachment SET filename=? WHERE ROWID=2", ("/var/mobile/Library/SMS/Attachments/aa/bb/at-photo/photo.jpg",))
        with self.assertRaises(tool.CleanupError):
            self.run_tool(apply=True)
        self.run_tool(apply=True, filename_prefix=PREFIX)

    def test_cli_summary_and_errors(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = tool.main(["--db", str(self.db), "--attachments", str(self.attachments)])
        self.assertEqual(code, 0)
        self.assertNotIn("synthetic-private", stdout.getvalue())
        self.assertEqual(json.loads(stdout.getvalue())["missing_attachment_rows"], 2)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(tool.main(["--db", str(self.base / "missing.db"), "--attachments", str(self.attachments)]), 2)
        self.assertTrue(stderr.getvalue().startswith("Error: "))

    def test_program_messages_are_english(self):
        source = ast.parse(Path(tool.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(source):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                with self.subTest(line=node.lineno):
                    self.assertTrue(node.value.isascii(), node.value)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), self.assertRaises(SystemExit) as stopped:
            tool.main(["--help"])
        self.assertEqual(stopped.exception.code, 0)
        self.assertIn("Check and repair an offline iOS Messages export", stdout.getvalue())
        self.assertIn("Apply all cleanup and repairs after backing up", stdout.getvalue())
        self.run_tool(apply=True)
        self.assertEqual(self.run_tool(apply=True)["reason"], "No changes needed.")

    def test_cli_interruption_message_is_english(self):
        stderr = io.StringIO()
        with mock.patch.object(tool, "run", side_effect=KeyboardInterrupt):
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(tool.main([]), 130)
        self.assertEqual(
            stderr.getvalue(),
            "Operation interrupted. If changes had started, check the status in the backup's manifest.json.\n",
        )

    def test_cli_defaults_use_script_directory_not_working_directory(self):
        script = self.base / "sms_cleanup.py"
        shutil.copyfile(tool.__file__, script)
        self.db = self.db.parent.rename(self.base / "database") / "sms.db"
        self.attachments = self.attachments.rename(self.base / "Attachments")
        before_hash = tool.sha256(self.db)
        before_files = tool.inventory(self.attachments)
        for args in ([], ["--apply"]):
            with self.subTest(args=args):
                result = subprocess.run(
                    [sys.executable, str(script), *args],
                    cwd=self.temporary.name + "/SMS-Attachment",
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                output = json.loads(result.stdout)
                self.assertEqual(output["applied"], bool(args))
                self.assertEqual(output["missing_attachment_rows"], 2)
                if not args:
                    self.assertEqual(tool.sha256(self.db), before_hash)
                    self.assertEqual(tool.inventory(self.attachments), before_files)
                    self.assertFalse(self.manifests())
                else:
                    self.assertEqual(output["after"]["missing_attachment_rows"], 0)
                    self.assertEqual(len(self.manifests()), 1)

    def test_cli_legacy_sms_directory_requires_explicit_database_path(self):
        self.attachments = self.attachments.rename(self.base / "Attachments")
        before_hash = tool.sha256(self.db)
        with mock.patch.object(tool, "__file__", str(self.base / "sms_cleanup.py")):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(tool.main(["--apply"]), 2)
            self.assertIn("database", stderr.getvalue())
            self.assertEqual(tool.sha256(self.db), before_hash)
            self.assertFalse(self.manifests())
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(tool.main(["--db", str(self.db)]), 0)

    def test_cli_missing_default_database_does_not_create_it(self):
        with mock.patch.object(tool, "__file__", str(self.base / "tool/sms_cleanup.py")):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(tool.main([]), 2)
        self.assertFalse((self.base / "tool").exists())


if __name__ == "__main__":
    unittest.main()
