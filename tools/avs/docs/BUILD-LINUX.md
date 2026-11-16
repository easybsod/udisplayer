# Linux：建置並套用支援 AVS+／AVS2 Main10 的 FFmpeg

本指南以 **udisplayer 原始碼根目錄**為起點，適用於現代 Linux x86-64（例如 Debian 12、Ubuntu 24.04 LTS、Fedora、Rocky Linux、AlmaLinux）。目標是由外部 FFmpeg 解碼 AVS+／AVS2 4K50 10-bit 串流，再由 `udisplayer` 輸出 **H.264/AAC HLS，最高 1080p30，50fps 通常輸出 25fps**。

`udisplayer` 不嵌入 FFmpeg、不直接連結 `libavcodec`；`tools/avs/` 只包含 Patch、來源版本鎖定、建置及測試工具。下列操作不會自動修改系統的 `/usr/bin/ffmpeg`、既有頻道或 systemd 服務。

> **授權提醒：** 建置或重新散布客製 FFmpeg／libdavs2 須遵守它們各自的 GPL／LGPL 條款、第三方程式碼的適用聲明及完整原始碼提供義務。主程式 MIT 授權不會取代這些要求。參見 [第三方授權聲明](../../../THIRD_PARTY_NOTICES.md)。

## 1. 解碼器與輸出管線

| 原始來源 | 解碼路徑 | 最終輸出 |
|---|---|---|
| AVS／AVS+（AVS1-P16、AWQ） | FFmpeg `cavs`、AWQ Patch、`cavsvideo` parser | H.264/AAC HLS |
| AVS2 Main10（4K／50fps／10-bit） | 經修補的 **10-bit libdavs2 CPU 軟解** | H.264 1080p25/AAC HLS |
| H.264 輸出 | 有支援 GPU 時 VA-API 硬編，否則 `libx264` 軟編 | 瀏覽器不需原生 AVS 解碼 |

注意：VA-API 硬編 H.264 **不等於** AVS2 硬解，4K50 CPU 解碼仍可能無法達到即時。只有 `ffprobe` 偵測到 AVS2 或 FFmpeg 列出 `libdavs2`，也不能證明 Main10 真正可解碼；後續需測 10-bit 碼流。

## 2. 準備 Linux 開發套件

Debian 12／Ubuntu 24.04 LTS：

```bash
sudo apt-get update
sudo apt-get install -y \
  git build-essential pkg-config nasm python3 \
  libx264-dev libgnutls28-dev libva-dev libdrm-dev
```

Fedora／Rocky Linux／AlmaLinux：

```bash
sudo dnf install -y \
  git gcc gcc-c++ make pkgconf-pkg-config nasm python3 \
  gnutls-devel libva-devel libdrm-devel
# 如果目前的多媒體套件庫有提供，才安裝：
sudo dnf install -y x264-devel
```

某些 RPM 發行版的預設套件庫不提供 `x264-devel`；**不是安裝程序失敗就無法繼續**，第 3 節可從固定 SHA 獨立編譯 x264。若只要軟編 H.264，`libva`、`libdrm` 可以省略，且第 4 節不使用 `WITH_VAAPI=1`。

先檢查：

```bash
command -v git gcc g++ make pkg-config nasm python3
pkg-config --modversion gnutls
pkg-config --modversion x264 || echo '請使用第 3 節建置 x264'
pkg-config --modversion libva libva-drm libdrm   # VA-API 選用
```

請保留 assembler（nasm／yasm）的 x86 SIMD 最佳化，不要為了跳過依賴而把解碼效率大幅犧牲掉。

## 3. 隔離建置 x264（選用）與 10-bit libdavs2（AVS2 必要）

以下在 `udisplayer` 主專案根目錄執行：

```bash
cd tools/avs
./scripts/check-local.sh
export AVS_BUILD="$HOME/.cache/udisplayer-avs-build"
mkdir -p "$AVS_BUILD"
```

`manifests/sources.env` 固定 FFmpeg、davs2、x264、libcavs、nilaoda 上游 commit；`manifests/patches.sha256` 僅包含我們的差異 Patch，另以 `manifests/davs2-upstream-patches.sha256` 驗證**使用者建置時從第三方倉庫下載**的 `0001`、`0012`、`0013` 原始補丁。AVS+ AWQ 模型表也從固定版本的 `libcavs` 取回，在本地生成，不存入本 Repository。腳本拒絕覆寫已存在的原始碼樹，重建請換一個工作目錄。

