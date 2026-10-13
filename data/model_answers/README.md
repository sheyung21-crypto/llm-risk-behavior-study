# 結構化模型回答

本目錄依使用者批准，提供全部 2,634 個正式 canonical jobs（凍結矩陣中的標準工作）各自唯一的成功模型回答。這些是模型生成資料，不是真人問卷回答或真人實驗 outcome。沒有重新呼叫模型、挑選答案或改變研究計分。

| 檔案 | 每行內容 | 行數 |
|---|---|---:|
| original.jsonl | C1／C3、兩張 List、repeat 1，每筆 21 個 LOTTERY／SAFE 選擇 | 1,368 |
| c2.jsonl | C2 sensitivity、兩張 List、repeat 1，每筆 21 個選擇 | 684 |
| k.jsonl | C1 stability diagnostic 的 repeats 2–5、兩張 List，每筆 21 個選擇 | 240 |
| stage1.jsonl | H8 八題模型預測，整數 0–10；不含 withheld 真人答案 | 342 |

技術 Pilot、失敗／中斷 attempts、stderr、原始 session events、模型解釋文字及未完成輸出均不在本目錄。

## 格式與資料界線

JSONL 每行是一個 JSON object。欄位限定為 `collection`、`condition`、`list_id`、`repeat_index`、`model`、`reasoning`、`codex_cli_version` 及 `answer`。`answer` 保留原成功 response 的結構與數值；JSON 序列化格式不保證逐 byte 相同。

Stage 2 的 `choices` 按原 row 1–21 排列。Stage 1 的 `responses` 依 R3、R6、R8、R12、R13、R17、R24、R41 排列。模型／reasoning 與實際 CLI 版本逐筆核對原完成 metadata；CLI patch 差異是執行環境資訊，不代表模型變更。主要 K 仍為 1；`k.jsonl` 的額外 repeats 只屬穩定性診斷。

不提供 participant key、替代 subject ID、job ID、session ID、timestamp、prompt hash 或其他連結表。整筆回答按公開欄位的 canonical JSON 字典序重新排序，與受試者／執行順序無關；不同檔案的同一行位置不代表同一人。完全相同的模型回答仍保留全部筆數，不能去重。

因此可以逐筆核對 21 列回答與既定單表計分，但不能跨 List／condition／repeat 對回同一人，也不能由本包重現 ICC、H8 真人比較或與真人 outcome 的相關。這個設計減少對回真人資料的途徑，不宣稱數值回答在所有外部資料組合下絕對不可識別。

## 驗證

在套件根目錄執行：

```text
python -B tools/verify_package.py
python -B tests/test_model_answers.py
```

驗證器檢查欄位白名單、數值／選項、行數、條件／List／repeat 分布及檔案 SHA-256。本機匯出另外核對 frozen matrix、完成 marker、原 response／metadata checksum。公開包沒有內部 matrix 或 receipts，因此公開檢查不是對原來源的獨立稽核。來源範圍與匯出摘要見 [匯出紀錄](../../provenance/model_answer_export.json)。
