#!/usr/bin/env python3
"""Check and repair an offline iOS Messages export without connecting to your phone or iCloud."""

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import pickle
import shutil
import sqlite3
import stat
import sys
import tempfile
import uuid


class CleanupError(Exception):
    pass


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@contextmanager
def read_database(db):
    connection = sqlite3.connect(db.as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        yield connection
    finally:
        connection.close()


def check_database(connection):
    if [row[0] for row in connection.execute("PRAGMA integrity_check")] != ["ok"]:
        raise CleanupError("Database integrity check failed.")
    if list(connection.execute("PRAGMA foreign_key_check")):
        raise CleanupError("The database has foreign key errors. Fix them before applying cleanup.")


def columns(connection, table):
    return [row[1] for row in connection.execute("PRAGMA table_info(" + quote(table) + ")")]


def require_schema(connection):
    required = {
        "attachment": {"rowid", "guid", "filename", "transfer_name", "total_bytes", "transfer_state"},
        "message": {"rowid", "cache_has_attachments"},
        "message_attachment_join": {"message_id", "attachment_id"},
    }
    for table, names in required.items():
        if not names <= {name.lower() for name in columns(connection, table)}:
            raise CleanupError("Unsupported SMS schema: " + table)


def records(connection, table):
    for row in connection.execute("SELECT * FROM " + quote(table)):
        yield {key.lower(): row[key] for key in row.keys()}


def inventory(root):
    def unreadable(error):
        raise CleanupError("Cannot read the entire attachment directory. Unreadable files will not be treated as missing.") from error

    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=unreadable):
        for name in dirs + files:
            if (Path(directory) / name).is_symlink():
                raise CleanupError("The attachment directory contains a symlink. Stopping to avoid accessing files outside it.")
        for name in files:
            if name == ".DS_Store" or name.startswith("._"):
                continue
            path = Path(directory) / name
            if not path.is_file():
                raise CleanupError("The attachment directory contains a non-regular file.")
            info = path.stat()
            result[path.relative_to(root).as_posix()] = (info.st_size, info.st_mtime_ns, info.st_ino)
    return result


def split_filename(filename):
    if not isinstance(filename, str) or "/Attachments/" not in filename:
        return None
    head, relative = filename.rsplit("/Attachments/", 1)
    prefix = head + "/Attachments/"
    if not prefix.startswith(("/", "~/")) or not prefix.endswith("/Library/SMS/Attachments/"):
        return None
    if any(part in ("", ".", "..") for part in relative.split("/")):
        return None
    return prefix, relative


