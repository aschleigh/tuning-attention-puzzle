# Basic Max patch: puzzle OSC monitor + speed mapping
New patcher (Cmd-N)

- `udpreceive 7500` **outlet 1** → `OSC-route /puzzle` **inlet 1**
- `OSC-route /puzzle` **outlet 1** → `OSC-route /grab /drop /stage /amp` **inlet 1**
- `OSC-route /grab /drop /stage /amp` **outlet 1** → `print grab` **inlet 1**
- `OSC-route /grab /drop /stage /amp` **outlet 2** → `print drop` **inlet 1**

Leave outlets 3, 4 and 5 unconnected. 

### The speed mapping

- `OSC-route /puzzle` **outlet 1** → `OSC-route /speed` **inlet 1**
  (a second cable from the same outlet; that's fine)
- `OSC-route /speed` **outlet 1** → `unpack 0 0.` **inlet 1**
- `unpack 0 0.` **outlet 2** → `scale 0. 1. 8000. 400.` **inlet 1**
- `scale 0. 1. 8000. 400.` **outlet 1** → `lores~ 2000 0.7` **inlet 2**

### The test voice

- `saw~ 110` **outlet 1** → `lores~ 2000 0.7` **inlet 1**
- `lores~ 2000 0.7` **outlet 1** → `*~ 0.1` **inlet 1**
- `*~ 0.1` **outlet 1** → `ezdac~` **inlet 1**
- `*~ 0.1` **outlet 1** → `ezdac~` **inlet 2**

Both ezdac~ inlets from the same source, so it plays in both ears.

## Test it

1. Window → Max Console, and keep it where you can see it.
2. Cmd-E to lock.
3. Click the `ezdac~` speaker icon. You should hear a buzzy tone.
4. Click the puzzle window and press **R** to resend all OSC state.
5. Pinch a piece and release it.

Expected in the console: `grab: 3 1` on the pinch, `drop: 3 5 2 1` on the
release. That reads as piece 3, right hand; then dropped at cell (5,2) into a
group of 1.

Expected in the audio: move your hand slowly and the tone is bright; dart it
around and the tone goes dull.