若 `pkg-config --exists x264` 不成立，先執行**選用**的固定 SHA、PIC 靜態 x264 編譯：

```bash
JOBS=4 ./scripts/build-x264.sh "$AVS_BUILD/x264"
export PKG_CONFIG_PATH="$AVS_BUILD/x264/install/lib/pkgconfig:$PKG_CONFIG_PATH"
pkg-config --modversion x264
```

AVS2 Main10 **必須**先編譯 10-bit libdavs2：

```bash
JOBS=4 ./scripts/build-davs2-10bit.sh "$AVS_BUILD/davs2"
export PKG_CONFIG_PATH="$AVS_BUILD/davs2/install/lib/pkgconfig:$PKG_CONFIG_PATH"
pkg-config --modversion davs2
```

這會編譯帶 PIC 的靜態 libdavs2，並測試跳幀狀態與 SIMD／SAO。`DAVS2_X86_V3=1` 只供完全支援 x86-64-v3 CPU 的明確實驗，**不是預設，也不推薦為小幅加速犧牲舊 CPU 相容性**。

## 4. 建置 FFmpeg：包含 AVS+ AWQ 與 AVS2 Main10

**以下二選一**，不要在同一工作目錄重複建置。

需要 Linux VA-API H.264 硬編的 GPU 主機：

```bash
WITH_LIBDAVS2=1 WITH_VAAPI=1 JOBS=4 \
  ./scripts/build-ffmpeg.sh "$AVS_BUILD/ffmpeg"
export FFMPEG_AVS="$AVS_BUILD/ffmpeg/ffmpeg-src/ffmpeg"
```

沒有 GPU，或打算完全使用 CPU 轉碼：

```bash
WITH_LIBDAVS2=1 JOBS=4 \
  ./scripts/build-ffmpeg.sh "$AVS_BUILD/ffmpeg-cpu"
export FFMPEG_AVS="$AVS_BUILD/ffmpeg-cpu/ffmpeg-src/ffmpeg"
```

建置腳本會自動套用我們的 AWQ Patch，並明確啟用 `--enable-gpl`、`--enable-version3`、`--enable-libx264`、`--enable-gnutls`、`--enable-parser=cavsvideo`、`--enable-libdavs2`。AVS2 先套用原作者的三份 Patch，然後才套用 udisplayer 下游補丁；只下載到使用者的隔離建置目錄。指定 `WITH_VAAPI=1` 時會檢查 libva、libva-drm、libdrm 開發檔並要求 VA-API，避免本來要硬編卻因依賴缺失而被默默停用。**不使用 `--enable-nonfree`。** GPLv3 版本旗標不能替使用者取得第三方未授予的權利。

產出在 `$AVS_BUILD/ffmpeg/ffmpeg-src/`（CPU-only 版本則是 `ffmpeg-cpu/ffmpeg-src/`），包括 `ffmpeg`、`ffprobe`。建置完成後會自動呼叫 [verify_ffmpeg.py](../scripts/verify_ffmpeg.py) 做與播放器相同的 FFmpeg 能力檢查，避免只驗證了版本、卻缺少 HTTPS、AAC、x264、HLS muxer 或 `split` 等必要濾鏡。

## 5. 驗證 Main10 解碼及可選的 GPU 硬編

以下仍在 `tools/avs/` 工作目錄內：

```bash
# 先檢查 udisplayer 的所有 FFmpeg 基本需求，另要求 AVS2
python3 scripts/verify_ffmpeg.py \
  --ffmpeg "$FFMPEG_AVS" --require-avs2

# VA-API 建置時，檢查 H.264 VA-API encoder、scale_vaapi 等能力
python3 scripts/verify_ffmpeg.py \
  --ffmpeg "$FFMPEG_AVS" --require-avs2 --require-vaapi
```

若有 GPU，可額外測試**真實硬體編碼**，而不只依賴 `ffmpeg -encoders` 名稱；下列 `/dev/dri/renderD128` 是 Linux DRM 裝置檔名，**不是內網 IP**，請依實際存在裝置修改：

```bash
ls /dev/dri/renderD*
python3 scripts/verify_ffmpeg.py \
  --ffmpeg "$FFMPEG_AVS" --require-avs2 \
  --vaapi-device /dev/dri/renderD128
```

