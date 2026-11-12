# macmaster

本機的終端機監控。CPU、GPU、記憶體、磁碟、網路和行程在同一個畫面。它可以在 macOS 和 Linux 上執行，不需要第三方 Python 套件。macOS 上透過 `ctypes` 使用 Mach、IOKit、`libIOReport`、AppleSMC 和 `sysctl`；Linux 上讀 `/proc`、`/sys` 和 `libnvidia-ml.so.1`。

```bash
macmaster                      # 全螢幕 TUI
macmaster --once               # 印出一張彩色快照後結束
macmaster --gpu-only           # GPU 面板與 GPU 行程
macmaster --json               # 一份 JSON 快照
macmaster --compact            # 密集的單畫面摘要
macmaster --stream             # 一行一筆 JSON
macmaster --record run.jsonl   # 把樣本附加到檔案
macmaster --replay run.jsonl   # 回放錄製
macmaster --simulate-nvidia    # 本機沒有 NVIDIA GPU 時預覽版面
macmaster --fleet host-a host-b
```

`master mac` 和 `btop-nvtop` 是同一個程式。

## 畫面上有什麼

- **CPU。** 總計與每核心使用率、Braille 歷史、Apple 晶片的 E 核與 P 核時脈、負載、開機時間、功耗與電池。
- **GPU。** Apple GPU 使用率（裝置、渲染器、tiler）、統一記憶體、時脈、P-state、GPU 與 ANE 功耗、晶粒溫度，以及每個行程的 Metal 時間。NVIDIA 則有 SM 與記憶體控制器使用率、VRAM、BAR1、時脈、P-state、編碼器與解碼器、功耗、溫度、風扇、PCIe 世代、寬度與吞吐量，以及每個行程的 CUDA 或圖形使用量。
- **記憶體與磁碟。** RAM、swap，以及每個掛載點的即時讀寫吞吐。
- **網路。** 每個介面的位址、吞吐與累計。`b` 和 `n` 切換介面。
- **行程。** PID、程式、命令、GPU、執行緒、使用者、記憶體、VRAM、GPU% 和 CPU% 在同一張表。

Apple 晶片和 NVML 這兩條路徑都不需要 sudo。

## 按鍵

| 按鍵 | 作用 |
|---|---|
| `1`–`5` | 顯示或隱藏 CPU、GPU、記憶體與磁碟、網路、行程 |
| `z` `m` | 放大目前的面板 |
| `g` | 只看 GPU 行程 |
| `t` | 行程樹 |
| `/` `f` | 篩選行程。`Esc` 清除 |
| `<` `>` `s` | 切換排序欄 |
| `r` | 反轉排序 |
| `↑` `↓` `j` `k` | 在行程清單中移動 |
| `Enter` `Space` | 行程詳情 |
| `K` `x` | 向選取的行程送訊號（`SIGTERM` / `SIGKILL`） |
| `d` | 下一張 GPU |
| `b` `n` | 下一個網路介面 |
| `+` `-` | 重新整理間隔，從 100 毫秒到 10 秒 |
| `c` | 色彩主題 |
| `?` `h` | 說明 |
| `q` `Esc` | 結束 |

主題：`default`、`nvidia`、`apple`、`dracula`、`nord`、`monokai`、`tokyo-night`、`catppuccin`。

## 小型機群

```bash
macmaster --fleet
macmaster --fleet gpu-a mac-studio
```

沒有主機名稱時，`--fleet` 只看這台機器。多出來的參數是 SSH 別名。遠端必須已經把 `macmaster` 放在 `PATH` 上；蒐集器會在那裡執行 `macmaster --json`。以 `-` 開頭的名稱會被拒絕。

這和 `gpumaster` 是兩件事。節點不該安裝這套工具時，用 `gpumaster`。每台機器都已經有 `macmaster`，而你想把本機儀表板在每台上各看一次時，用 `macmaster --fleet`。