def analyze(connection, files, prefix=None):
    require_schema(connection)
    check_database(connection)
    if connection.execute(
        "SELECT count(*) FROM message_attachment_join j "
        "LEFT JOIN attachment a ON a.ROWID=j.attachment_id "
        "LEFT JOIN message m ON m.ROWID=j.message_id "
        "WHERE a.ROWID IS NULL OR m.ROWID IS NULL"
    ).fetchone()[0]:
        raise CleanupError("The database has invalid message-attachment links. Cleanup will not be applied.")
    attachments = list(records(connection, "attachment"))
    blockers, warnings = [], []
    owners, by_id = {}, {}
    for row in attachments:
        rowid, guid = row["rowid"], row["guid"]
        if not isinstance(rowid, int) or not isinstance(guid, str) or not guid:
            raise CleanupError("Unsupported attachment ID or GUID format.")
        by_id[rowid] = row
        for alias in {guid, row.get("original_guid") or guid}:
            if not isinstance(alias, str) or "/" in alias or alias in (".", ".."):
                raise CleanupError("Unsupported attachment GUID format.")
            if alias in owners and owners[alias] != rowid:
                blockers.append("Multiple attachments share a GUID. Cannot safely determine which attachment owns the files.")
            owners[alias] = rowid
    candidates = {rowid: [] for rowid in by_id}
    orphan_files = []
    for relative in sorted(files):
        parts = relative.split("/")
        if len(parts) != 4:
            blockers.append("Unsupported attachment directory layout. Expected <level1>/<level2>/<GUID>/<filename>.")
        owner = owners.get(parts[-2]) if len(parts) >= 2 else None
        if owner is None:
            orphan_files.append(relative)
        else:
            candidates[owner].append(relative)
    existing_prefixes = set()
    missing, pending_names, states, mismatches = [], [], [], []
    for rowid, row in by_id.items():
        paths = candidates[rowid]
        filename = row["filename"]
        if filename is not None and not isinstance(filename, str):
            raise CleanupError("The filename field is not text.")
        parsed = split_filename(filename)
        if filename and parsed is None:
            blockers.append(f"Attachment {rowid} has an unsupported filename format. Data will be kept and changes will not be applied.")
        if parsed:
            existing_prefixes.add(parsed[0])
            if parsed[1] in files and parsed[1] not in paths:
                blockers.append(f"Attachment {rowid}'s filename points to a file owned by a different GUID.")
        if not paths:
            missing.append({"attachment_id": rowid, "guid": row["guid"], "filename": filename})
            continue
        if filename:
            if not parsed or parsed[1] not in paths:
                blockers.append(f"Attachment {rowid} has files, but its existing filename points elsewhere. It will not be overwritten.")
        else:
            exact = [p for p in paths if Path(p).name == row["transfer_name"]]
            folded = [
                p for p in paths
                if isinstance(row["transfer_name"], str)
                and Path(p).name.casefold() == row["transfer_name"].casefold()
            ]
            if len(exact) == 1:
                selected, method = exact[0], "exact_transfer_name"
            elif len(folded) == 1:
                selected, method = folded[0], "case_insensitive_transfer_name"
            elif len(paths) == 1:
                selected, method = paths[0], "unique_file_under_guid"
            else:
                blockers.append(f"Attachment {rowid} has multiple files. Cannot identify the main file.")
                continue
            pending_names.append({"attachment_id": rowid, "relative_path": selected, "matching_method": method})
        if row["transfer_state"] == 0:
            states.append(rowid)
        elif row["transfer_state"] != 5:
            warnings.append(f"Attachment {rowid} has a transfer_state other than 0 or 5. The value will be kept.")
        actual_bytes = sum(files[path][0] for path in paths)
        if actual_bytes != row["total_bytes"]:
            mismatches.append({
                "attachment_id": rowid, "db_total_bytes": row["total_bytes"],
                "actual_bytes": actual_bytes,
            })
    if prefix is not None and not split_filename(prefix + "example"):
        raise CleanupError("--filename-prefix must be an absolute or ~/ path ending in /Library/SMS/Attachments/.")
    if prefix is None:
        if len(existing_prefixes) > 1 and pending_names:
            blockers.append("Existing filenames have multiple prefixes. Specify --filename-prefix.")
        prefix = sorted(existing_prefixes)[0] if existing_prefixes else "~/Library/SMS/Attachments/"
    for item in pending_names:
        item["filename"] = prefix + item["relative_path"]
    if (attachments or files) and not any(candidates.values()):
        blockers.append("No attachments match any files. Check the directory. Refusing to delete all data.")
    missing_ids = {item["attachment_id"] for item in missing}
    if missing_ids and "properties" in {name.lower() for name in columns(connection, "chat")}:
        properties = [row["properties"] for row in records(connection, "chat") if row["properties"]]
        for alias, rowid in owners.items():
            if rowid in missing_ids and any(
                alias.encode() in (value if isinstance(value, bytes) else str(value).encode())
                for value in properties
            ):
                blockers.append(f"Missing attachment {rowid} is still referenced by chat.properties. Manual repair is required.")
    joins = list(records(connection, "message_attachment_join"))
    affected = {row["message_id"] for row in joins if row["attachment_id"] in missing_ids}
    remaining_message_ids = {row["message_id"] for row in joins if row["attachment_id"] not in missing_ids}
    # 快取僅更新受刪除影響的訊息，不順帶重寫其他訊息。
    caches = {message_id: int(message_id in remaining_message_ids) for message_id in affected}
    return {
        "summary": {
            "attachment_rows": len(attachments), "attachment_files": len(files),
            "missing_attachment_rows": len(missing), "orphan_files": len(orphan_files),
            "filename_updates": len(pending_names), "transfer_state_updates": len(states),
            "message_join_deletions": sum(row["attachment_id"] in missing_ids for row in joins),
            "size_mismatches": len(mismatches),
            "messages": connection.execute("SELECT count(*) FROM message").fetchone()[0],
        },
        "filename_prefix": prefix, "missing_attachments": missing, "orphan_files": orphan_files,
        "filename_updates": pending_names, "transfer_state_updates": states,
        "message_cache_updates": caches, "size_mismatches": mismatches,
        "warnings": sorted(set(warnings)), "blockers": sorted(set(blockers)),
    }


