# tokenmaster

在一個終端機畫面裡看 Codex、Claude、Antigravity、Grok、Cursor 和 OpenRouter 的每週配額與預付點數。

tokenmaster 從各產品既有的設定目錄或 macOS 鑰匙圈尋找憑證。先用 `tokenmaster doctor` 檢查，再用 `-p` 選擇平台。

`auth`、`login` 與帳號快照可以把憑證儲存或複製到 tokenmaster 設定與具名 profile。部分平台會重新整理權杖並寫回憑證來源。設定預設位於 `~/.config/tokenmaster`；該位置無法寫入時，會退回工作目錄中的 `.tokenmaster/`。本儲存庫已忽略此後援目錄，其他儲存庫需要自己的忽略規則。可用 `TOKENMASTER_CONFIG_DIR` 指定固定設定目錄。請妥善保存設定與 profile。

```bash
tokenmaster                         # 儀表板
tokenmaster --detail                # 模型池與 5 小時視窗
tokenmaster -p codex,claude         # 只看部分平台
tokenmaster --width 110             # 配合較窄的終端機
tokenmaster --watch                 # 原地重新整理
tokenmaster watch -i 10 -d
tokenmaster --json
```

`master token` 是同一條命令。

## 平台

| 識別名 | 它讀取什麼 |
|---|---|
| `codex` | `~/.codex/auth.json` 與 Codex 用量 API。本地工作階段 JSONL 是後援 |
| `claude` | `~/.claude/.credentials.json` 或 macOS 鑰匙圈，然後是 Anthropic 用量 API |
| `antigravity` | 本地語言伺服器 Connect-RPC；Linux 預設個人帳號也支援[原生鑰匙圈配額 API](antigravity-linux.md) |
| `grok` | `grok` CLI 的驗證檔、grok.com cookie，或 xAI API 金鑰 |
| `cursor` | `cursor-agent` 的驗證、macOS 鑰匙圈，或 `state.vscdb` |
| `openrouter` | `OPENROUTER_API_KEY`，或用 `tokenmaster auth` 存下來的金鑰 |

呼叫是並行的。在 `tokenmaster config path` 顯示的設定檔中，以 `deadline_seconds` 限制一次儀表板重新整理的等待時間。預設為 `timeout_seconds` 的 2.5 倍（預設單項逾時 8 秒，總期限為 20 秒）；`0` 關閉總期限。已取得的部分資料會保留；正在進行的憑證寫入仍可能需要在程式結束前完成。

平台整合取決於上游回應與憑證格式。各帳號可取得的配額視窗與點數不同，單有 API 金鑰不保證能取得訂閱配額。本地工作階段用量及 API 等值費用估算不等於平台帳單。

## 帳號

同一個平台可以同時看多個訂閱。每個具名帳號在 `~/.config/tokenmaster/profiles/<provider>/<account>` 有自己的目錄。

```bash
tokenmaster login claude work
tokenmaster account snapshot codex personal
tokenmaster account add openrouter team --api-key "$OPENROUTER_API_KEY"
tokenmaster account list
tokenmaster exec claude work
tokenmaster exec claude:auto          # 用預設 smart 策略選擇帳號
eval "$(tokenmaster env claude work)" # 只影響這個 shell
tokenmaster -p claude:default,claude:work
tokenmaster account remove claude work --purge
```

`login` 與 `exec` 需要先安裝平台原生 CLI。`snapshot` 複製既有登入，不會建立新訂閱。`pick` 推薦帳號並顯示理由，加上 `--quiet` 才只輸出帳號名稱。策略有 `smart`（預設）、`most-remaining` 和 `expiring-first`；選擇時會考慮可用配額、短視窗上限與重設時間。`exec` 在啟動時選定帳號，不會替已執行的工作階段切換帳號。

## 其他命令

```bash
tokenmaster auth                      # 憑證精靈
tokenmaster auth openrouter --api-key "$OPENROUTER_API_KEY"
tokenmaster quota openrouter --weekly-limit 30 --weekly-unit USD
tokenmaster quota grok --credit-limit 50 --credit-used 12.5 --credit-unit USD
tokenmaster quota openrouter --reset

tokenmaster doctor                    # 本地憑證與快取
tokenmaster config show
tokenmaster config path
tokenmaster config clear-cache

tokenmaster statusline --no-refresh   # 僅讀快取，不查詢平台
tokenmaster statusline --format tmux --async-refresh
tokenmaster statusline --format claude-code --async-refresh

tokenmaster history daily --days 30
tokenmaster history daily -p codex --days 30
tokenmaster history models
tokenmaster history projects
tokenmaster history projects --export csv --output usage.csv

tokenmaster notify                    # 80%、95%、100% 時的桌面通知
tokenmaster notify --threshold 75,90,100 --dry-run
tokenmaster --watch --notify
```

重設那一欄包含消耗節奏（`surplus`、`on_track`、`fast`、`overburn`），以及視窗可能耗盡的估計。`statusline` 重用新鮮快照，但快照缺少或過舊時，預設可能同步查詢平台。加上 `--async-refresh`，已有的過期快照可先回傳，再於背景更新；第一次查詢仍可能等待。提示字元若完全不能查詢平台，使用 `--no-refresh`，並先執行儀表板建立快照。

`history` 讀取本地 SQLite 帳本，預設為 `~/.local/share/tokenmaster/warehouse.db`，並同步可用的本地工作階段，除非指定 `--no-sync`。支援 `daily`、`weekly`、`monthly`、`projects`、`models` 與 `snapshots` 檢視。已匯入的紀錄在上游 CLI 清理日誌後仍保留；歷史覆蓋範圍取決於已匯入的資料。

寫進這本帳的專案名稱會收成短標籤。家目錄前綴在寫入之前就會被拿掉。

## 畫面

儀表板有五欄：平台、方案、每週剩餘、重設、點數。顏色跟著剩餘比例：15% 或以下是紅色，40% 或以下是黃色，更高是綠色。`--detail` 會加上各模型池。版面會計算東亞寬字元，終端機較窄時可用 `--width`（或 `TOKENMASTER_MAX_WIDTH`）壓縮欄寬。
