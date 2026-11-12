# thermalmaster

把這台機器上的每一個溫度、風扇和功耗感測器，畫成終端機裡的即時曲線。只有一個 Python 檔，只用標準函式庫。它直接讀作業系統，不需要 root。同一個檔案可以在 macOS 和 Linux 上執行。

```bash
thermalmaster                         # 本機
thermalmaster --once                  # 每個感測器印一次
thermalmaster --json                  # 一份 JSON 快照
thermalmaster --all                   # 連平常隱藏的感測器也顯示
thermalmaster --probe                 # 每個來源的原始傾印
thermalmaster --record run.csv        # 把樣本寫進 CSV（--headless、--duration 2h）
thermalmaster --bell                  # 新的嚴重超標時讓終端機響鈴
thermalmaster --fleet fleet.json      # 檔案裡的每一台機器，經由 SSH
thermalmaster --fleet a,b             # 直接列出 SSH 別名
thermalmaster --fleet fleet.json --mobile
```

`master thermal` 是同一條命令。

## 機群

`fleet.json` 列出 SSH 別名：

```json
{
  "hosts": [
    "gpu-a",
    "dgx-spark",
    "mac-studio",
    {"name": "mac-laptop", "mobile": true}
  ]
}
```

範例見 [`examples/thermalmaster.fleet.json`](../../examples/thermalmaster.fleet.json)。

單純的 `--fleet FILE` 會跳過標成 `"mobile": true` 的項目，因為合上的筆電只是一台一直處於離線、又不停止重試的主機。`--mobile` 會把它們加進來。明確的逗號清單會照單使用。`"disabled": true` 會去掉一台主機。`"target": "local"` 直接讀查看器自己，不走 SSH。`"target": "user@host"` 會原樣傳給 SSH。

其他機器不用安裝任何東西。查看器把 `app.py` 經由 SSH 送進 `python3 -`，所以每台機器跑的都是你正在看的這個版本。一台機器只需要 SSH 金鑰登入（`BatchMode`）和 Python 3.8 或更新版本。若找不到 `fleet.json`，錯誤訊息會請你複製範例。

機群畫面每台機器一個方塊。`Enter` 打開那台機器。`Esc` 回到機群。連不上的機器顯示 `down`，並附上 SSH 自己的原因，然後以退避間隔重試。重新連上後歷史會保留。連線沒斷、卻不再送資料時顯示 `stale`。

```bash
thermalmaster --fleet fleet.json --once
thermalmaster --fleet fleet.json --json
thermalmaster --fleet fleet.json --record fleet.csv
```

沒有 `--fleet` 的 `thermalmaster` 不會開啟網路。

## 按鍵

| 按鍵 | 作用 |
|---|---|
| 方向鍵、`hjkl`、滑鼠滾輪 | 選取群組、感測器或機器 |
| 點擊 | 選取。再點一次打開 |
| `Enter` | 該群組的每個感測器，選取的曲線在上方 |
| `a` | 全部感測器 |
| `m` | 畫群組最大值或群組平均值 |
| `s` | 切換排序。機群畫面是 `config`、`hottest`、`margin` |
| `/` | 子字串篩選 |
| `w` | 只看過熱、告警、降頻或快速上升的感測器 |
| `f` | 釘選目前的感測器 |
| `[` `]` `{` `}` | 在歷史中前進或後退 1 或 10 個樣本 |
| `b` | 新的嚴重超標時響鈴 |
| `Esc`、Backspace | 返回 |
| `+` `-` | 縮短或加長時間窗 |
| `u` | 攝氏或華氏 |
| `p`、空白鍵 | 暫停畫面。取樣仍繼續 |
| `r` | 重設最小、最大，以及峰值超標記憶 |
| `g` | 方框或 ASCII |
| `?` | 說明，包括每個來源的讀取成本 |
| `q` | 結束 |

## 怎麼讀畫面

每個群組（CPU、GPU、SoC、記憶體、儲存、功耗、風扇、電池等）有一個方塊。曲線是該群組隨時間的最大值，標題是目前的最大值。`m` 把曲線改成平均值。時間從右往左：現在在右緣。

顏色從青色（涼）經過綠色、黃色（警告）到紅色（嚴重）。感測器有回報硬體限值時就用那個限值。警告或嚴重超標會留下峰值標記，直到按 `r`。降頻狀態會顯示成徽章。

`--all` 會保留平常隱藏的感測器，並印出每一個的 `hidden_reason`。被隱藏的讀數包括停在占位溫度上的未使用 Apple 晶片鍵、沒有接上的韌體通道，以及沒有讀數的感測器。

## 數字從哪裡來

**macOS**，經由 IOKit：

| 來源 | 內容 |
|---|---|
| AppleSMC | `T???` 溫度、風扇、功耗鍵 |
| IOHID | PMU 晶粒感測器、NAND、電池 |
| `kern.thermalstate` | 系統熱壓力 |

**Linux**：

| 來源 | 內容 |
|---|---|
| `/sys/class/hwmon` | coretemp、k10temp、NVMe、DIMM、網卡、GPU、風扇、功耗、電池 |
| `/sys/class/thermal` | 熱區，包括 SoC 與筆電 ACPI 區 |
| `/sys/class/power_supply` | 沒有 hwmon 裝置時的電池溫度 |
| `/sys/class/powercap` | Intel RAPL 能量計數，換算成瓦；只有 root 能讀時會跳過 |
| `/sys/devices/system/cpu` | 封裝與核心降頻計數 |
| NVML | GPU 與 HBM 溫度、降速限值、風扇、功耗、P-state、降頻原因 |
