# Hand-tracked puzzle → Max over OSC

A hand-tracked jigsaw built on the `tuning-attention-demos` tracker. The puzzle sends two OSC streams to UDP 7500: the repo tracker's `/cv/hand/...`
hand data, and a `/puzzle/...` namespace carrying the state of the puzzle itself.

Both scripts live in the **repo root**, beside `pyproject.toml`:

```
tuning-attention-demos/
├── puzzle_tracked.py      <- the app
├── generate_clips.py      <- makes the audio, run once
├── inputs/                <- your movement recordings (16-bit WAV)
├── sounds/                <- generated: piece_0/stage_00.wav ... piece_11/stage_09.wav
├── models/                <- from `uv run fetch-models`
└── patches/max/           <- the repo's patches; put yours here too
```

## First run, in order

```bash
cd tuning-attention-demos
uv sync                                  # once
uv run fetch-models                      # once, ~30 MB into models/
uv add python-osc requests               # the puzzle's two extra deps

export FLOWEDIT_KEY="your key"         
uv run python generate_clips.py  

uv run python puzzle_tracked.py
```


In the app: hold both hands open and press **O**, pinch both and press **C**.
That calibrates the pinch to your hands. **R** resends every OSC value — press it
after opening a Max patch, so the patch catches up without you restarting.

**Quit Max first** — one receiver per port — then:

```bash
uv run osc-dump 7500
```

Run the puzzle alongside it and pinch a piece. You should see `/puzzle/grab`
and `/puzzle/drop` scroll past.