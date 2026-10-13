# 公開資料

[model_answers/](model_answers/README.md) 另提供 2,634 筆結構化模型生成回答，不含真人問卷、outcome 或逐人連結。這些不是本節的彙總資料，也不能與其他公開列號配對。

## 彙總資料

aggregate/frozen_results.json 是凍結 machine-readable results 的數值一致副本，包含條件結果、既有 CI、H8、K、統計參考及執行摘要。沒有逐人分數。主檔的歷史欄位名稱保留，不能把所有自動布林欄位視為全機存取或正式發布安全認證。

| 檔案 | 一列／單位 |
|---|---|
| plot_points.csv | 圖中一個彙總估計與 95% CI，共 19 組；含軸範圍與座標 |
| descriptive_distributions.csv | 一個分數分布摘要，非逐人觀察 |
| stage1_h8_item_results.csv | 一題 H8 的彙總指標，共 8 題 |
| k_stability_results.csv | 一個重複次數的信度估計，非逐人重複結果 |
| paired_condition_comparisons.csv | 一組條件差異 |
| analysis_ledger.csv | 一個分析角色，共 12 項 |
| metric_scale_audit.csv | 一個測量／參考的尺度定義 |
| table_s2_list_schedule.csv | 一列實驗設計，共 21 列，非參與者選擇 |
| table_s4_reproducibility_hashes.csv | 一份材料的來源識別，不含材料內容 |

完整精度保留；paper/tables/ 的表格是顯示用四位小數版本。bias=模型−真人，MAE 單位需依尺度區分，SD ratio=模型 SD／真人 SD。圖說及 metric_scale_audit.csv 定義各統計量與參考線。
