# AVS／AVS+／AVS2 節目播放指南

**現代 Linux 的主要操作文稿：** [建置相容 udisplayer 的 AVS+／AVS2 Main10 FFmpeg](../tools/avs/docs/BUILD-LINUX.md)。本頁保留 AVS 格式、播放策略及故障排查；完整套件安裝、編譯、VA-API 驗證和 systemd 回退以新指南為準。

本文件說明如何在 **不修改、不內嵌 FFmpeg 的 `udisplayer`** 中測試 AVS 類型 IPTV 頻道。`udisplayer` 的職責是讀取 HTTP／HTTPS 來源、呼叫外部 FFmpeg，將視訊／音訊轉為瀏覽器可播放的 **H.264／AAC HLS**。**瀏覽器不需要支援 AVS；負責 AVS 解碼的是伺服器上的 FFmpeg。**

> **效能與相容性提醒：** 不同 AVS 廣播碼流、CPU、編碼器和網路環境的結果可能不同。正式使用前應自行驗證長時間播放、斷流恢復及多路並行。

## 1. 先決定需要哪一種解碼器

| IPTV 來源 | 目前採用的解碼方式 | 需要什麼 | 驗證程度 |
| --- | --- | --- | --- |
| 一般 AVS（舊式／非 AWQ） | FFmpeg 的內建 `cavs` | 可以先試發行版 FFmpeg，必須仍滿足 `udisplayer` 的 H.264／AAC 與 HTTPS 要求 | 依來源而異，不能保證每條碼流都相容 |
| **AVS+／AVS1-P16**（部分 1080i25 頻道） | FFmpeg `cavs` 加上 AWQ 修補、`cavsvideo` 解析器 | `tools/avs/` 的 FFmpeg Patch 與建置腳本 | 需以完整場畫面與音訊自行驗證 |
| **AVS2 Main10／4K50** | 客製化 **10-bit `libdavs2`**，由 FFmpeg `libdavs2` 解碼器呼叫 | 先建置 `libdavs2`，再讓客製版 FFmpeg 啟用 `--enable-libdavs2` | CPU 需求高，**不保證即時播放** |

這三者不是相同 codec；**只補 AVS+ AWQ，無法讓 FFmpeg 自動支援 AVS2 10-bit**。也不要把 AVS2 的 4K 解碼誤認為 H.264 硬體解碼：即使電腦具備 H.264 硬體編碼，AVS2 來源仍可能必須由 CPU 解碼。

## 2. 原始碼與建置工具位置

AVS 修補、固定上游版本、建置工具與測試位於 **`udisplayer/tools/avs/`**：

```text
udisplayer/
├── README.md
├── docs/avs.md                     # 播放和安裝指南
├── udisplayer/                     # Python/Web/HLS，僅呼叫外部 FFmpeg
└── tools/avs/
    ├── README.md                   # AVS 功能與建置入口
    ├── patches/ffmpeg/avsplus-awq.patch
    ├── patches/davs2/main10-udisplayer.patch # udisplayer 下游修改
    ├── manifests/sources.env       # 鎖定上游 SHA
    ├── manifests/patches.sha256    # 我們的 Patch 完整性驗證
    ├── manifests/davs2-upstream-patches.sha256 # 外部 Patch 的 SHA-256
    ├── scripts/                    # 編譯、檢查
    ├── tests/                      # 解碼器正確性／性能測試
    └── docs/BUILD-LINUX.md        # Linux 建置與使用指南
```

**主程式不內嵌 FFmpeg / libdavs2 完整原始碼、二進位檔或動態連結 FFmpeg 程式庫。** `tools/avs/` 僅存放 udisplayer 自己的差異 Patch 與編譯工具。第三方 Patch／參考表由使用者於建置時從原作者倉庫取得，核對 SHA-256 後在本地組合；不會隨服務執行而自動安裝客製 FFmpeg。即使不用 AVS 修補，`udisplayer` 仍可獨立使用發行版提供的 FFmpeg 播放其他格式。

**授權：** 播放器自有程式碼依 MIT 授權；FFmpeg／davs2 衍生 Patch 依其原始碼和第三方部分各自的 LGPL／GPL／BSD 條款提供，請參閱 [第三方授權聲明](../THIRD_PARTY_NOTICES.md) 與 [授權全文](../LICENSES/)。

