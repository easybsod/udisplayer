# udisplayer · AVS／AVS+／AVS2 軟體解碼

本目錄提供在現代 Linux 上建置 AVS 系列解碼器所需的 **udisplayer 自有差異 Patch**、固定上游版本、建置工具與測試。第三方原始碼及第三方 Patch **由使用者執行腳本時從原作者倉庫下載**，不包含於本 Repository。播放器透過獨立的 FFmpeg CLI 將 AVS 視訊轉成 **H.264／AAC HLS**，並不直接連結 FFmpeg 函式庫。

AVS 系列視訊**全部依 CPU 軟體解碼**；有可用 GPU 時，後續 H.264 編碼可以使用 VA-API。4K50／10-bit 來源可能需要強大的 CPU，並不保證即時轉碼。

## 元件

| 內容 | 用途 |
| --- | --- |
| `patches/ffmpeg/avsplus-awq.patch` | udisplayer 的 FFmpeg AVS+ AWQ 解碼擴充（參考表建置時下載） |
| `patches/davs2/main10-udisplayer.patch` | 第三方 davs2 修補之後，我們自己的 RPS、SAO、跳幀等差異 |
| `manifests/sources.env` | 固定 FFmpeg／davs2／x264／libcavs／nilaoda 上游提交 |
| `manifests/patches.sha256` | 本 Repository 兩份 Patch 的 SHA-256 |
| `manifests/davs2-upstream-patches.sha256` | 第三方原始 Patch 的 SHA-256 |
| `scripts/build-davs2-10bit.sh` | 獨立建置 AVS2 10-bit 解碼器 |
| `scripts/build-ffmpeg.sh` | 產生 AVS+／AVS2 相容 FFmpeg，使用 GPLv3 相容配置 |
| `scripts/build-x264.sh` | 發行版缺少 x264 開發檔時的選用建置 |
| `scripts/verify_ffmpeg.py` | 檢查 FFmpeg HLS、解碼器、HTTPS 與 VA-API 能力 |
| `scripts/extract_awq_models.py` | 從下載的 `libcavs` 產生本地 AWQ 參考表 |
| `scripts/analyze_mpegts.py` | 本地 MPEG-TS 時戳與封包完整性診斷 |
| `tests/` | AVS2 保留影格、SIMD／SAO、跳幀狀態測試 |

## Linux 建置

請先閱讀 [完整 Linux 安裝與建置指南](docs/BUILD-LINUX.md)，安裝 GnuTLS、x264、nasm 與 C/C++ 開發工具。以下範例在專案根目錄執行，不會取代作業系統的 FFmpeg：

```bash
cd tools/avs
./scripts/check-local.sh

JOBS=4 ./scripts/build-davs2-10bit.sh /tmp/udisplayer-avs-davs2
export PKG_CONFIG_PATH="/tmp/udisplayer-avs-davs2/install/lib/pkgconfig:${PKG_CONFIG_PATH:-}"

WITH_LIBDAVS2=1 JOBS=4 ./scripts/build-ffmpeg.sh /tmp/udisplayer-avs-ffmpeg
python3 scripts/verify_ffmpeg.py \
  --ffmpeg /tmp/udisplayer-avs-ffmpeg/ffmpeg-src/ffmpeg --require-avs2
```

若只需要 AVS+ AWQ，無須先建置 davs2，也不用 `WITH_LIBDAVS2=1`。若 CPU 不支援 x86-64-v3，勿使用 `DAVS2_X86_V3=1`。

**第三方來源的套用次序：** `build-davs2-10bit.sh` 先從 `xatabhk/davs2-10bit` 下載固定提交，再從 `nilaoda/mpv-iina-avs` 下載 `0001`、`0012`、`0013` 三個原始 Patch 並驗證 SHA-256；最後才套用 `main10-udisplayer.patch`。本 Repository **不包含第三方 10-bit AVX2 Motion Compensation 原碼**。AVS+ 建置腳本另從 `xeonliu/libcavs` 取得固定版本的 AWQ 參考表，在本機建置目錄產生 C include 檔，而非在本 Repository 保存該表。

AVS2 `avs2_skip_nonref` 為實驗性功能，預設關閉；僅在具備完整 GOP 測試及畫面一致性基準的環境中開啟。CPU/GPU 縮放效果依硬體而異。

## 授權

udisplayer 自有播放器與自有輔助腳本採 MIT；**這不代表經過修改的 FFmpeg 或 davs2 程式碼改為 MIT**。FFmpeg 的 AVS 原始檔受 LGPL-2.1-or-later 管轄，davs2 受 GPL-2.0-or-later 管轄，外部 AVS2 Patch 須依原作者提供的實際授權使用；`libcavs` 參考表採 BSD-3-Clause。**使用者有責任先確認其選擇的第三方來源授權是否允許預期用途，尤其是來源倉庫可能存在的研究／非商業聲明。** 詳見 [THIRD_PARTY_NOTICES.md](../../THIRD_PARTY_NOTICES.md) 與 [LICENSES/](../../LICENSES/)。

本倉庫不提供完整 FFmpeg／davs2 原始碼、編譯二進位或廣播節目素材；如自行再散布客製化 FFmpeg 成品，仍須依其適用 GPL／LGPL 條款提供完整對應來源及建置資訊。
