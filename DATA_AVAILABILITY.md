# 資料取得、使用依據與發布界線

原研究為 Falk、Becker、Dohmen、Huffman 與 Sunde 的偏好調查模組研究（Management Science, 69(4), 1935–1950；2022 線上發表、2023 卷期）。官方[文章頁](https://doi.org/10.1287/mnsc.2022.4455)及[補充材料頁](https://pubsonline.informs.org/doi/suppl/10.1287/mnsc.2022.4455)提供來源入口。原資料位置及內容識別見 provenance/source_catalog.csv；本包沒有重新散布原始 DTA、第三方 PDF 或 Stata 程式。

## 本次新查得的正式條款

2026-09-09 檢查該論文專屬的[官方資料下載頁](https://services.informs.org/dataset/mnsc/download.php?doi=mnsc.2022.4455)，頁面將檔案用途限於驗證原論文主結果的可重現性，其他用途需要原作者或相應資料權利人的明確許可。本次沒有勾選同意、填寫個人資料、下載原資料或聯絡作者。

2026-10-13 更新：使用者明確確認原作者已同意資料研究使用。此處依使用者確認記錄，不附上同意信、聯絡資訊或其他私人通訊，也不將研究使用同意延伸解讀為原始資料、完整問卷或連結資料的公開再散布許可。

本版依使用者明確批准，公開於 [GitHub](https://github.com/sheyung21-crypto/llm-risk-behavior-study)。程式／文件尚未指定開放再利用授權。完整題目文字與選項的跨來源再散布未確認，因此提供題號、量尺、來源頁與條件索引，不另散布完整問卷文字表。

## 公開套件的資料範圍

包含研究者既有彙總結果、圖表、方法、未渲染提示詞，以及使用者本次批准加入的 2,634 筆結構化模型回答。模型回答僅保留核准的選項／數字及條件／List／repeat／模型版本資訊，移除原始識別與執行順序，不含真人答案或逐人連結表，詳見 [模型回答說明](data/model_answers/README.md)。

仍排除原始資料、真人問卷回答、真人 outcome、逐人分析分數、split 與 job matrices、participant keys、rendered persona prompts、原始模型對話／自由文字、逐人 OOF prediction、runtime stores、憑證及原始逐人 scatter SVG。新圖表只有彙總估計與分布摘要。模型預測不應被當作對真人的已知事實。

## 受限材料

本包不承諾「向作者索取即可取得」。本次不提供受限資料申請或下載服務；若未來提供，須先確認資料管理人、可提供的材料、使用目的、授權及安全條件。原始資料應由讀者依原提供者條件取得；模型回答與真人資料的逐人連結仍不公開。請勿將受試者資料上傳至 GitHub issues。

## 論文可用的資料與程式碼聲明

以下文字可用於論文；定稿時另記錄實際引用的 Git commit SHA：

> 本研究之方法原始碼、計分程式、未填入真人回答的提示詞模板、彙總結果及移除原始識別與逐人連結的結構化模型回答，公開於 [GitHub](https://github.com/sheyung21-crypto/llm-risk-behavior-study)（套件版本：2026-10-13-model-answers）。原始真人問卷、實驗 outcome、逐人連結表、實際 persona prompts 與原始 session logs 不隨庫散布。公開模型回答可供核對單表選擇與計分，但不足以單獨重現與真人 outcome 的配對分析。原始資料須依原提供者的授權條件取得。