## 3. 在 Linux x86-64 上隔離建置 AVS FFmpeg

以下以 Bash、單一 Repository 的 `tools/avs/` 與 `/tmp` 獨立目錄為例。先確保已準備 Git、GCC／G++、make、pkg-config、nasm／yasm、所需 FFmpeg 編譯依賴、**libx264 開發檔**與 **GnuTLS 開發檔**。套件名稱依發行版而異，例如 Rocky Linux 的 `gnutls-devel`；`x264` 的開發套件必須提供 `x264.pc`。不要為了編譯覆蓋作業系統原有的 FFmpeg。

以 Ubuntu／Debian 為例，可以先準備常用的建置套件：

```bash
sudo apt update
sudo apt install git build-essential pkg-config nasm libx264-dev libgnutls28-dev
```

Fedora／Rocky Linux 等 RPM 系統則需準備 `gcc`、`gcc-c++`、`make`、`git`、`pkgconf-pkg-config`、`nasm`、`gnutls-devel`，以及**從發行版相容套件庫**取得的 `x264-devel`。部分系統需要額外啟用多媒體套件庫，不能假設預設套件庫一定提供。若 `pkg-config --modversion x264` 或 `gnutls` 失敗，請先修正建置依賴，不能忽略腳本的拒絕訊息。

```bash
# 從 udisplayer 主專案根目錄進入 AVS 工具子目錄
cd tools/avs
./scripts/check-local.sh

# 這兩項均須成功；否則先安裝對應 -devel 套件或設定 PKG_CONFIG_PATH
pkg-config --modversion x264
pkg-config --modversion gnutls
```

`check-local.sh` 檢查資料夾、腳本及我們的 Patch 完整性，但不取代使用者對第三方授權的查核。兩個建置腳本會從原作者倉庫取得固定上游 commit（包含第三方 AVS2 Patch／AVS+ 參考表）、驗證 SHA-256、按順序套用修補，並在**指定的工作目錄**編譯，不會安裝到 `/usr/bin`，也不會重啟服務。工作目錄如果已有相同的原始碼目錄會拒絕覆寫，請使用新的測試路徑。

### A. 只需要 AVS+（1080i 等）

```bash
JOBS=4 ./scripts/build-ffmpeg.sh /tmp/udisplayer-avs-ffmpeg
```

這會使用固定版本 FFmpeg、AVS+ AWQ Patch、`cavsvideo` parser、`libx264`、AAC 與 GnuTLS／HTTPS；**不會**自動編入 10-bit AVS2。編譯後外部執行檔位置是：

```text
/tmp/udisplayer-avs-ffmpeg/ffmpeg-src/ffmpeg
```

### B. 同時需要 AVS2 4K50／10-bit

繼續在**同一個 `tools/avs` 目錄**執行：

```bash
# 1. 先編譯 10-bit libdavs2（含單元測試）
JOBS=4 ./scripts/build-davs2-10bit.sh /tmp/udisplayer-avs-davs2

# 2. 讓 FFmpeg 的 pkg-config 可以找到剛編譯的 davs2.pc
export PKG_CONFIG_PATH="/tmp/udisplayer-avs-davs2/install/lib/pkgconfig:${PKG_CONFIG_PATH:-}"
pkg-config --modversion davs2

# 3. FFmpeg 同時編入 AVS+ 與 AVS2 解碼器
WITH_LIBDAVS2=1 JOBS=4 ./scripts/build-ffmpeg.sh /tmp/udisplayer-avs-ffmpeg
```

這裡的 `/tmp/udisplayer-avs-ffmpeg` 必須是**尚未使用過的建置目錄**；若剛才已執行 AVS+ 專用建置，請改用 `/tmp/udisplayer-avs-ffmpeg-with-davs2` 等其他路徑，並在後續步驟同步調整 FFmpeg 路徑。

