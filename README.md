# iCloud Message Cleanup

English | [繁體中文](README_zh.md)

This tool cleans up Messages files extracted from an iPhone backup. It also tries to fix missing photo thumbnails when the photo files are still there.

- Message text is kept.
- Attachment records with missing files are deleted.
- Files with no matching message attachment record are also deleted.

**Download all attachments first and keep an untouched full backup. Cleaning up before downloads finish can delete attachment records you still want.**

## 1. Download all attachments

On your iPhone, choose:

**iCloud → Messages in iCloud → Download Attachments → Download All Attachments**

Wait for the download to finish, then back up your iPhone with iMazing. Use this backup for both items in the next step.

**Only after confirming the full backup is complete:**

1. Turn off Messages in iCloud. If other iPhones, iPads, or Macs also sync messages, turn it off there too.
2. In iCloud's message settings, choose to delete the messages stored in iCloud so the old cloud data does not sync back.

This deletes messages stored in iCloud. Keep the full backup and leave sync off until you have restored and checked your phone.

## 2. Extract the files with iMazing

In iMazing, create an editable copy of this backup. Leave the original backup untouched. Open the editable copy's file system and copy out these two items:

| Where to look | What to copy | Where to put it on your computer |
| --- | --- | --- |
| `/HomeDomain/Library/SMS/` | `sms.db` | This project's `database/` folder |
| `/MediaDomain/Library/SMS/` | The whole `Attachments` folder | Inside this project's folder |

These are two different locations. Copy both. Do not rename or rearrange the attachment files.

The result should look like this:

```text
iCloud-Message-Device-Cleanup/
├── sms_cleanup.py
├── database/
│   └── sms.db
└── Attachments/
    └── Original folders and files, unchanged
```

**Save a separate copy of the whole project folder before continuing.** Leave that copy untouched so you can start over if needed.

## 3. Check first, without deleting anything

You need Python 3.10 or later. No extra packages are needed. Run `python3 --version` in Terminal to check your version.

In Terminal, type `cd ` (with a space), drag this project's folder into the window, then press Enter.

Run:

```sh
python3 sms_cleanup.py
```

This only checks the files. **It does not change them.** Look for these fields in the output:

| Field | Meaning |
| --- | --- |
| `missing_attachment_rows` | Records with no matching file, to be deleted |
| `orphan_files` | Files with no matching attachment record, to be deleted |
| `filename_updates` | Attachments whose file paths need filling in |
| `transfer_state_updates` | Attachments whose status needs fixing |
| `blockers` | Must be `[]` to continue. If it contains text, resolve the problem first. |

**Stop if the deletion counts look too high or you are not sure all attachments finished downloading.**

## 4. Apply the cleanup

Make sure you kept the full copy and the check results look right. Then run:

```sh
python3 sms_cleanup.py --backup-dir ./backup --apply
```

This **changes `database/sms.db` directly and deletes files in `Attachments/` that have no matching record**. Before changing anything, it saves the original database and files to be deleted in a new folder under `backup/`.

When it finishes:

- `"applied": true` means the changes were applied.
- `"applied": false` with `No changes needed.` means nothing needed changing.
- If you see an error, stop. Do not restore these results to your phone.

## 5. Check again

```sh
python3 sms_cleanup.py
```

The four counts in the table above should now be `0`, and `blockers` should be `[]`.

If `size_mismatches` is not `0`, some file sizes differ from their records. Check those separately. The tool does not repair file contents.

You have now cleaned up **the files on your computer**. Your phone has not changed. The tool does not put files back into iMazing or restore them to your iPhone.

## 6. Restore your phone, then turn sync back on

1. In iMazing's editable backup, replace the original files with the cleaned versions:
   - `database/sms.db` → `/HomeDomain/Library/SMS/sms.db`
   - `Attachments/` → `/MediaDomain/Library/SMS/Attachments/`. Replace the whole folder, rather than merging it, or deleted attachments may remain.
2. Use iMazing to restore this editable backup to your iPhone.
3. Open Messages on your phone and check the conversations and attachments.
4. Once everything looks right, turn Messages in iCloud back on to upload the cleaned messages. Wait for this upload to finish before turning sync back on for your other devices.

**Restoring returns your phone to the state it was in when you made the backup. Data added since then will not be kept.** If the result is wrong, leave Messages in iCloud off.

## What if the result is wrong?

Keep the current files, then make a fresh working copy from the untouched full copy you saved in step 2. Do not overwrite data added since then.

Cleanup cannot recover attachments that were never downloaded. Thumbnail repairs are not guaranteed on every iOS version. This is not an official Apple or iMazing tool.

## Other uses

- Save a check report: run `python3 sms_cleanup.py --report ./report.json`. The file must not already exist.
- Use files elsewhere: specify `sms.db` with `--db` and the `Attachments` folder with `--attachments`.
- See all options: run `python3 sms_cleanup.py --help`.