def schema(connection):
    return [tuple(row) for row in connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
    )]


def expected_hashes(connection, plan=None):
    missing = {item["attachment_id"] for item in plan["missing_attachments"]} if plan else set()
    names = {item["attachment_id"]: item["filename"] for item in plan["filename_updates"]} if plan else {}
    states = set(plan["transfer_state_updates"]) if plan else set()
    caches = plan["message_cache_updates"] if plan else {}
    result = {}
    for entry in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        table = entry[0]
        row_hashes = []
        for row in records(connection, table):
            if table == "attachment":
                rowid = row["rowid"]
                if rowid in missing:
                    continue
                if rowid in names:
                    row["filename"] = names[rowid]
                if rowid in states:
                    row["transfer_state"] = 5
            elif table == "message_attachment_join" and row["attachment_id"] in missing:
                continue
            elif table == "message" and row["rowid"] in caches:
                row["cache_has_attachments"] = caches[row["rowid"]]
            row_hashes.append(hashlib.sha256(pickle.dumps(tuple(row.values()), protocol=4)).digest())
        # 比較所有邏輯資料，不依賴 ROWID 或資料表的實體掃描順序。
        result[table] = hashlib.sha256(b"".join(sorted(row_hashes))).hexdigest()
    return result


def write_json(path, data):
    with path.open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def replace_manifest(path, data):
    temporary = path.with_name(".manifest-" + uuid.uuid4().hex + ".json")
    try:
        write_json(temporary, data)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def apply_changes(db, root, plan, files, backup_base):
    if plan["blockers"]:
        raise CleanupError("Cannot apply changes: " + "; ".join(plan["blockers"]))
    changed = any(plan[key] for key in (
        "missing_attachments", "orphan_files", "filename_updates", "transfer_state_updates"
    ))
    if not changed:
        return {"applied": False, "reason": "No changes needed.", "backup_directory": None}
    if backup_base.is_relative_to(root):
        raise CleanupError("The backup directory cannot be inside the attachment directory.")
    original_mode = stat.S_IMODE(db.stat().st_mode)
    backup_base.mkdir(parents=True, exist_ok=True)
    backup = backup_base / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
    backup.mkdir(mode=0o700)
    before_db = backup / "sms.db.before"
    shutil.copyfile(db, before_db)
    os.chmod(before_db, 0o600)
    original_hash = sha256(before_db)
    with read_database(before_db) as source:
        expected = expected_hashes(source, plan)
        before_schema = schema(source)
    orphan_hashes, orphan_metadata = {}, {}
    for relative in plan["orphan_files"]:
        source, destination = root / relative, backup / "orphan-files" / relative
        info = source.stat()
        orphan_metadata[relative] = {
            "mode": stat.S_IMODE(info.st_mode), "atime_ns": info.st_atime_ns, "mtime_ns": info.st_mtime_ns,
        }
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(source, destination)
        os.chmod(destination, 0o600)
        copied_hash = sha256(destination)
        if sha256(source) != copied_hash:
            raise CleanupError("An attachment changed during backup. Stopping.")
        orphan_hashes[relative] = copied_hash
    manifest = {
        "version": 1, "status": "preparing", "database": str(db), "attachments": str(root),
        "original_db_sha256": original_hash, "original_db_mode": original_mode,
        "orphan_sha256": orphan_hashes, "orphan_metadata": orphan_metadata, "plan": plan,
    }
    manifest_path = backup / "manifest.json"
    write_json(manifest_path, manifest)
    fd, temporary = tempfile.mkstemp(prefix=".sms-cleanup-", suffix=".db", dir=db.parent)
    os.close(fd)
    work = Path(temporary)
    replaced, removed = False, []
    try:
        shutil.copyfile(before_db, work)
        with closing(sqlite3.connect(work)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            # 待刪紀錄的檔案不存在；檔案操作由工具在備份後處理。
            connection.create_function("delete_attachment_path", 1, lambda filename: None)
            connection.create_function("before_delete_attachment_path", 2, lambda rowid, guid: None)
            connection.execute("BEGIN IMMEDIATE")
            trigger = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='add_to_sync_deleted_attachments'"
            ).fetchone()
            if trigger:
                connection.execute("DROP TRIGGER add_to_sync_deleted_attachments")
            connection.execute("CREATE TEMP TABLE cleanup_missing(id INTEGER PRIMARY KEY)")
            connection.executemany("INSERT INTO cleanup_missing VALUES(?)", [
                (item["attachment_id"],) for item in plan["missing_attachments"]
            ])
            connection.execute(
                "DELETE FROM message_attachment_join WHERE attachment_id IN (SELECT id FROM cleanup_missing)"
            )
            connection.execute("DELETE FROM attachment WHERE ROWID IN (SELECT id FROM cleanup_missing)")
            if trigger:
                connection.execute(trigger[0])
            connection.executemany(
                "UPDATE attachment SET filename=? WHERE ROWID=? AND (filename IS NULL OR filename='')",
                [(item["filename"], item["attachment_id"]) for item in plan["filename_updates"]],
            )
            connection.executemany(
                "UPDATE attachment SET transfer_state=5 WHERE ROWID=? AND transfer_state=0",
                [(rowid,) for rowid in plan["transfer_state_updates"]],
            )
            connection.executemany(
                "UPDATE message SET cache_has_attachments=? WHERE ROWID=?",
                [(value, rowid) for rowid, value in plan["message_cache_updates"].items()],
            )
            check_database(connection)
            if schema(connection) != before_schema or expected_hashes(connection) != expected:
                raise CleanupError("Unexpected database changes detected. Stopping before applying changes.")
            connection.commit()
            if connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal":
                if connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] != 0:
                    raise CleanupError("WAL checkpoint failed on the working copy.")
        with read_database(work) as staged:
            check_database(staged)
            if expected_hashes(staged) != expected:
                raise CleanupError("Working copy validation failed.")
        updated_hash = sha256(work)
        manifest.update(status="prepared", updated_db_sha256=updated_hash)
        replace_manifest(manifest_path, manifest)
        if inventory(root) != files:
            raise CleanupError("The attachments changed during checking. Stopping before applying changes.")
        os.chmod(work, original_mode)
        os.replace(work, db)
        replaced = True
        for relative, file_hash in orphan_hashes.items():
            path = root / relative
            if sha256(path) != file_hash:
                raise CleanupError("A file scheduled for deletion changed. Rolling back.")
            path.unlink()
            removed.append(relative)
        with read_database(db) as final:
            check_database(final)
            if expected_hashes(final) != expected or schema(final) != before_schema:
                raise CleanupError("Database validation failed after applying changes. Rolling back.")
            final_files = inventory(root)
            if final_files != {name: info for name, info in files.items() if name not in orphan_hashes}:
                raise CleanupError("Attachment validation failed after applying changes. Rolling back.")
            remaining = analyze(final, final_files, plan["filename_prefix"])
            if remaining["blockers"] or any(remaining["summary"][name] for name in (
                "missing_attachment_rows", "orphan_files", "filename_updates", "transfer_state_updates"
            )):
                raise CleanupError("Some repairs remain incomplete after applying changes. Rolling back.")
        manifest.update(status="completed", after=remaining["summary"])
        replace_manifest(manifest_path, manifest)
        return {
            "applied": True, "backup_directory": str(backup), "updated_db_sha256": updated_hash,
            "after": remaining["summary"],
        }
    except BaseException:
        manifest["status"] = "failed_before_commit"
        try:
            if replaced:
                if sha256(before_db) != original_hash:
                    raise CleanupError("The database backup checksum does not match. The source will not be overwritten.")
                shutil.copyfile(before_db, work)
                os.chmod(work, original_mode)
                os.replace(work, db)
                for relative in removed:
                    destination = root / relative
                    if destination.exists():
                        raise CleanupError("Another file exists at the restore location. Restore manually from the backup.")
                    saved = backup / "orphan-files" / relative
                    if sha256(saved) != orphan_hashes[relative]:
                        raise CleanupError("The attachment backup checksum does not match. Manual recovery is required.")
                    shutil.copyfile(saved, destination)
                    info = orphan_metadata[relative]
                    os.chmod(destination, info["mode"])
                    os.utime(destination, ns=(info["atime_ns"], info["mtime_ns"]))
                manifest["status"] = "rolled_back"
        except BaseException as rollback_error:
            manifest["status"] = "manual_recovery_required"
            raise CleanupError("Automatic rollback could not be completed. Keep the backup: " + str(backup)) from rollback_error
        finally:
            replace_manifest(manifest_path, manifest)
        raise
    finally:
        if work.exists():
            work.unlink()
        # 僅清除工具自行建立的工作副本側錄檔，不動來源的側錄檔。
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(work) + suffix)
            if sidecar.exists():
                sidecar.unlink()