`DAVS2_X86_V3=1` 為另一項**明確選用**的編譯實驗，只能用在完全支援 x86-64-v3 的 CPU；不要因為是 64-bit 電腦就預設啟用。`UDISPLAYER_AVS2_SKIP_NONREF=1` 則是另一項**運行期、預設關閉**的實驗跳幀功能，兩者不是一回事。有 GPU、需要加速 H.264 編碼時，可在 FFmpeg 建置時增加 `WITH_VAAPI=1`（需要 libva、libdrm 開發套件）；完整驗證程序請依前述 Linux 指南執行。

## 4. 驗證新 FFmpeg 是否具備 `udisplayer` 必要能力

返回**主專案根目錄**檢查剛編譯的 FFmpeg，不要只看 `ffmpeg -version` 或系統 PATH：

```bash
cd ../..
FFMPEG_AVS=/tmp/udisplayer-avs-ffmpeg/ffmpeg-src/ffmpeg

"$FFMPEG_AVS" -hide_banner -decoders | grep -E 'cavs|libdavs2'
"$FFMPEG_AVS" -hide_banner -encoders | grep -E 'libx264|aac'
"$FFMPEG_AVS" -hide_banner -protocols | grep -E '^[[:space:]]*(http|https)[[:space:]]*$'
"$FFMPEG_AVS" -hide_banner -buildconf | grep -- '--enable-gpl'

# 使用 udisplayer 目前實際採用的 FFmpeg 啟動檢查
python3 -c 'import sys; from udisplayer.config import validate_ffmpeg; print(validate_ffmpeg(sys.argv[1]))' "$FFMPEG_AVS"
```

AVS+ 版本至少應有 `cavs`；包含 AVS2 的版本還應有 `libdavs2`。`validate_ffmpeg()` 會檢查 `libx264`、AAC、`bwdif`／`fps`／`scale` 等濾鏡、HLS／MPEG-TS muxer 和 HTTP／HTTPS 協定，但**不會**替你保證 AVS+ AWQ 或 AVS2 10-bit 的實際碼流都能正確解碼。

**特別注意：** 其他用途編譯的精簡 FFmpeg 即使列出 `cavs`、`libdavs2`，也可能缺少 `split` 等必要濾鏡或 HTTPS，導致 `validate_ffmpeg()` 失敗。**請以第 3 節的獨立建置腳本產物為基礎，並確實執行完整能力檢查**，不要將未驗證的二進位檔當成可部署版本。

如已具備合法的完整 MPEG-TS 測試檔，可以先確認 codec 與短時間解碼（請替換實際檔案路徑）：

```bash
"${FFMPEG_AVS%/*}/ffprobe" -v error -probesize 4000000 \
  -analyzeduration 4000000 \
  -show_entries stream=codec_name,width,height,r_frame_rate,profile \
  -of compact /path/to/sample.ts

"$FFMPEG_AVS" -hide_banner -v warning \
  -probesize 4000000 -analyzeduration 4000000 \
  -i /path/to/sample.ts -map 0:v:0 -an -frames:v 50 -f null -
```

`ffprobe` 能認出 `avs2` **不代表**有可用的 Main10 解碼器；還須確認 `libdavs2` 存在並實際產生畫面。AVS2 保留影格的全 Y／U／V 像素一致性驗證工具位於 `tools/avs/tests/compare-avs2.py`，須提供**完整 GOP 的 AVS2 Elementary Stream**，不能拿截斷的 MPEG-TS 直接當作該測試輸入。

## 5. 在 `udisplayer` 中試播 AVS 頻道

現在已回到**`udisplayer` 主專案根目錄**。確認已有透過 `--init-channel-list` 或其他方式建立的 `channels.json`，並且頻道是**伺服器可存取**的 HTTP／HTTPS URL（例如經 udpxy 轉出的 MPEG-TS）。現有轉碼程式預期來源同時包含視訊與音訊，只有影像、沒有音訊的來源可能因 `-map 0:a:0` 而失敗。

對 AVS2 4K50，可以只在該頻道的 `channels.json` 物件加入 `decoder_threads`：

```json
{
  "channels": [
    {
      "id": 1,
      "name": "AVS+ 範例",
      "url": "https://media.example.org/samples/avsplus.ts",
      "codec_hint": "cavs"
    },
    {
      "id": 2,
      "name": "AVS2 4K 測試",
      "url": "https://media.example.org/samples/avs2.ts",
      "codec_hint": "avs2",
      "decoder_threads": 4
    }
  ]
}
```

