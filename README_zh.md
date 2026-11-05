# iCloud 訊息清理

[English](README.md) | 繁體中文

這個工具會清理從 iPhone 備份取出的「訊息」檔案，並嘗試修復照片有檔案、卻沒有顯示縮圖的問題。

- 訊息文字會保留。
- 找不到檔案的附件紀錄會刪除。
- 沒有對應訊息附件紀錄的檔案，也會刪除。

**請先下載所有附件，並保留一份沒動過的完整備份。附件沒下載完就清理，可能會刪掉你還想保留的附件紀錄。**

## 1. 下載所有附件

在 iPhone 上依序選擇：

**iCloud → iCloud 雲端訊息 → 下載附件 → 下載所有附件**

等下載完成，再用 iMazing 備份 iPhone。後面要取出的檔案，都使用這次備份。

**確認完整備份已完成後，才做下面兩件事：**

1. 關閉 iCloud 的「雲端訊息」同步。其他 iPhone、iPad 或 Mac 若也有同步訊息，請先關閉。
2. 在 iCloud 的訊息設定中選擇刪除雲端訊息，避免雲端的舊資料再同步回來。

這會刪除 iCloud 上的訊息。請保留完整備份，等還原完成、確認正常後，再開啟同步。

## 2. 用 iMazing 取出檔案

先在 iMazing 為這次備份建立可編輯副本，原始備份不要動。打開可編輯副本的檔案系統，取出以下兩項：

| 到哪裡找 | 取出什麼 | 放到電腦上哪裡 |
| --- | --- | --- |
| `/HomeDomain/Library/SMS/` | `sms.db` | 本專案的 `database/` 資料夾 |
| `/MediaDomain/Library/SMS/` | 整個 `Attachments` 資料夾 | 本專案的資料夾內 |

兩個位置不同，請分別取出。不要把附件拆散或改名。

完成後應該像這樣：

```text
iCloud-Message-Device-Cleanup/
├── sms_cleanup.py
├── database/
│   └── sms.db
└── Attachments/
    └── 原本的資料夾與附件，保持不變
```

**把整理好的整個專案資料夾另存一份，再繼續。** 這份不要修改，清理結果不對時可以重來。

## 3. 先檢查，不刪除

需要 Python 3.10 以上，不用安裝其他套件。可在終端機輸入 `python3 --version` 查看版本。

在終端機輸入 `cd `（後面留一個空格），把本專案資料夾拖進終端機，再按 Enter。

接著執行：

```sh
python3 sms_cleanup.py
```

這一步只檢查，**不會修改檔案**。畫面會列出一些英文，先看這幾項：

| 畫面上的名稱 | 意思 |
| --- | --- |
| `missing_attachment_rows` | 有紀錄但找不到檔案，清理時會刪除的紀錄數 |
| `orphan_files` | 沒有對應附件紀錄，清理時會刪除的檔案數 |
| `filename_updates` | 要補上檔案位置的附件數 |
| `transfer_state_updates` | 要修復狀態的附件數 |
| `blockers` | 必須是 `[]` 才能繼續；有文字就先處理問題 |

**刪除數量比預期多，或不確定附件有沒有下載完整，就先不要繼續。**

## 4. 確認後再清理

確定已經保留完整副本，而且上一步的結果合理，再執行：

```sh
python3 sms_cleanup.py --backup-dir ./backup --apply
```

這一步會**直接修改 `database/sms.db`，並刪除 `Attachments/` 裡沒有對應紀錄的檔案**。修改前的資料庫和待刪檔案，會另存到 `backup/` 裡的新資料夾。

完成後：

- `"applied": true` 表示已套用變更。
- 如果顯示 `"applied": false` 和 `No changes needed.`，代表不用清理。
- 如果顯示錯誤，先停下來，不要拿這份結果還原到手機。

## 5. 再檢查一次

```sh
python3 sms_cleanup.py
```

前面表格中的四個數量都應該是 `0`，`blockers` 應該是 `[]`。

若 `size_mismatches` 不是 `0`，代表有附件的大小與紀錄不符，需要另外確認。工具不會修復檔案內容。

到這裡，完成的是**電腦上的檔案清理**，手機還沒改變。工具不會自動把檔案放回 iMazing，也不會還原到 iPhone。

## 6. 還原到手機，再開啟同步

1. 在 iMazing 的可編輯備份中，用清理後的檔案取代原本內容：
   - `database/sms.db` → `/HomeDomain/Library/SMS/sms.db`
   - `Attachments/` → `/MediaDomain/Library/SMS/Attachments/`。取代整個資料夾，不要只合併複製，否則已刪除的附件可能還在。
2. 用 iMazing 把這份可編輯備份還原到 iPhone。
3. 打開手機的「訊息」，確認對話與附件正常。
4. 確認沒問題後，再開啟 iCloud 雲端訊息，讓手機把整理後的訊息同步上傳。其他裝置也等這次上傳完成後，再開啟同步。

**還原會讓手機回到備份當時的狀態，備份後新增的資料不會保留。** 如果結果不對，先不要開啟雲端訊息同步。

## 結果不對怎麼辦？

先保留目前的檔案，再從步驟 2 留下的完整副本重新複製一份來操作。不要覆蓋後來新增的資料。

清理不會找回原本就沒下載的附件，也不保證每個 iOS 版本都能恢復縮圖。本工具不是 Apple 或 iMazing 官方工具。

## 其他用法

- 想保存檢查報告：執行 `python3 sms_cleanup.py --report ./report.json`。`report.json` 必須尚不存在。
- 檔案放在其他地方：用 `--db` 指定 `sms.db`，用 `--attachments` 指定 `Attachments` 資料夾。
- 查看所有參數：執行 `python3 sms_cleanup.py --help`。
