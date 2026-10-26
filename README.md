# BF6 Kill Tracker

Finds every kill and death in Battlefield 6 gameplay recordings, flags kill streaks, and turns them into a finished highlight video. It reads two things on screen: the kill skulls that pop up under the crosshair, and the kill feed in the top right. Runs on Windows, macOS and Linux.

```
bike-c4.mp4
  duration 0:21.52  |  4 kills  |  1 death(s), K/D 4.0  |  1 streak(s)  |  processed in 8.3s
  kill types: 3 gun, 1 explosive
  most kills in one life: 4
  timeline:
    0:11.17  (explosive)  [streak 1]
    0:14.22  (gun)  [streak 1]
    0:16.13 x2  (gun, gun)  [streak 1]
    0:19.30  -- died --
```

## Desktop app

![BF6 Kill Tracker app](docs/gui.png)

1. **Add videos.** Use **Add videos…** or **Add folder…**, or drag files onto the window.
   - **Watch folder…** automatically adds and scans new recordings saved to your Battlefield or ShadowPlay folder.
2. **Scan.** Click **Find streaks**. Each video is scanned in the background.
   - The list shows kills, deaths, K/D and streaks for each video.
   - Scans are remembered, so reopening a video is instant.
3. **Check the results.** The streak list shows kill types (headshot, gun, explosive) for each clip.
   - **Show all single kills too** also lists kills outside streaks.
   - Streak rules (minimum kills, time between kills, which kill types count) regroup the list instantly. A death always ends a streak.
4. **Preview and trim.** Click a clip to play it in the preview box: low resolution, no sound, starts instantly.
   - Set each clip's exact start and end with **[ Start here** / **End here ]**, or type the times.
   - **Open in video player** plays the clip at full quality, with sound.
5. **Order the clips.** The **Clip order** tab sets the order of clips in the video: drag them, or use *Best streak last*.
6. **Build.** Click **Build highlight video…**. Options:
   - **Export preset:** YouTube 1440p60 / 1080p60, Vertical 9:16 (TikTok, Shorts, Reels; centred on the crosshair and the kill notification), Discord (under 25 MB), or GIF.
   - **Music:** set its volume, optionally lower it while the game is loud, and optionally cut clips on the beat.
   - **Extras:** kill call-outs (DOUBLE KILL, 4 KILL STREAK), a clip title, and a slow-motion replay of the best kill in each clip.
   - **Encoder:** GPU (NVIDIA, Intel, AMD) on Windows when available, otherwise the CPU.
7. **Save your work.** **Save project…** / **Open project…** keep your videos, ticked clips, trims, order and settings.
8. **See your stats.** **Library & stats** shows every scanned session: best streaks, K/D, kills per hour and headshot rate.

The app checks GitHub for new releases at start-up and shows a button when one is available.

## Install

**Windows, no Python needed:**
1. Download `BF6KillTracker-windows.zip` from the repository's Releases page. Every commit's build is also available from the Actions tab, under *windows-build*.
2. Unzip it.
3. Run `BF6KillTracker.exe`. The same exe also takes the command-line arguments shown below.

**From source (any OS):** you need Python 3.9 or newer; 3.9, 3.12, 3.14 and 3.15 are tested. ffmpeg is bundled through `imageio-ffmpeg`.
- Windows: double-click `bf6killtracker.bat`.
- macOS / Linux: double-click `BF6 Kill Tracker.command`.

Both launchers install everything on first run, and again whenever `requirements.txt` changes. To set it up by hand:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m bf6killtracker            # opens the app
```

## Command line

```sh
python -m bf6killtracker analyze VIDEO_OR_FOLDER [more ...] [options]
```

| Option | Meaning |
|---|---|
| `--json FILE` / `--csv FILE` | Save results. The CSV has one row per kill or death, with types and weapons. |
| `--reel FILE.mp4` | Join every streak from all inputs into one highlight video (`.gif` for a GIF). |
| `--preset NAME` | Export preset, e.g. `"YouTube 1080p60"` or `"Discord (under 25 MB)"`. |
| `--music FILE --beat-sync` | Add background music, optionally cutting on the beat. |
| `--overlays --titles --replay` | Kill call-outs, clip titles, slow-mo replays. |
| `--max-mb N`, `--encoder NAME` | File-size limit; encoder choice. |
| `--clips DIR` | Also export each streak as a separate clip. |
| `--streak-gap 5 --streak-min 3` | What counts as a streak. |
| `--streak-type headshot` | Only count these kill types toward streaks. |
| `--rescan`, `--no-feed` | Ignore remembered scans; skip the kill feed (faster, but no deaths or kill types). |
| `--hud-scale 0.9` | Force a HUD scale. Normally detected automatically. |

`calibrate clip.mp4 --at 52.5` writes a PNG showing where the detector looks.

## How it works

1. **Auto-calibration.** A few keyframes are searched for skulls at several sizes. This sets the HUD scale and position, so other HUD scales, resolutions and ultrawide recordings work without settings.
2. **Decoding.** One ffmpeg pass decodes only two small regions of each frame: the kill notification and the kill feed. Frames are then processed in parallel. GPU decoding is used on Windows, with an automatic fallback to software decoding.
3. **Skulls.** Each icon slot is matched against white and red (headshot) skull templates, with contrast and colour checks. Look-alike icons are rejected: assist, order, revive, and the vehicle-destroyed gear. A new kill is the pop-in animation followed by a skull settling in, or a skull appearing on an empty HUD.
4. **Kill feed.** Rows with your "▸" marker are tracked as they slide in and scroll up.
   - Marker on the killer side: your kill. The weapon icon and the red headshot skull give the kill type.
   - Marker on the victim side: a death.
   - Feed kills the skulls missed (smoke, scoreboard open) are added.
5. **Streaks and lives.** Kills are grouped into streaks (each kill within N seconds of the last, never across a death) and into lives (kills between deaths).

Weapon icons live in `bf6killtracker/templates/weapons/<category>_<name>.png`. Add a cropped icon there to teach it a new weapon. Unknown weapons show as `other`.

## Accuracy

All results below were checked by hand against the skulls and the kill feed.

| Clip | Kills | Deaths | Kill types | Notes |
|---|---|---|---|---|
| `quick-19-killstreak.mp4` (1:32) | 18 | 1 | 7 gun, 6 explosive, 4 headshot, 1 other | |
| `Battlefield 6 - 2026-06-14 …` (0:28) | 7 | 0 | correct | Includes a 4-kill burst and a squad revive, which is not counted. |
| `bike-c4.mp4` (0:22) | 4 | 1 | correct | C4 tank kill, kills over yellow flowers, then a death. |

Re-encodes at 30 fps, at 1080p, at HUD scale 0.85 and at ultrawide 3440×1440 gave the same kills.

Detection still depends on the default kill-feed and HUD colours. Colour-blind modes may need new templates.

## Tests

```sh
python -m pytest -q
```

Unit, reel, GUI (headless Qt), kill-feed, calibration and workflow tests use generated data and run in CI on Windows, macOS and Linux. The end-to-end tests run on the reference clips when they're in `~/Downloads`.