以上均為 **example.org 保留示範網址，沒有實際節目**。`decoder_threads` 必須是 **1～16 的整數**；不設定則使用全域 `--decoder-threads`，預設 2。此設定只影響輸入解碼，不會增加 `--encoder-threads`（H.264 軟體編碼）數量。

已確認 AVS／AVS2 的來源可以使用 `codec_hint`，讓轉碼器**直接 CPU 解碼並保留 VA-API H.264 硬編**，避免第一次以 VA-API 解碼器連線失敗才重新開始串流。`codec_hint` 只影響轉碼策略，不會強制覆蓋 FFmpeg 的真實 codec autodetection，請不要錯誤標記非 AVS 頻道。

對已標記 `codec_hint: "cavs"` 或 `"avs2"` 的頻道，瀏覽器 HLS.js 改為**穩定優先**配置：起播至少等待 3 個 HLS 分段、目標延遲 10 秒、最大延遲 26 秒，容忍不超過 0.5 秒的媒體時間戳小間隙；一般頻道維持原本初始 1 個分段和 6 秒的設定。會增加 AVS 啟播／直播延遲，但可降低短暫轉碼波動造成的停頓；**若轉碼長期低於 1.0×，增加緩衝無法根治 CPU 瓶頸**。

**4K AVS2 進一步調整：** 已透過實際影格 `interlaced_frame=0` 確認逐行掃描時，可設定 `"video_scan": "progressive"` 略過 `bwdif`，但仍維持 1080p25 H.264/AAC，短樣本實測僅快約 0.6%。另可在不同硬體實驗 `"avs2_gpu_scale": true`，但既有四 vCPU 測試環境上傳完整 4K 後由 GPU 縮放反而慢約 6～11%，所以**預設 false**。GPU 縮放失敗會先回退 CPU 縮放並保留 H.264 VA-API 硬編。

本地傳輸分析工具：`python3 tools/avs/scripts/analyze_mpegts.py /path/to/recording.ts`。它會掃描 TS CC、transport error、PES PTS/DTS 和時間戳回退，**不會連線上游**。

一部四 vCPU Linux VM 實測同一段 AVS2 4K50 碼流，`decoder_threads: 6` 比原本 4 執行緒更快，8～10 個執行緒沒有穩定額外收益。故只建議 該 VM 的 AVS2 4K 測試來源 測試 6，其他平台須自行量測。


```bash
# 強制純軟體測試，避免 GPU 驅動／硬解相容性干擾
python3 server.py \
  --channel-list channels.json \
  --ffmpeg "$FFMPEG_AVS" \
  --host localhost --port 8081 \
  --hw-render none \
  --encoder-threads 2 --max-streams 1 --debug
```

試播時從同一台電腦開啟 `http://localhost:8081/`。測試成功後也**不要立刻取代系統 FFmpeg 或正式服務**；上述建置產物位於 `/tmp`，重開機或暫存清理後可能消失。正式安裝與 systemd 的 `--ffmpeg` 絕對路徑、檔案權限及相依套件仍需獨立規劃。

### AVS2 非參考 B-frame 跳幀（只供隔離實驗）

僅在已取得正確的 FFmpeg／AVS2 完整解碼基準、測試樣本的畫面一致性通過後，才考慮對**隔離的測試行程**設定：

```json
{
  "id": 2,
  "name": "AVS2 4K 測試",
  "url": "https://media.example.org/samples/avs2.ts",
  "codec_hint": "avs2",
  "decoder_threads": 4,
  "avs2_skip_nonref": true
}
```

`avs2_skip_nonref` 預設 `false`，必須是布林值，且只能用於 `codec_hint: "avs2"` 的頻道。只對此頻道的 FFmpeg 子行程開啟實驗性跳幀，不會使其它頻道一起啟用；可以在不更動 FFmpeg 安裝的情況下恢復成 `false` 並重新啟動服務。

如果需要獨立執行解碼器研究測試，也可以對**測試行程**設定環境變數：

```bash
UDISPLAYER_AVS2_SKIP_NONREF=1 python3 server.py \
  --channel-list channels.json --ffmpeg "$FFMPEG_AVS" \
  --host localhost --port 8081 --hw-render none \
  --encoder-threads 2 --max-streams 1 --debug
```

