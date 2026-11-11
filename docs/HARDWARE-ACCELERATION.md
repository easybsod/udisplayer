# Linux VA-API hardware acceleration

udisplayer can use DRM render nodes for optional hardware video decoding and
H.264 encoding. Its HTTP API, HLS output, frontend, channel JSON, and AAC
audio path are unchanged. Each active channel continues to have exactly one
FFmpeg worker and viewers of the same channel share that worker.

## Quick start

**No special switch is required to enable hardware acceleration.** On Linux,
udisplayer automatically selects the first accessible DRM render node. Once
the VA-API runtime and GPU driver are installed, launch the application with
its usual arguments:

```sh
python3 server.py --debug
```

To use a specific GPU, or to deliberately test software playback:

```sh
python3 server.py --hw-render=/dev/dri/renderD128 --debug
python3 server.py --hw-render=none
```

For the packaged Linux executable, replace `python3 server.py` with
`./udisplayer-linux-x86_64`. These options do not change the channel JSON or
require per-channel codec configuration.

## Command-line selection

```sh
python3 server.py --ffmpeg /path/to/ffmpeg
python3 server.py --hw-render=/dev/dri/renderD128
python3 server.py --hw-render=none
```

- Omit `--hw-render` to select the first accessible `/dev/dri/renderD*` on
  Linux. If none exists, use software without failing startup.
- `--hw-render=none` disables **all** hardware decoding and encoding.
- An explicit device must be a valid, accessible DRM render node. Invalid or
  unavailable devices fail startup; no alternate GPU is silently substituted.
- With several GPUs, inspect `ls -l /dev/dri/by-path/*-render` and pass the
  existing `pci-*-render` symlink under `/dev/dri/by-path/` to `--hw-render`.
  This avoids relying on a render-node number that can change across boots.
- Input formats are not restricted by a codec switch. FFmpeg automatically
  recognizes the source: hardware decode is preferred when usable, otherwise
  playback falls back to software decode. Output remains H.264/AAC.
- No custom `LIBVA_*` environment variables are required; the installed
  `libva` and GPU driver determine hardware support.

## Runtime prerequisites

On Linux the selected FFmpeg needs VA-API support, `libva`, `libva-drm`, a
GPU-specific VA driver, and read/write access to the DRM render node.
The kernel graphics driver alone does not provide VA-API user-space support.

Example for **Ubuntu / Debian with supported Intel GPUs**:

```sh
sudo apt update
sudo apt install -y vainfo libva-drm2 intel-media-va-driver
```

Example for **Rocky Linux / other RHEL-family systems with supported Intel
GPUs**, after enabling compatible repositories for the OS release:

```sh
sudo dnf install -y libva intel-gmmlib
# Some releases provide intel-media-driver through RPM Fusion Nonfree:
sudo dnf install -y intel-media-driver
```

Install `vainfo` / `libva-utils` separately where the distro provides it.
On AMD or other GPUs, install the corresponding Mesa / vendor VA-API driver
instead of Intel's driver. Package names, available codec profiles and
distribution licensing differ; check your distribution's package sources.

Verify the device, driver, and FFmpeg capabilities:

```sh
ls -l /dev/dri/renderD*
vainfo --display drm --device /dev/dri/renderD128
ffmpeg -hide_banner -hwaccels
ffmpeg -hide_banner -encoders | grep h264_vaapi
```

For relevant codecs, `vainfo` reports `VAEntrypointVLD` for decoding and
`VAEntrypointEncSlice` or `VAEntrypointEncSliceLP` for encoding. These are
driver capabilities, **not** proof that a particular source stream is
hardware-decodable or that the exact FFmpeg encoder configuration will work.

`ffmpeg -encoders` listing `h264_vaapi` is insufficient: startup probes the
selected device, then **actually encodes one frame** with `h264_vaapi`.
Variable bitrate control (VBR) is tried first to honour the 2.2 Mbps VBV
budget; if the driver rejects VBR the default driver rate control is probed
separately and may not enforce the same VBV bounds.
The decoder is considered *eligible* if device initialization succeeds;
actual H.264/HEVC decode capability is determined on the running source.
Drivers that cannot hardware-decode a particular stream automatically fall
back without denying software playback.

When no render node or working VA-API driver exists, the application retains
its CPU software pipeline. If an **explicitly** selected device is missing
or inaccessible, startup instead fails with a clear configuration error.

## Permissions and systemd

The service account needs read/write access to the selected render node.
On many distributions this is managed by the `render` group:

```sh
ls -l /dev/dri/renderD128
id -nG
```