def run(db, attachments, apply=False, report=None, backup_dir=None, filename_prefix=None):
    db_input = Path(db).expanduser()
    if db_input.is_symlink():
        raise CleanupError("The database cannot be a symlink.")
    db, root = db_input.resolve(strict=True), Path(attachments).expanduser().resolve(strict=True)
    if not db.is_file() or not root.is_dir() or db.is_relative_to(root):
        raise CleanupError("Specify an sms.db file outside the Attachments directory and a separate Attachments directory.")
    report = Path(report).expanduser().absolute() if report else None
    if report:
        if os.path.lexists(report) or report.resolve().is_relative_to(root):
            raise CleanupError("The report path already exists or is inside the attachment directory. No file will be overwritten.")
        if not report.parent.is_dir():
            raise CleanupError("The report's parent directory does not exist.")
    files = inventory(root)
    with read_database(db) as connection:
        plan = analyze(connection, files, filename_prefix)
    if inventory(root) != files:
        raise CleanupError("The attachments changed during checking. Close other programs and try again.")
    result = {"applied": False, "plan": plan}
    if apply:
        base = Path(backup_dir).expanduser().resolve() if backup_dir else db.parent / "sms-cleanup-backups"
        result.update(apply_changes(db, root, plan, files, base))
    if report:
        try:
            write_json(report, result)
        except OSError as error:
            if result["applied"]:
                raise CleanupError("Changes were applied, but the report could not be written. Backup: " + result["backup_directory"]) from error
            raise
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    base = Path(__file__).resolve().parent
    parser.add_argument("--db", default=base / "database" / "sms.db", help="Offline sms.db file; default: database/sms.db beside the script")
    parser.add_argument("--attachments", default=base / "Attachments", help="Attachments directory; default: Attachments/ beside the script")
    parser.add_argument("--apply", action="store_true", help="Apply all cleanup and repairs after backing up")
    parser.add_argument("--report", help="Save the full JSON report without overwriting an existing file")
    parser.add_argument("--backup-dir", help="Parent directory for backups; default: sms-cleanup-backups beside the database")
    parser.add_argument("--filename-prefix", help="Prefix for new paths; default: existing format, or ~/Library/SMS/Attachments/ if none exists")
    args = parser.parse_args(argv)
    try:
        result = run(args.db, args.attachments, args.apply, args.report, args.backup_dir, args.filename_prefix)
        output = {key: value for key, value in result.items() if key != "plan"}
        output.update(result["plan"]["summary"])
        output["warnings"] = result["plan"]["warnings"]
        output["blockers"] = result["plan"]["blockers"]
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 2 if output["blockers"] else 0
    except (CleanupError, OSError, sqlite3.Error, ValueError) as error:
        print("Error: " + str(error), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Operation interrupted. If changes had started, check the status in the backup's manifest.json.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
