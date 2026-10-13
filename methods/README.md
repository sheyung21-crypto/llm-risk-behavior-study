# 研究方法及必要執行資訊

409 筆原始資料要求 54 個風險問卷欄位及真人行為完整，形成 382 人。事前一次分派 40 人至技術先導、342 人至正式樣本；分組使用 NumPy PCG64、seed=20260902，不按結果分層，不重抽。技術先導原 420 jobs 停止，保留的 13 個完成 jobs 不進正式分析。

C1 含 46 題、為唯一主要條件；C2 移除 R48／R50、含 44 題、為嚴格資訊敏感度；C3 加入 R49、含 47 題、為任務近端診斷。逐題納入狀態見 condition_membership.csv。R48 為假想中獎後彩券投入，R50 為有收入風險轉職，R49 為放棄彩券所需確定金額。完整問卷原文未再散布，來源與欄位索引見 item_index.csv。

Stage 1 是獨立保留題檢查：38 題問卷輸入，預測未提供答案的 H8 八題；H8=R3、R6、R8、R12、R13、R17、R24、R41。C1／C2／C3 則使用真人 H8 答案；Stage 1 預測不回填。實際先完成 C1／C3，再執行獨立 Stage 1 及 C2／K，不把概念流程當成執行時間順序。

## 提示詞與執行

prompts/ 保留四份原始範本及兩個 JSON Schema 的 exact bytes，沒有填入真人回答。PROFILE_JSON 每次僅填單一人的核准問卷；CHOICE_ROWS_JSON 由固定 Lists 填入；HELDOUT_QUESTIONS_JSON 只含保留題題目與量尺。這些是研究方法，不因與 AI 有關就移除。

固定模型 gpt-5.6-terra、reasoning=medium、standard speed、fast mode=false、ChatGPT 登入、paid API=false；temperature 由平台管理、不可由研究者指定。每個 job 使用新 session／workspace；不混合參與者、不提供真人行為結果。CLI 設定與實際觀察版本需區分：設定要求 0.153.3；正式完成紀錄顯示 424 jobs 為 0.153.0、2,210 jobs 為 0.153.3，不能聲稱全部使用單一 CLI 版本。

執行矩陣：C1／C3 1,368 jobs、Stage 1 342、K 診斷新增 240、C2 684，共 2,634 canonical jobs；共有 2,650 attempts。pre-session attempt limit=3，格式不合法只可一次 format retry，重試提示詞 byte-identical；quota pause 保留已完成結果，只續作未完成 jobs。安全失敗須停止，不能以重試規則放寬隔離。執行彙總沿用凍結結果，不重新掃描逐筆模型紀錄。完整彙總見 execution_metadata.json。

## 計分與診斷

每張 List 有 21 列，選擇為 LOTTERY／SAFE；彩券各半機率得 1,000 或 0 點，確定金額見 lists/。List 2 為研究者事前固定擾動重建，非聲稱逐字取自原作者第二表。

LOTTERY→SAFE 的 SAFE 列數是一基分數；多次此方向轉換時取其列數平均。全 SAFE=1、全 LOTTERY=22 是研究者事前邊界規則。兩類選擇都有但沒有 LOTTERY→SAFE 時分數 missing；不插補、不因數值缺失重試。個人分數為兩表平均，缺一表不以單表替代。tools/switching.py 為原計分函式的精確副本。

主要 K 永久為 1；另固定 30 人，每人五次兩表測量，用來估計 ICC(1,1)，平均 3／5 次信度只是同一變異成分換算，不用來改 K。Stage 1 與 K 先盲態完成並封存後，正式分析才解鎖 x_risk_av。資料準備曾讀行為以選完整案例；早期本機測試也有已記錄的存取事件，故不能把正式分析解鎖敘述成整個專案從未讀過 outcome。

## 統計架構

主要指標為 C1 分數與真人 x_risk_av 的 Spearman 相關，既有 95% percentile bootstrap CI；推論使用 10,000 次 participant-vector resampling／置換，相關置換 p 為雙尾加一修正。C2 是 sensitivity，C3 是 diagnostic；配對相關差異方向為其他條件減 C1。次要尺度描述為 Pearson、MAE、平均偏差、SD 比。完整角色及尺度見 data/aggregate/analysis_ledger.csv、metric_scale_audit.csv。

Ridge 是 supervised contextual reference，固定 C1 的 46 個數值特徵，5×5 nested CV，training-fold-only scaling，以 MSE 選 33 個 10^-4 至 10^4 的 alpha。R2+R3 是原研究題目及權重的背景參考，原標準化尺度不與轉換列 MAE 直接相比。常數中位數與按真人 SD 換算誤差為結果後描述；不是新增主要結果。固定 seeds 及推論設定見 analysis_settings.json。

## 原始碼閱讀快照

source_archive/ 保留正式分析、reconciliation、計分及 prompt rendering／validation 的原始碼 exact bytes，可追蹤方法。這是選取的閱讀快照，不是完整可執行研究庫；未帶入 participant stores、依賴模組、歷史 Git gates、完整題目文字或受限資料。請使用 tools/ 的公開入口進行套件驗證與彙總圖表重建，勿把 source_archive 中的研究主程式當成公開的一鍵分析。

原始 AGENTS、Goals、登入 attestation、安裝路徑及歷史合成執行產物仍留內部；科學必要規則已整理於本文件及來源記錄。