此測試只用 FFmpeg 自產的影像編碼 10 幀 H.264，不需要任何外部頻道。如果 GPU 編碼失敗，先排查驅動、權限或硬體能力；軟體解碼仍可獨立運作。

若有**合法取得、完整 GOP 的本地 AVS2 Main10 MPEG-TS 樣本**，再進行 50 幀實際解碼：

```bash
python3 scripts/verify_ffmpeg.py \
  --ffmpeg "$FFMPEG_AVS" --require-avs2 \
  --sample-ts "$HOME/videos/avs2-sample.ts"
```

這一步用來證明 **10-bit 碼流真正能解出畫面**；只有 `ffprobe` 顯示 AVS2 或 `-decoders` 顯示 `libdavs2` 並不足夠。樣本不應加入 Git；要對比保留影格的完整 YUV 雜湊，可使用 `tests/compare-avs2.py`，但它需要已解封裝、具完整 GOP 的 AVS2 **Elementary Stream**。

## 6. 安裝獨立 FFmpeg 路徑，保留可回退版本

先確認上述檢查通過，再安裝到不與 `/usr/bin/ffmpeg` 衝突的永久版本目錄：

```bash
export AVS_PREFIX=/opt/udisplayer-media/avs-main10-v1
sudo install -d -m 755 "$AVS_PREFIX/bin"
sudo install -m 755 "$FFMPEG_AVS" "$AVS_PREFIX/bin/ffmpeg"
sudo install -m 755 "$(dirname "$FFMPEG_AVS")/ffprobe" "$AVS_PREFIX/bin/ffprobe"

ldd "$AVS_PREFIX/bin/ffmpeg"
sha256sum "$FFMPEG_AVS" "$AVS_PREFIX/bin/ffmpeg"
python3 scripts/verify_ffmpeg.py \
  --ffmpeg "$AVS_PREFIX/bin/ffmpeg" --require-avs2
```

如果 `ldd` 顯示 `not found`，就必須先補齊對應動態函式庫。libdavs2／自建 x264 可以靜態連結，但 GnuTLS、libva 以及系統函式庫仍可能是動態依賴；**不同 Linux 版本之間直接複製 FFmpeg 二進位不保證可執行**。下次升級請建立 `avs-main10-v2` 等新目錄，不要覆蓋舊檔。

## 7. 在 udisplayer 啟動參數與頻道設定套用

回到主專案根目錄，建立配置檔；若已有配置檔，`--init-config` 會拒絕覆寫，請保留現有頻道並只追加新項目：

```bash
cd ../..
python3 server.py --config channels.json --init-config
```

以下只展示格式，**沒有任何真實頻道、個人資料或內網資訊**。`example.org` 是保留的範例網域，不能拿來實際播放：

```json
{
  "channels": [
    {
      "id": 1,
      "name": "AVS+ test",
      "url": "https://media.example.org/samples/avsplus.ts",
      "codec_hint": "cavs"
    },
    {
      "id": 2,
      "name": "AVS2 Main10 4K50 test",
      "url": "https://media.example.org/samples/avs2.ts",
      "codec_hint": "avs2",
      "decoder_threads": 4,
      "avs2_skip_nonref": false
    }
  ]
}
```

`codec_hint` 指定 `cavs`／`avs2` 後，播放器會直接用 CPU 解 AVS，避免先嘗試不支援的 VA-API AVS 解碼；如果 GPU 能進行 H.264 硬編，仍會選擇 H.264 VA-API。`decoder_threads`（合法 1～16）是**每頻道解碼器**獨立設定，並不等同 `--encoder-threads`；建議比較 2、4、6，不要假定執行緒越多越好。

只在已驗證 `interlaced_frame=0` 的逐行掃描來源使用 `"video_scan": "progressive"`，以略過 `bwdif`；預設 `auto` 維持原有去交錯。實驗性 `"avs2_gpu_scale": true` 只允許 AVS2＋progressive，且有 H.264 VA-API 硬編才生效。既有四 vCPU 測試上 GPU 縮放慢約 6～11%，**預設不要啟用**。新 GPU 上測試如失敗，會先退回 CPU 縮放，保留 VA-API 硬編。

先用純軟體方式在本機驗收：

```bash
python3 server.py --config channels.json \
  --ffmpeg "$AVS_PREFIX/bin/ffmpeg" \
  --host localhost --port 8081 \
  --hw-render none --max-streams 1 --debug
```

