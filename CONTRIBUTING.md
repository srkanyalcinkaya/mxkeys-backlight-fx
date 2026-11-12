# Contributing to MX Keys Light

Thanks for wanting to help! New effects, support for other Logitech keyboards, bug reports and
translations are all welcome.

## Quick start

```sh
git clone https://github.com/srkanyalcinkaya/mxkeys-backlight-fx.git
cd mxkeys-backlight-fx
./run.sh --fg        # creates .venv on first run, runs in the foreground
```

Your terminal needs **Input Monitoring** permission to talk to the keyboard
(System Settings → Privacy & Security → Input Monitoring), and **Accessibility** for the
keypress-based effects. Logs: `~/Library/Logs/mxkeys-backlight-fx/mxlight.log`.

To check the keyboard connection on its own:

```sh
.venv/bin/python mxlight.py   # prints device info, blinks 3 times, restores automatic mode
```

## Project layout

| File | What it does |
|---|---|
| `mxlight.py` | HID++ 2.0 protocol: opening the keyboard, reading/writing the backlight |
| `app.py` | Effect engine (`Engine`), window, menu bar icon, HTTP remote control |
| `run.sh` | Start / stop from source |
| `packaging/` | `build.sh` builds the `.app` + release zip, `make_icon.py` draws the icon |

## Adding an effect

Two small edits in `app.py`:

1. Add a row to `EFFECTS`:
   ```python
   ("rain", "🌧️", L("Yağmur", "Rain"), L("Rastgele ışık damlaları", "Random drops of light"), ["period"]),
   ```
   The last item lists which settings rows to show (`hold`, `fade`, `period`, `kps`, `bpm`, `hz`,
   `morse`, `unit`, `minutes`). Add a new setting to `DEFAULTS` and the `Window` form if you need one.
2. Add a branch in `Engine.compute()` that sets `v` to a brightness between **0 and 1**:
   ```python
   elif e == "rain":
       v = ...
   ```
   The engine handles the min/max sliders, the blink overlay and sending levels to the keyboard.

Things to keep in mind when designing an effect:

- The backlight is **one white zone with 7 levels** (effects use 1–7). Smooth curves will look
  slightly stepped.
- Changes between levels 1 and 7 apply almost instantly. **Level 0 takes ~5 s**, which is why
  effects never go below level 1.
- Over Bluetooth a command takes ~50 ms (1–3 s if the keyboard was idle). Anything faster than about
  6 changes per second won't be visible.

## Testing on real hardware

There are no automated hardware tests, so please try your change on a keyboard and say in the PR
what you tested. Useful checks:

- The effect looks right with the window open **and** closed (menu bar only).
- Quitting returns the keyboard to automatic mode (`mode=1` in `mxlight.py` output).
- Nothing breaks if the keyboard is switched off and on while the app runs.

For logic-only changes you can run the app without a keyboard: it shows a preview and keeps retrying
the connection in the background.

## Other Logitech keyboards

The code looks for any Logitech device with HID++ feature `0x1982` (Backlight2). If you have a
different keyboard (MX Keys, MX Keys Mini, Craft…), please open an issue with:

- the model and connection (Bluetooth or Bolt/Unifying receiver),
- the output of `.venv/bin/python mxlight.py`,
- whether you saw the 3 blinks.

Receivers are not supported yet because they use a device index other than `0xFF`. A PR for that
is very welcome.

## HID++ notes

The README has a section on the non-obvious behaviour of the MX Keys S (static effect, level 0
delay, inactivity timeout, exclusive open on macOS). If you find out something new about the
protocol, please add it there. Notes like these are the most useful part of this repo for other
people.

## Translations

UI strings use `L("Türkçe", "English")`, which picks one based on the system language
(override with `MXKEYS_LANG=en` or `tr`). To add a language, extend `L()` in `mxlight.py`;
a small dictionary per language is fine.

## Pull requests

- Keep PRs focused: one effect or one fix per PR.
- Match the existing style: plain Python, no new dependencies unless really needed.
- Update the README if you change behaviour or add an effect.
- Release builds: `packaging/build.sh <version>` (Apple Silicon, ad-hoc signed).

## Reporting bugs

Please include your macOS version, Mac model (Apple Silicon or Intel), keyboard model and
connection, what you expected, what happened, and the last lines of
`~/Library/Logs/mxkeys-backlight-fx/mxlight.log`.

By contributing you agree that your contributions are licensed under the [MIT License](LICENSE).
