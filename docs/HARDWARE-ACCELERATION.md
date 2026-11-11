# Linux VA-API hardware acceleration

udisplayer can use DRM render nodes for optional hardware video decoding and
H.264 encoding. Its HTTP API, HLS output, frontend, channel JSON, and AAC
audio path are unchanged. Each active channel continues to have exactly one
FFmpeg worker and viewers of the same channel share that worker.

## Command-line selection

```sh
python3 server.py --ffmpeg /path/to/ffmpeg
python3 server.py --hw-render=/dev/dri/renderD128
python3 server.py --hw-render=/dev/dri/by-path/pci-0000:01:00.0-render
python3 server.py --hw-render=none
```

- Omit `--hw-render` to select the first accessible `/dev/dri/renderD*` on
  Linux. If none exists, use software without failing startup.
- `--hw-render=none` disables **all** hardware decoding and encoding.
- An explicit device must be a valid, accessible DRM render node. Invalid or
  unavailable devices fail startup; no alternate GPU is silently substituted.
- Input formats are not restricted by a codec switch. FFmpeg automatically
  recognizes the source: hardware decode is preferred when usable, otherwise
  playback falls back to software decode. Output remains H.264/AAC.
- No custom `LIBVA_*` environment variables are required; the installed
  `libva` and GPU driver determine hardware support.

## Runtime prerequisites

On Linux the selected FFmpeg needs VA-API support, `libva`, `libva-drm`, a
GPU-specific VA driver, and read/write access to the DRM render node.
`ffmpeg -encoders` listing `h264_vaapi` is insufficient: startup probes the
selected device, then **actually encodes one frame** with `h264_vaapi`.
Variable bitrate control (VBR) is tried first to honour the 2.2 Mbps VBV
budget; if the driver rejects VBR the default driver rate control is probed
separately and may not enforce the same VBV bounds.
The decoder is considered *eligible* if device initialization succeeds;
actual H.264/HEVC decode capability is determined on the running source.
Drivers that cannot hardware-decode a particular stream automatically fall
back without denying software playback.

To inspect the driver independently:

```sh
vainfo --display drm --device /dev/dri/renderD128
```

On Rocky Linux / RHEL-family guests, the libva runtime and an Intel VA-API
driver must be installed separately from the kernel `i915` driver. The
appropriate driver and package availability depend on distribution and GPU
generation. `i915` and `/dev/dri/renderD128` alone do **not** prove that
VA-API encoding works.

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