這個環境變數會傳給該 `udisplayer` 行程產生的 FFmpeg 子行程，**不是單一頻道的設定**。它僅針對 Patch 判定符合條件的 AVS2 Main10／50～60fps 非參考 B-frame 嘗試略過，必須執行必要的參考影格管理，且無法保證所有廣播 GOP 模式都安全。**禁止在未完成長時測試前將其加入正式 systemd 環境。**

目前仍固定輸出 H.264／AAC、最大 1920×1080／30fps；一般 50fps 來源轉成 **25fps**，不是 24fps。效能不足時，降低成 24fps 也不會大幅減少 AVS2 4K 必需的參考影格解碼計算。

## 6. 常見故障與判斷方式

| 現象 | 優先檢查 |
| --- | --- |
| `libx264 development files are required` | 建置環境缺少 x264 header／`x264.pc`，或 `PKG_CONFIG_PATH` 不正確；與運行時缺少編碼器是兩種不同問題 |
| `GnuTLS development files are required` | 安裝相容的 GnuTLS 開發檔；沒有 HTTPS 的客製 FFmpeg 無法通過 `udisplayer` 啟動檢查 |
| `WITH_LIBDAVS2=1 requires libdavs2 development files` | 先完成 davs2 建置並匯出其 `install/lib/pkgconfig`；確認 `pkg-config --modversion davs2` 成功 |
| `FFmpeg 缺少必要能力` | 使用第 4 節 `validate_ffmpeg()` 檢查，避免啟動時不小心使用 PATH 中另一份 FFmpeg |
| `Un-supported bit-depth 10` 或沒有 `libdavs2` | 仍使用 8-bit davs2 或 FFmpeg 沒有編入 Main10 函式庫；僅 `ffprobe` 識別到 AVS2 並不足夠 |
| AVS+ 顯示 `AEC picture without a slice header` | 確認 FFmpeg 使用 AWQ Patch、`cavsvideo` parser，來源是否從場畫面中途／PES 邊界加入；比較完整錄製檔 |
| `PES packet size mismatch`、`Packet corrupt` | 可能是錄製檔尾端截斷或上游丟包；應使用完整 GOP 重測，不要直接當作解碼器演算法錯誤 |
| 來源正常但一直顯示載入中 | 檢查來源是否有音訊、第一段 HLS 是否產生、`--debug` 日誌及 `--ffmpeg` 實際路徑 |
| AVS2 4K50 CPU 長期滿載、延遲持續累積 | 目前普通 4 vCPU 尚不保證即時處理。先限制同時轉流數，測試 `decoder_threads` 2／4，注意 x264 編碼及縮放也消耗 CPU；增加執行緒不代表必定更快 |
| 環境切換後出現破圖／音畫不同步 | 先關閉 `UDISPLAYER_AVS2_SKIP_NONREF`，重測完整解碼；仍有問題時保留不含個資的 PTS、FFmpeg 警告及短樣本進行隔離分析 |

## 7. 授權、散布與正式部署邊界

- `udisplayer` 不包含完整 FFmpeg、libdavs2、x264 上游原始碼或二進位檔，也不與 libav* 程式庫連結；透過獨立 FFmpeg CLI／子行程轉碼。**差異 Patch 不因位於 MIT 專案中就改採 MIT 授權**；請保留 FFmpeg／davs2 原始碼及第三方修改的授權聲明。
- FFmpeg 建置啟用 `--enable-gpl --enable-version3`、libx264 和 GnuTLS，不使用 `--enable-nonfree`。若再散布客製 FFmpeg 二進位，須依實際適用的條款提供完整對應源碼、修改、建置資訊與必要的通知。
- 解碼器可能涉及另外的影音專利義務；開源授權不會自動提供對節目來源的重傳權。
- 主專案不會代為安裝客製版 FFmpeg；應使用獨立安裝目錄，保留回退原版 FFmpeg 的路徑，並先測試長時間播放／斷流恢復／不同節目格式。

完整建置命令與 Linux 套件需求請參閱 [AVS Linux 建置指南](../tools/avs/docs/BUILD-LINUX.md)。
