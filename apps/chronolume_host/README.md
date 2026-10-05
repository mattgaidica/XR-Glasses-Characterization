# chronolume_host

The Chronolume stimulus application. It draws exact 8-bit RGB into a host window (the glasses as an extended display, or a mock window on the laptop) and independently sets/logs hardware brightness, electrochromic film, duty cycle, and display mode over USB. Runs on macOS and Windows.

Project overview: the [repository README](../../README.md). Scientific contract: [`docs/Chronolume_Project_Characterization_Gates.md`](../../docs/Chronolume_Project_Characterization_Gates.md).

## Build

From the repository root. Needs CMake, a C++17 compiler, and (on first configure) network access to fetch GLFW.

macOS:

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
```

Windows (Visual Studio 2022 Build Tools, x64):

```powershell
cmake -S . -B build -G "Visual Studio 17 2022" -A x64
cmake --build build --config Release
```

The VITURE backend is compiled when the platform SDK library exists (`vendor/viture/macos/aarch64/libglasses.dylib` or `vendor/viture/windows/x86_64/glasses.dll`); otherwise the build is mock-only. The library is loaded at runtime from that folder, so nothing needs to be copied next to the executable.

## Run (no glasses)

```bash
./build/apps/chronolume_host/chronolume_host --device mock
```

On Windows the binary is `build\apps\chronolume_host\Release\chronolume_host.exe`.

| Option | Meaning |
|---|---|
| `--device mock\|viture` | Backend (default `mock`). |
| `--monitor N` | Monitor used by `P` / `fullscreen on`. Indices are printed at startup. Default: primary monitor. |
| `--control-port N` | Opt-in automation channel on `127.0.0.1:N` (see below). Keyboard control is unchanged. |

The terminal is the operator console. The window is the stimulus surface (fullscreen it onto the glasses display when hardware is present).

| Key | Action |
|---|---|
| `0`–`6` | Blue ladder: 0, 16, 32, 64, 128, 192, 255 |
| `K` `R` `G` `B` `W` | Black, red, green, blue, white |
| `F1` `F2` `F3` | Left eye, right eye, both (side-by-side halves) |
| `[` `]` | Brightness down / up (mock or SDK) |
| `,` `.` | Duty cycle down / up by 10, clamped to 0–100 (mock or SDK); requested and reported duty print to the console and log as a `duty` event |
| `9` | Duty cycle 98 (the SDK `H` preset) |
| `F` | Toggle electrochromic film |
| `S` | Start/stop session log |
| `D` | Print capability report |
| `V` | Read back framebuffer RGB (each half) |
| `P` | Fullscreen |
| `Esc` | Quit |

Session logs write to `data/sessions/`.

## With glasses

Extend the desktop onto the glasses (**do not mirror**; on Windows use Win+P → Extend). Move the stimulus window to that display, fullscreen it (or start with `--monitor N` and press `P`), then:

```bash
./build/apps/chronolume_host/chronolume_host --device viture
```

The SDK library is loaded only in this mode. If macOS blocks it, clear quarantine on the SDK libs:

```bash
xattr -dr com.apple.quarantine vendor/viture/macos/aarch64
```

On Windows, turn off HDR, Night light, and Auto color management for the glasses display so the panel receives the requested RGB unaltered. Check with `V` (framebuffer readback).

The SDK binaries stay on disk under `vendor/viture/macos/` and `vendor/viture/windows/` but are gitignored. Keep the original SDK zips as backup.

Do not use the SDK demo apps (`vendor/viture/*/demo/`); they are not part of this workflow.

## Control port

`--control-port N` accepts one localhost client and reads newline-terminated commands. Each command is answered with one JSON line **after the next frame has been presented**, including the full logged state (`r g b label eye brightness duty_cycle film display_mode wear calibration backend device`) and a framebuffer readback of both halves. Remote commands are recorded as `control` events in the active session log.

While an image is shown, the readback also carries `image: {w, h, hash, fb_hash, mismatched_pixels, match}`: the whole framebuffer is read back (top row first), compared with the image after eye masking, and both are hashed with FNV-1a 64 over the RGB bytes (`uprtek.frames.frame_hash` gives the same value). `mismatched_pixels` is -1 when the sizes differ.

`wear` is the glasses' wear sensor: `1` worn, `0` not worn, `-1` not reported (mock, or devices without wear detection). It is read at startup and updated from the SDK state callback; every change is printed and logged as a `wear` event. The display can blank when the sensor reports not worn, so check it when a capture reads dark. On a phantom, turn off Wearer Detection with the [VITURE firmware tool](https://www.viture.com/firmware/update) (the display otherwise turns off a minute after the glasses are "removed"); the SDK cannot read or change that setting. The lab Luma Ultra has had it off since 2026-10-03.

| Command | Effect |
|---|---|
| `ping` | Liveness check. |
| `info` | State plus firmware, product id, fullscreen, monitor, log path. |
| `rgb R G B [label]` | Full-field color (0–255). |
| `image <label> <path>` | Shows a binary PPM (P6, maxval 255) pixel for pixel, nearest-neighbour, with no color processing; the path is the rest of the line, so it may contain spaces. The image is stretched only if it is not the framebuffer size, which the readback then reports as a mismatch. Eye masking applies (the unshown half is black). The state's `r g b` read `0 0 0` while an image is shown, and `image` / `image_hash` name it; `rgb` clears it. |
| `eye left\|right\|both` | Side-by-side half selection (`F1`/`F2`/`F3`). |
| `brightness N` | Hardware brightness via the SDK (mock clamps 0–8). The duty cycle is re-read afterwards, so the reply shows whether the brightness change moved it. |
| `duty N` | Display duty cycle, 0–100 (the SDK's presets are 30, 42 and 98 = brightest). The reply and log carry the value the glasses report back. |
| `film on\|off` | Electrochromic film. |
| `calibration <version>` | Calibration tag logged with every stimulus event. |
| `fullscreen on\|off` | Same as `P`, on the `--monitor` display. |
| `log start [path]` / `log stop` | Session log (default path under `data/sessions/`). |
| `mark key=value …` | Writes a `mark` event with the current state (used to tie spectrometer captures to the log). |
| `quit` | Exit. |

Example session from PowerShell:

```powershell
$c = New-Object Net.Sockets.TcpClient('127.0.0.1', 7777); $s = $c.GetStream()
$w = New-Object IO.StreamWriter($s); $r = New-Object IO.StreamReader($s); $w.AutoFlush = $true
$w.WriteLine('rgb 0 0 255 B255'); $r.ReadLine()
```
