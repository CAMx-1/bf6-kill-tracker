# BF6 Kill Tracker

Finds every kill in a Battlefield 6 gameplay recording by watching for the kill skull that pops up under the crosshair, and flags kill streaks (lots of kills in a short time). It works on video files and runs on Windows, macOS and Linux.

```
quick-19-killstreak.mp4
  duration 1:31.90  |  18 kills  |  4 streak(s)  |  processed in 35.7s
  kills:
    0:08.05
    0:13.63  [streak 1]
    ...
    0:52.02 x3  [streak 3]
  STREAK 3: 3 kills in 0.0s  (0:52.02 - 0:52.02)
  most kills in any 10s window: 5 (starting 0:44.33)
```

## Desktop app

![BF6 Kill Tracker app](docs/gui.png)

1. Click **Add videos…** or **Add folder…**, or drag files onto the window.
2. Click **Find streaks**. Each video is scanned in the background, with progress shown in the list.
3. Check the streaks on the right. Click one to see a frame from each kill, or double-click it (or press **Preview streak**) to watch it in your video player. Changing the streak rules (minimum kills, max time between kills) regroups the streaks instantly, with no rescan.
4. Untick any streak you don't want, set the lead-in and tail, then click **Build highlight video…**. Every ticked streak, from every video, is cut out and joined into one MP4, with short fades between clips and no filler.

Clips from recordings with different resolutions or frame rates are scaled to match. You can pick the output resolution and frame rate.

## Install

You need Python 3.9 or newer. ffmpeg is bundled through `imageio-ffmpeg`, so you don't have to install it separately. If an `ffmpeg` is already on your PATH, the tracker uses that one instead.

**Windows:** install Python from python.org, then double-click `bf6killtracker.bat`. The first run sets everything up and then opens the app. From a terminal, the same file runs the command line, e.g. `bf6killtracker.bat analyze "C:\Videos\clip.mp4" --reel highlights.mp4`.

**macOS / Linux:** double-click (or run) `BF6 Kill Tracker.command`, which works the same way. To install manually:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m bf6killtracker            # opens the app
.venv/bin/python -m bf6killtracker analyze ~/Videos/clip.mp4
```

## Usage

```sh
python -m bf6killtracker analyze VIDEO_OR_FOLDER [more ...] [options]
```

| Option | Meaning |
|---|---|
| `--json FILE` / `--csv FILE` | Save results (CSV has one row per kill). |
| `--json-stdout` | Print JSON instead of the text report. |
| `--reel FILE.mp4` | Join every streak from all inputs into one highlight video. |
| `--clips DIR` | Export a separate clip of every streak into DIR. |
| `--clip-every-kill` | With `--clips`, also export each kill. |
| `--pad-before 4 --pad-after 3` | Seconds of video around exported clips. |
| `--streak-gap 5` | Max seconds between kills in the same streak. |
| `--streak-min 3` | Kills needed to count as a streak. |
| `--window 10` | Also report the most kills in any 10-second window. |
| `--sample-fps 30` | Analyse fewer frames per second. |
| `--hud-scale 0.9` | Set this if you changed the in-game HUD scale. |

Folders are scanned for `.mp4 .mkv .mov .avi .webm .m4v .ts` files.

A kill entry like `0:44.33 x2` means one notification added two skulls at once, for example a grenade or mine multi-kill.

### Checking the HUD alignment

If results look wrong on your recordings (a different HUD scale, or a non-16:9 resolution), grab a frame where a skull is visible:

```sh
python -m bf6killtracker calibrate clip.mp4 --at 52.5
```

This writes a PNG with the detection boxes drawn on it. A green box means a skull was detected there. The boxes should sit on the skull icons; if they don't, try `--hud-scale`.

## How it works

1. Only a small strip of each frame around the kill notification is decoded (ffmpeg crops it), and every video is rescaled to 2560×1440 coordinates first, so 1080p, 1440p and 4K all use the same layout.
2. Each of the 5 icon slots is matched against white-skull and red headshot-skull templates. The templates include motion-blurred variants, and matches must pass contrast and colour checks. The yellow order icon and the damage-assist icon are rejected.
3. A new kill is recognised in either of two ways:
   - **pop**: the translucent square that animates in, followed by a skull settling in the rightmost slot;
   - **appear**: a skull settling into an otherwise empty HUD, for when motion blur hides the pop.
4. Skulls that blink, or reappear after smoke, flashes or the scoreboard briefly hid them, are not counted again.
5. The number of skulls added tells multi-kills apart from single kills.
6. Kills are grouped into streaks whenever each one follows the previous within `--streak-gap` seconds.

## Accuracy

On the bundled reference clip (`quick-19-killstreak.mp4`, 2560×1440, variable-frame-rate HEVC), all 18 kills were found with correct multi-kill counts and no false positives. The ground truth was checked by hand against the skulls and the kill feed. The result was the same at 60 fps, at 30 fps, and on a 1080p/30 fps re-encode of the clip.

That is one clip from one player's settings. Different HUD scale, colour-blind modes, or HUD mods may need `--hud-scale` or new templates in `bf6killtracker/templates/`.

## Tests

```sh
python -m pytest -q
```

The unit, highlight-reel and GUI tests (headless Qt) use generated data and run on every OS (see `.github/workflows/tests.yml` for Windows, macOS and Linux CI). `tests/test_sample_video.py` runs end-to-end on the reference clip when it exists at `~/Downloads/quick-19-killstreak.mp4`, or at the path set in `BF6KT_SAMPLE`.
