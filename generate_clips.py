"""Generate a clarity ladder for every puzzle piece with the FlowEdit service.

Each piece gets 10 clips, from stage_00 (still close to your movement recording,
murky) to stage_09 (the finished target instrument, clear):
    sounds/piece_0/stage_00.wav ... stage_09.wav, sounds/piece_1/..., etc.

Setup (once):
    put your recordings (16-bit WAV, up to 47 s each) in the inputs/ folder
    uv add requests
    export FLOWEDIT_KEY="your key"        # never paste the key into code or chat
Run:
    uv run python generate_clips.py
Re-running skips pieces that are already complete, so you can stop and resume.
"""
import base64, glob, io, os, sys, time, wave
import requests

HUB = os.environ.get("FLOWEDIT_HUB", "https://audioflow.csail.mit.edu")
KEY = os.environ.get("FLOWEDIT_KEY")
if not KEY:
    sys.exit('Set your key first:  export FLOWEDIT_KEY="..."')
H = {"Authorization": f"Bearer {KEY}"}

# ---- settings ----------------------------------------------------------
INPUTS = sorted(glob.glob("inputs/*.wav"))   # every WAV in inputs/, in name order. Empty = first class sample clip
# The 12 pieces are split into blocks, one block per recording. With 4 recordings: pieces 0-2 use the first,
# 3-5 the second, 6-8 the third, 9-11 the fourth (see TARGETS for what each block becomes).
LENGTH = "full"                    # "full": each clip keeps its own length (loops drift against each other)
                                   # "shortest": trim all to the shortest, so every loop stays locked in time
MAX_SECONDS = 47                   # the service limit; lower it (e.g. 12) for quicker renders
SOURCE_PROMPT = None               # None = let the service describe each input
SOURCE_FALLBACK = "Recorded sound of a person moving, footsteps, breath and cloth rustle"
OUT = "sounds"
SEED = 7                           # fixed so reruns are comparable
TARGET_CFG = 5.0                   # edit strength: 3 gentle, 5 usual, 7 a lot
EXTRA = {}                         # any other /generate fields you want to override
# 12 pieces: movement quality x register. (instrument + style, note, Hz, envelope)
TARGETS = [
    ("Bowed cello, slow legato, warm, resonant", "A2", 110, "soft attack, long sustain"),
    ("Bowed cello, slow legato, warm, resonant", "A3", 220, "soft attack, long sustain"),
    ("Bowed violin, slow legato, warm, singing", "A4", 440, "soft attack, long sustain"),
    ("Plucked harp, light quick notes, bright, airy", "A2", 110, "soft attack, short decay"),
    ("Plucked harp, light quick notes, bright, airy", "A3", 220, "soft attack, short decay"),
    ("Plucked harp, light quick notes, bright, airy", "A4", 440, "soft attack, short decay"),
    ("Piano, heavy deliberate strikes, dark", "A1", 55, "hard attack, long decay"),
    ("Piano, heavy deliberate strikes, dark", "A2", 110, "hard attack, long decay"),
    ("Piano, heavy deliberate strikes, dark", "A3", 220, "hard attack, long decay"),
    ("Warm synth pad, flowing continuous line, soft", "A2", 110, "gentle, long sustain"),
    ("Clarinet, flowing continuous line, soft, breathy", "A3", 220, "gentle, long sustain"),
    ("Breathy flute, flowing continuous line, soft", "A4", 440, "gentle, long sustain"),
]
# ------------------------------------------------------------------------

STAGES = 10      # num_intermediates (9) + the final


def target_prompt(t):
    return f"TrackType: Instrument, {t[0]}, calm, A minor, {t[1]}, {t[2]} Hz, {t[3]}, dry"


def trim_wav(b, seconds):
    with wave.open(io.BytesIO(b)) as w:
        params = w.getparams()
        frames = w.readframes(min(w.getnframes(), int(seconds * w.getframerate())))
    out = io.BytesIO()
    with wave.open(out, "wb") as o:
        o.setparams(params)
        o.writeframes(frames)
    return out.getvalue()


