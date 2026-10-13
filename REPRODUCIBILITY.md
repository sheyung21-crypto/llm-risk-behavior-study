# 可重現性範圍與操作

## 公開檢查

使用 Python 3.12，在套件根目錄執行：

```text
python -B tools/verify_package.py
python -B tests/test_public_tools.py
python -B tests/test_model_answers.py
```

這三項只用 Python 標準函式庫。驗證器核對完整檔案清單與 SHA-256、公開資料粒度規則、相對連結、彙總數字對應，以及新增模型回答的嚴格欄位、筆數與選項。合成測試不使用研究參與者資料。請勿從內部研究 repository 執行完整 pytest 來代替此處的獨立測試。

## 圖表重建

所需套件版本記於 requirements-figures.txt。可在您自有的獨立 Python 環境安裝：

```text
python -m pip install -r requirements-figures.txt
python -B tools/rebuild_figures.py --font /path/to/local-cjk-font.ttf --output /path/to/new-figure-output
```

請將兩個示意路徑替換為本機實際位置。Windows 可以指定已安裝的 msjh.ttc。字型不隨套件散布；原版字型名稱與雜湊見 provenance/figure_environment.json。輸出目錄必須尚未存在且位於套件外。程式會拒絕覆寫與符號連結路徑。

本入口沿用已審閱的繪圖函式，只改路徑、字型參數及輸出位置。它重建 6 圖、7 表、13 頁 PDF 與相關素材；只讀包內核准彙總結果，不接觸上層研究庫、內部 Git 或逐人資料，不重新估計信賴區間或統計量。相同字型／依賴下可與 paper/ 的 SVG、CSV 及 PDF 核對；不同字型的換行與圖形 bytes 不保證一致。

本次實際測試是「將套件複製到獨立暫存位置後，限制讀寫範圍，從非套件工作目錄執行」，並使用現有 Python 環境。未進行全新機器套件安裝、跨平台數值重算或實體列印驗證。

## 三種不同層次

| 層次 | 本包提供 |
|---|---|
| 方法理解 | 論文、提示詞範本、欄位及條件索引、計分程式、統計原始碼閱讀快照與執行摘要 |
| 彙總結果再呈現 | 可以由已凍結數據重建圖表及表格；不是獨立驗證統計估計 |
| 模型單表回答核對 | 提供 2,634 筆未連結個人的模型回答，可核對單表計分；不提供跨表／條件／repeat 的逐人配對 |
| 受限資料數值重現 | 尚不能由本包單獨完成；需授權資料、逐人分數／輸出、矩陣及歷史證據 |

重新呼叫模型不等於重現當時輸出，本研究亦不再生成模型回答。模型歷史快照不能保證服務未來提供相同輸出。

## 受限數值重現尚需哪些材料

從已封存分數重建主要分析至少需要固定問卷輸入、C1／C2／C3 逐人分數、兩表／K 診斷資料、H8 逐人誤差、locked outcome、原始 MainSample，以及當時方法與盲態文件的正確 bytes。公開模型回答足以核對單表計分；完整逐人配對與來源核對仍需要受限的 canonical metadata、完成紀錄與 matrix bindings。reconciliation 另依賴已封存的逐人合併結果。

內部專案已有只讀歷史驗證與受限輸入契約，但它依賴不公開的 Git blobs，故沒有把該入口複製成一個必然失敗的公開指令。歷史盲態報告與後續閱讀報告不同；不能改 expected hash、倒填盲態記錄或在原研究目錄重跑分析。取得權利及必要資料後，應另建受限工作區，以固定版本、明定換行及隔離輸出規則重現。

## 完整性與換行

MANIFEST.json 保存每個交付檔案的 SHA-256，唯一不包含自身。套件 .gitattributes 使用 -text 保留交付 bytes，避免 Git 自動換行讓歷史副本與圖表輸入雜湊變動。這不改動內部研究庫的 Git 設定。SHA-256 能檢查意外變更，並不是第三方簽章或獨立預註冊。
