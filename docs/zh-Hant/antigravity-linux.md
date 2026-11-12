[English](../en/antigravity-linux.md) | **中文**

# Linux 上的 Antigravity CLI 配額

`tokenmaster` 可在未啟動 IDE 時，讀取預設 Antigravity 個人帳號的模型配額。它先嘗試既有語言伺服器 RPC，再透過 Linux 鑰匙圈中既有的登入，查詢 Google 配額 API。

## 需求

- 使用者已在 Antigravity 登入個人帳號，登入資料位於已解鎖的 Secret Service 鑰匙圈。
- `PATH` 中有 `secret-tool`（Ubuntu 的套件為 `libsecret-tools`）。
- 可以連到 `https://cloudcode-pa.googleapis.com`。
- 自動重新整理憑證需要原生 `agy`，位於 `PATH` 或 `~/.local/bin/agy`。

以原本登入的同一個 Linux 使用者執行已安裝的工具：

```bash
tokenmaster -p antigravity --detail
tokenmaster -p antigravity --json
```

報告來源標示為 `Antigravity CLI Google quota API`。先用 `loadCodeAssist` 取得帳號方案與配額專案，再用 `fetchAvailableModels` 讀取模型配額及重設時間。零餘額可能以省略 `remainingFraction`、但仍提供重設時間的方式回傳；此時顯示剩餘零。隱藏的工具模型不納入計算，未提供的點數保持不可用。

## SSH 與登入來源

原生 CLI 偵測到 SSH 時，可能使用另一套檔案型憑證。因此，即使桌面鑰匙圈已有有效登入，SSH 中的 `agy models` 仍可能要求登入。此適配器直接讀取預設桌面鑰匙圈；SSH 工作階段必須能存取該鑰匙圈與 D-Bus 服務。

配額請求若回傳 HTTP 401，tokenmaster 會透過原生 `agy models` 重新整理同一份鑰匙圈登入，再重試一次。OAuth 重新整理與憑證保存由原生 CLI 處理，tokenmaster 不另寫憑證副本。該命令只查詢模型資料，不提交模型提示詞。HTTP 403 或 429 會直接回報，不重複驗證。

目前此後援路徑涵蓋**預設 Linux 個人鑰匙圈帳號**。具名 profile、自訂 `ANTIGRAVITY_DIR` / profile 目錄與明確指定的 RPC 憑證，繼續使用原本的隔離行為，不會退回預設鑰匙圈。僅存於 SSH 檔案的登入與企業 / ADC 憑證不在此適配器範圍內。

每週與短視窗讀數沿用既有 Antigravity 模型池解析與快取。上游可能只提供目前生效的配額視窗；已登入不保證同時取得兩個視窗或點數餘額。

[返回 tokenmaster](tokenmaster.md) · [工具集 README](../../README.zh-Hant.md)
