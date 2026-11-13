# Personal Knowledge Agent

Windows 個人知識助理的開源開發原型，MIT 授權。以本地 SQLite FTS5 搜尋 Markdown、HTML、PDF、Word、Excel 與 CSV，提供來源位置、可校對的 OCR／表格衍生文字、可選 OpenAI 或本地模型問答。專案仍在開發，尚非完整正式產品。

## 隱私與安全

來源文件以唯讀方式存取，索引及工作副本存於獨立資料目錄。外網 AI 連線預設關閉；啟用後每次請求前會預覽欲傳送的問題與片段。OpenAI Key 使用 Windows 憑證管理員，請勿提交 Key、文件、索引、`.pka` 備份或本機 `data/`。AI、OCR 與掃描表格結果需要人工核對。

## 本機開發

需要 Windows、Python 3.12、Tkinter 與支援 FTS5 trigram 的 SQLite。安裝 `requirements-dev.txt` 的依賴後執行 `python app.py`。首次使用須選擇資料來源，程式不得放在來源資料夾內。測試：`python -m unittest -v test_core test_ai test_package test_budget test_pdf_features test_installer test_adb test_research`。特定 OCR 測試會在本機引擎或語言資料不可用時跳過。

PDF 預覽使用 pypdfium2；文字型表格使用 pdfplumber。Windows OCR 可使用已安裝的辨識語言；Tesseract OCR 需要自行安裝 Tesseract。簡體中文語言資料可由使用者另行取得官方 [tessdata_fast](https://github.com/tesseract-ocr/tessdata_fast)，並放在 `data/ocr/tessdata/`；第三方資料有其自身授權。程式不自動下載模型。

## 已知限制

批次 OCR 限同一文件每次 25 頁，產物須校對後才能供 AI 引用；掃描表格依文字位置推測，複雜表格和合併儲存格不保證正確。`.pka` 不加密，真實大型資料庫需另做還原演練。OpenAI 費用是按使用者填寫單價的 APP 本地估算，不是官方帳戶餘額。

程式尚未具備簽章更新、自動解除安裝、完整版本還原及正式 Tauri 介面。

## ADB 裝置探索與研究模式

支援 Android 閱讀器截圖、視覺模型規劃、每一步人工確認、待校對步驟紀錄與截圖備份。型號與韌體差異可能影響 ADB 行為，尚需實機驗證；不支援自動執行任意命令。

研究模式允許自由輸入品牌、型號與韌體，也可在使用者選擇後唯讀取得已連線裝置資訊，經使用者確認後以 OpenAI Responses API 網路搜尋公開資料、顯示來源並建立待驗證筆記。不會執行研究到的指令。本機模型仍可供文字／視覺問答，網路研究目前需要 OpenAI。網路搜尋工具費目前不計入本地估算，設定預算時研究功能會阻擋，避免繞過預算。

發行時請分別遵守並隨發行包附上 Android Platform Tools、Tesseract、其語言資料及其他依賴的第三方授權。程式碼採 MIT；不代表上述第三方元件皆為 MIT。請勿提交使用者知識庫、截圖、憑證、備份或日誌。
