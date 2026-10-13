# 問卷條件化大型語言模型與個體風險排序

本套件整理本研究的方法、凍結彙總結果、論文、圖表與經白名單整理的結構化模型回答，供研究方法查閱、單表計分核對及彙總圖表重建。這是獨立公開套件，沒有內部 Git 歷史、真人問卷回答、真人 outcome 或可對回個人的識別表。

**公開儲存庫：[sheyung21-crypto/llm-risk-behavior-study](https://github.com/sheyung21-crypto/llm-risk-behavior-study)。** 版本為 `2026-10-13-model-answers`。唯一作者為楊世永（Yang Shih-Yung），機構為天主教輔仁大學金融與國際企業學系。使用者已確認原作者同意資料研究使用，並批准本套件與模型回答公開；這不代表原始資料再散布授權。未另指定開放再利用授權，見 [授權狀態](LICENSE_STATUS.md) 及 [資料與權利說明](DATA_AVAILABILITY.md)。目前投稿目標為《東吳經濟商學學報》，本包不是匿名送審版，亦不表示論文已獲錄用。

## 研究與結果

本研究檢驗：模型只接收單一受試者的問卷回答後，生成的風險選擇分數能否對應同一人的實驗行為排序。409 人中有 382 位完整案例；40 位技術先導排除後，正式樣本為 342 人。C1 是主要條件；C2 是嚴格資訊敏感度；C3 是任務近端診斷。主測量固定 K=1。

C1 Spearman ρ=0.3404，95% CI [0.2436, 0.4332]，MAE=3.3661 列，平均偏差=−2.7593 列，SD 比=0.7656。結果支持部分個體排序對應，也顯示分數偏低與變異壓縮，不能解讀為可替代真人行為測量。

## 從哪裡開始

- [論文](paper/manuscript.md)、[附錄](paper/supplement.md)、[13 頁圖表 PDF](paper/figures_tables.pdf)。
- [研究方法與執行資訊](methods/README.md)：樣本、條件、提示詞、計分、重試及分析角色。
- [重現指南](REPRODUCIBILITY.md)：純標準函式庫驗證、合成計分測試、彙總圖表重建。
- [數據字典](data/README.md)、[重畫說明](paper/redrawing_guide.md)。
- [2,634 筆結構化模型回答](data/model_answers/README.md)：不含真人答案、原始 Prompt、participant／job／session IDs 或逐人連結。
- [引用狀態](CITATION.md)、[授權狀態](LICENSE_STATUS.md)、[發布檢查清單](RELEASE_CHECKLIST.md)。

## 套件結構

| 路徑 | 內容 |
|---|---|
| paper/ | 保留全部 24 篇參考文獻的稿件、6 圖、7 表、向量及點陣素材 |
| data/aggregate/ | 完整精度的既有彙總結果；沒有逐人列 |
| data/model_answers/ | 正式成功模型回答；無真人答案與逐人連結，重新排序且保留重複答案 |
| methods/ | 未填入個人回答的提示詞、schema、兩張選擇表、欄位索引與原始碼閱讀快照 |
| tools/ | 公開驗證器、純計分函式及獨立圖表重建入口 |
| tests/ | 只使用合成選擇序列及暫存檔的測試 |
| provenance/ | 檔案來源、轉換記錄、圖表輸入雜湊、環境與完整性清單 |

方法原始碼快照用於閱讀；完整受限分析仍需要另行核准的資料、完整歷史環境與證據，不能只靠本包重算所有統計。本包不啟動 LLM、不連接帳戶、不下載資料。