def wav_seconds(b):
    with wave.open(io.BytesIO(b)) as w:
        return w.getnframes() / w.getframerate()


def describe(b):
    try:
        r = requests.post(f"{HUB}/describe", headers=H, timeout=120,
                          json={"audio_b64": base64.b64encode(b).decode(), "words": 40})
        r.raise_for_status()
        return r.json()["caption"]
    except Exception as e:
        print(f"  (describe unavailable: {e}); using fallback source prompt")
        return SOURCE_FALLBACK


def load_inputs():
    if INPUTS:
        raw = [open(p, "rb").read() for p in INPUTS]
    else:
        clips = requests.get(f"{HUB}/samples/manifest.json", timeout=30).json()["clips"]
        raw = [requests.get(f"{HUB}/samples/{clips[0]['file']}", timeout=60).content]
        print("using class sample:", clips[0]["title"], clips[0].get("license", ""))
    durations = [wav_seconds(b) for b in raw]
    cap = min(MAX_SECONDS, 47)
    if LENGTH == "shortest":
        cap = min(cap, min(durations))
    print("input lengths (s):", [round(d, 1) for d in durations], "-> using up to", round(cap, 1))
    return [trim_wav(b, cap) for b in raw]


def generate(audio, src, tar):
    body = {"src_prompt": src, "tar_prompt": tar,
            "audio_b64": base64.b64encode(audio).decode(),
            "seconds_total": max(1, min(47, round(wav_seconds(audio)))),
            "lfe_steps": 20, "n_avg": 10, "src_lfe_cfg_scale": 1.0,
            "tar_lfe_cfg_scale": TARGET_CFG, "num_intermediates": STAGES - 1,
            "even_steps": True, "seed": SEED}
    body.update(EXTRA)
    r = requests.post(f"{HUB}/generate", headers=H, json=body, timeout=2700)
    if not r.ok:
        raise RuntimeError(f"{r.status_code}: {r.text[:300]}")
    j = r.json()
    # source-like (high t) first, then toward the target, then the final
    slots = sorted(j["intermediates"], key=lambda s: -float(s["label"].removeprefix("t=")))
    slots.append({"label": "final", "audio_b64": j["final"]["audio_b64"]})
    return [base64.b64decode(s["audio_b64"]) for s in slots]


def main():
    try:
        h = requests.get(f"{HUB}/health", timeout=30).json()
        print(f"{h['active_workers']} workers online, {h['pending']} waiting, typical render {h['typical_render_s']} s")
    except Exception as e:
        print("health check failed:", e)
    inputs = load_inputs()
    captions = {}
    for i, t in enumerate(TARGETS):
        d = os.path.join(OUT, f"piece_{i}")
        if all(os.path.exists(os.path.join(d, f"stage_{k:02d}.wav")) for k in range(STAGES)):
            print(f"piece_{i}: already done")
            continue
        which = i * len(inputs) // len(TARGETS)     # contiguous blocks of pieces per recording
        if which not in captions:
            captions[which] = SOURCE_PROMPT or describe(inputs[which])
            print(f"input {which} described as: {captions[which]}")
        tar = target_prompt(t)
        print(f"piece_{i}: {tar}")
        for attempt in range(3):
            try:
                t0 = time.time()
                clips = generate(inputs[which], captions[which], tar)
                break
            except Exception as e:
                print(f"  attempt {attempt + 1} failed: {e}")
                time.sleep(5)
        else:
            print(f"  giving up on piece_{i}; rerun to retry")
            continue
        os.makedirs(d, exist_ok=True)
        for k, wav in enumerate(clips):
            open(os.path.join(d, f"stage_{k:02d}.wav"), "wb").write(wav)
        print(f"  saved {len(clips)} stages in {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()