For a user-managed service, ensure that the user belongs to the `render`
group (or is granted equivalent device access), then restart the user session
or service. Avoid running the web server as root just to access a GPU.

For a **system** service with `PrivateDevices=true`, the GPU node is hidden
inside the service namespace even if it is visible to the host. An example
systemd drop-in for a known render node is:

```ini
[Service]
PrivateDevices=no
SupplementaryGroups=render
DevicePolicy=closed
DeviceAllow=/dev/dri/renderD128 rw
```

Apply this to your own system service with
`sudo systemctl edit udisplayer.service`, then reload and restart it:

```sh
sudo systemctl daemon-reload
sudo systemctl restart udisplayer.service
sudo systemctl show udisplayer.service -p PrivateDevices -p DevicePolicy -p DeviceAllow -p SupplementaryGroups
```

This example permits only the named device through systemd's device policy;
adjust the path if your GPU uses a different render node. Other sandbox
controls and group memberships must be compatible with the service account.
No special systemd drop-in is required when the existing service already has
access to `/dev/dri/`.

For container deployments, expose the render device to the container and
ensure permissions match its runtime user. Merely installing the VA-API
packages in the container is not sufficient.

## Independent decoder and encoder fallback

For each channel, the preferred decoder is VA-API if available, otherwise
software. Independently, H.264 encoding prefers VA-API if available, otherwise
`libx264`. If a hardware decoder fails, only decoding changes to software;
the H.264 hardware encoder remains active. Conversely, on a hardware encoder
failure, only encoding changes to software and the hardware decoder stays.
When FFmpeg cannot identify the failing side, decoding is tried in software
first; if the issue persists, encoding also falls back. No codec-specific
whitelist or ban is applied to the incoming stream.

On FFmpeg exit, initial segment timeout or HLS stall, the affected channel
falls back only as needed.
Unrelated channels are not restarted. A fallback preserves the channel token,
viewer sessions and HLS URL, starts higher-numbered HLS segments, and marks
the new playlist with `EXT-X-DISCONTINUITY`. Viewers may experience a brief
rebuffer while the next complete segment is encoded. Existing exhausted
backend/cooldown behaviour still applies if every candidate fails.

`--debug` reports the selected transcoder and fallback decisions in the
service journal. GPU probe warnings are also logged on startup.

## Confirm acceleration is active

Launch with `--debug`, or inspect the journal for a system service:

```sh
sudo journalctl -u udisplayer.service -b --no-pager | grep -E 'VA-API device=|transcoder=|transcoder_fallback'
```

For example, the startup line may contain `device_ok=True`,
`h264_encode=True` and `rc_mode=VBR`. A channel line such as
`transcoder=vaapi_decode_encode` means the FFmpeg worker was started with
hardware decoding **and** hardware encoding; `transcoder=vaapi_encode` means
software decoding with hardware encoding. If a hardware stage fails, check
the `transcoder_fallback` lines for the selected fallback profile.

If startup shows `h264_encode=False`, check GPU driver installation, the
selected FFmpeg build, and device permissions. If your source cannot be
hardware-decoded, the source remains eligible for software decoding without
losing a working H.264 hardware encoder. Different source formats and GPU
generations may support different acceleration paths.

## Media-processing constraints

- All profiles preserve deinterlacing with the existing CPU `bwdif` filter,
  sample-aspect-ratio normalization, aspect-preserving 1920×1080 ceiling,
  2-second HLS segments, and AAC audio output.
- GPU decoding uses VA-API surfaces and converts them to NV12 on the GPU,
  then downloads frames for the existing deinterlacing and sizing filters.
  If hardware encoding is selected, the resulting NV12 frames are uploaded
  again. **This is not a GPU zero-copy pipeline.** Full hardware-only VPP may
  be considered separately after source-specific aspect and interlace tests.
- Output is always H.264 8-bit; HEVC is accepted as an **input** and uses the
  same hardware-preferred decode policy. There is no HEVC or VP9 HLS output.
- Concurrent transcoding throughput is limited by the GPU, shared memory
  bandwidth, software VPP and the driver's supported parallel sessions.

## Acceptance testing

```sh
python3 -m unittest discover -s tests -v
python3 server.py --help
```

With an actual GPU driver installed, test representative live channels in
H.264 progressive, HEVC 8-/10-bit, 1080i interlaced and non-square-pixel
formats. Validate `vainfo`, real encoded frame production, HLS playback,
audio sync, resolution/ratio, multiple concurrent channels and stop/restart
recovery. Deliberately remove driver access in a **staging** environment to
check per-channel fallback. Do not infer GPU throughput from the presence of
the FFmpeg encoder name or from a synthetic single-frame probe.
