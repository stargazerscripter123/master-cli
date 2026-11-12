# gpumaster

用一個畫面看每一台機器上的每一張 GPU，連線只用 SSH。GPU 節點上不用安裝任何東西。`remote.sh` 會經由多路複用的 OpenSSH 連線送進遠端的 `bash -s`。

節點需要 `ssh`、`bash`，以及 `nvidia-smi` 和 Linux `/proc`。Apple 晶片的 Mac 則用系統自帶的 `ioreg`、`sysctl` 和 `vm_stat`。

```bash
gpumaster                 # 即時 TUI
gpumaster --once          # 印出一個畫面後結束
gpumaster --json          # 一份 JSON 快照
gpumaster free [host]     # 現在值得用的 GPU（--min-mem GiB、-n COUNT、--format table|env|ssh|json）
gpumaster serve           # HTTP、curl ANSI、JSON，以及 Prometheus /metrics（--bind、--port）
gpumaster --mode NAME     # hosts.json 裡的一組主機
gpumaster --mobile        # 等同 --mode mobile；另有 --desktop、--laptop、--all
gpumaster check HOST      # 檢查一台機器的能力
gpumaster --probe HOST    # 原始遠端輸出，可存成測試樣本
gpumaster --replay DIR    # 用已擷取的樣本繪製，不走網路
gpumaster init            # 寫出 ~/.config/gpumaster/hosts.json
```

`master gpu` 是同一條命令。

## 設定

`gpumaster init` 會寫出 `~/.config/gpumaster/hosts.json`。也可以用 `-c FILE` 指定檔案。`User`、`Port`、`IdentityFile` 和 `ProxyJump` 寫在 `~/.ssh/config`，這裡只引用 SSH 別名：

```json
{
  "refresh_seconds": 2.0,
  "hosts": [
    {"name": "train-a", "target": "gpu-a", "tags": ["training"]},
    {"name": "spark", "target": "dgx-spark", "tags": ["gb10"]},
    {"name": "laptop", "target": "mac-laptop", "tags": ["laptop"], "hidden": true}
  ],
  "modes": {
    "desktop": ["train-a", "spark"],
    "laptop": ["laptop"]
  }
}
```

完整範例在 [`examples/gpumaster.hosts.json`](../../examples/gpumaster.hosts.json)。`"hidden": true` 的主機會離開預設畫面，免得一台大部分時間在睡眠的筆電一直顯示成紅色。`--all` 會把隱藏的主機也算進來。其他模式用 `name` 或 `target` 列出主機。

在 Apple 晶片上，若主機別名符合這台機器的晶片，就視為本機，不再開 SSH。M2 Max 對應 `m2x` 和 `mac-m2x`；Ultra 與 Pro 用同樣的規則（`u`、`p`）。寫成 `user@host`，或自帶 `ssh_args` 的目標，一律走 SSH。

執行時的 socket 放在 `/tmp/gm.$UID/`。這個路徑故意保持很短：macOS 會拒絕路徑達到 104 位元組的 SSH 控制 socket。

## 按鍵

| 按鍵 | 作用 |
|---|---|
| `↑` `↓` `j` `k` | 移動 GPU 選取 |
| `Enter` | 展開選取的 GPU |
| `Esc` `q` | 返回、清除 `/` 篩選、結束 |
| `/` | 依主機、GPU、使用者、容器、PID 或命令篩選 |
| `p` | 顯示或隱藏行程表 |
| `s` | 切換行程排序 |
| `S` | 切換主機排序（`config`、`util`、`free-mem`、`temp`） |
| `Tab` `n` `N` | 在詳細畫面選取行程 |
| `K` | 確認後遠端結束行程（先 `SIGTERM`，再 `SIGKILL`） |
| `h` | 隱藏或顯示閒置 GPU |
| `m` | 切換模式 |
| `g` | 方塊或 ASCII 圖形 |
| `d` | 傳輸診斷 |
| `f` | 凍結畫面；蒐集仍繼續 |
| `r` `R` | 立刻重新整理，或清除退避 |
| `+` `-` | 加快或放慢重新整理 |

## 怎麼讀一列

| 標記 | 意思 |
|---|---|
| `[+]` | 正在串流 |
| `[!]` | 主機是通的，但讀不到 GPU。CPU、RAM 和 PSI 仍是即時的 |
| `[x]` | SSH 失敗。只留一行，不會編造 GPU 列 |
| `[.]` | 在 `hosts.json` 裡被停用 |
| `*` | 推算出來的值 |
| `?` | 這個部位的數字不可靠 |
| `!` | 超過門檻 |
| `uma` | 統一記憶體。`used/total` 是共用池，不是獨立顯示記憶體 |
| `+` | `ps` 無法解析的 PID |

主機標題列包含 RAM、swap 和 PSI。一台機器可以四張 GPU 都閒著，卻因為主機記憶體耗盡而無法使用。

## 統一記憶體

DGX Spark / GB10 和 Apple 晶片沒有獨立的顯示記憶體。NVIDIA 在 GB10 上回報記憶體用量不受支援。這一列顯示的是 GPU 駐留記憶體（各行程 NVML 配置的加總，標成 `*`）除以整個共用池（`MemTotal`）。主機那一行再把這個池分成 GPU 持有、其他用途，以及仍然可用的部分。機群的 VRAM 合計只算獨立 GPU。統一記憶體的部分另有一行 UMA，因為把 GPU 記憶體再加到行程 RSS 上，會把同一批實體頁面算兩次。

## `free` 與 `serve`

```bash
gpumaster free --min-mem 20 -n 2 --format ssh
gpumaster serve --bind 127.0.0.1 --port 8765
curl -s localhost:8765/metrics
```

`free` 會跳過所在主機本身記憶體不足的 GPU。`serve` 是標準函式庫裡的一個小 HTTP 伺服器：ANSI 畫面、JSON，以及 Prometheus 文字。指標名稱是 `gpumaster_*`。

## 重播

```bash
gpumaster --probe gpu-a > captures/gpu-a.txt
gpumaster --replay captures --once
```

`--replay` 讀取 `*.txt` 樣本，以及可選的 `*.stderr`。它會忽略 AppleDouble 的 `._*` 檔案。