在瀏覽器打開 `http://localhost:8081/`，換成合法、可供這部機器存取的來源後，確認 HLS 影片與聲音都正常。若已通過 GPU 硬編測試，再移除 `--hw-render none` 讓系統自動選擇 DRM Render Node，或自行指定 `--hw-render=/dev/dri/renderD128`。4K50 在純 CPU 軟解加軟編路徑通常負擔極高，應實測 `speed` 是否超過 1.0×。

### AVS2 非參考影格選擇性解碼（明確 opt-in）

`avs2_skip_nonref` 預設 **false**，只允許在 `codec_hint: "avs2"` 的頻道中設為 `true`。此 Patch 在有限的完整 GOP 樣本中通過參考影格 RPS／POC 與保留影格 YUV 雜湊比對，但尚未驗證所有斷流、損壞封包及長時多路情況。

**先以 false 做完整解碼基準，確認畫質與影音同步後才針對單一 4K 測試頻道開啟**；若破圖或接續錯位，立即恢復為 false。不應為所有 FFmpeg 子行程設定全域 `UDISPLAYER_AVS2_SKIP_NONREF=1`。50fps 來源通常以 **25fps** 輸出，不會為了效能偷偷改為 24fps。

## 8. 套用至既有 systemd 服務與回退

先備份現有 unit、drop-in 和 `channels.json`，並記下原本可用的 FFmpeg 路徑。透過 `systemctl cat udisplayer` 檢查原本的完整 `ExecStart`，**只替換 `--ffmpeg` 路徑**，保留其餘的 `--config`、`--path`、`--host`、GPU 和服務參數。

systemd drop-in 若覆寫 `ExecStart`，必須先以 `ExecStart=` 清空後重寫**完整原命令**。以下只是示範結構，路徑與參數應由實際 unit 決定，**不可無條件覆蓋既有服務**：

```ini
[Service]
ExecStart=
ExecStart=/usr/bin/python3 /srv/udisplayer/server.py --config /etc/udisplayer/channels.json --ffmpeg /opt/udisplayer-media/avs-main10-v1/bin/ffmpeg --host localhost --port 8081
```

```bash
sudo systemctl edit udisplayer
sudo systemctl daemon-reload
sudo systemctl restart udisplayer
sudo systemctl is-active udisplayer
sudo journalctl -u udisplayer -n 60 --no-pager
```

**回退：** 把 `--ffmpeg` 換回備份的舊版執行檔，`daemon-reload`、`restart`，再確認服務 `active`。不要在未檢查所有 unit drop-in 時直接使用 `systemctl revert udisplayer`，以免連原本的其它客製設定一併撤銷。

## 9. 常見問題及安全／授權限制

| 問題 | 排查方向 |
|---|---|
| `pkg-config` 找不到 `x264` | 安裝 x264 開發檔，或 `build-x264.sh` 加上 `PKG_CONFIG_PATH` |
| FFmpeg 缺少 HTTPS | 安裝 GnuTLS 開發檔並以 `--enable-gnutls` 編譯 |
| 缺少 `split`、`bwdif`、HLS | 執行 `verify_ffmpeg.py`；不能只檢查版本號或 decoder 名稱 |
| `libdavs2` 無法解 10-bit | 確認固定 Main10 fork、正確 `davs2.pc` 與實際 10-bit 樣本 |
| VA-API 編碼失敗 | 檢查 `libva`／`libdrm`、GPU 驅動、DRM Render Node 權限與硬體支援 |
| 4K AVS2 持續卡頓 | `speed < 1.0x` 表示長期產能不足；比較解碼執行緒、硬編及 HLS 分段生成速度 |
| 跳幀後破圖／影音不同步 | 先關閉單一頻道 `avs2_skip_nonref`，再以完整 GOP 比對 |

**授權與發行：** 發布自己建置的 FFmpeg／davs2 二進位時，應保留完整對應原始碼、適用授權全文、第三方通知與建置資訊，並依實際授權條款提供取得方式。影音專利及內容播送權利另行適用。

遇到 `Packet corrupt`、PTS／DTS 時間戳異常，請用 `python3 tools/avs/scripts/analyze_mpegts.py /path/to/recording.ts` 對**本地合法錄製檔**分別檢查 TS CC、transport error、discontinuity 和 PES PTS／DTS。工具不會再向上游取串流，也不會占用線上轉碼效能。

更多播放器設定與 AVS 故障排除請參考 [AVS 播放指南](../../../docs/avs.md)。
