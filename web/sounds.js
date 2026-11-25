// @ts-check
// Sound effects for the play page: a short, quiet sound for each thing that
// happens in a game. They are made on the spot with Web Audio, as a few
// tones and a puff of noise, so there are no files to load. The volume is
// set in the settings dialog and kept in the browser; at 0 nothing plays,
// and nothing is set up.
"use strict";

const BitbathSounds = (() => {
  const KEY = "bitbath.volume";
  /** The slider's value for a new visitor, out of 100. */
  const DEFAULT = 50;
  /** The loudest the page gets, as a gain. The effects are meant to sit under whatever else is playing. */
  const LOUDEST = 0.5;

  function stored() {
    try {
      const text = localStorage.getItem(KEY);
      const n = text === null ? NaN : Number(text);
      return n >= 0 && n <= 100 ? n : DEFAULT;
    } catch (e) {
      // Storage can be turned off; the default still works.
      return DEFAULT;
    }
  }

  let current = stored();
  /** @type {AudioContext | null} */
  let ctx = null;
  /** @type {GainNode | null} */
  let master = null;
  /** @type {AudioBuffer | null} */
  let hiss = null;

  /** The volume, from 0 to 100. */
  function volume() {
    return current;
  }

  /** The master gain for a volume. It follows the square, as loudness is heard. @param {number} n */
  function gainFor(n) {
    return (n / 100) ** 2 * LOUDEST;
  }

  /** @param {number} n from 0 to 100 */
  function setVolume(n) {
    current = Math.max(0, Math.min(100, Math.round(n)));
    if (master) master.gain.value = gainFor(current);
    try {
      localStorage.setItem(KEY, String(current));
    } catch (e) {
      // The volume still holds for this visit.
    }
  }

  /** The audio context, made at the first sound. A browser lets it start only after a click or a key. */
  function audio() {
    const AC = typeof window === "undefined" ? null : window.AudioContext || /** @type {any} */ (window).webkitAudioContext;
    if (!AC) return null;
    if (!ctx) {
      ctx = /** @type {AudioContext} */ (new AC());
      master = ctx.createGain();
      master.gain.value = gainFor(current);
      master.connect(ctx.destination);
    }
    if (ctx.state === "suspended") ctx.resume().catch(() => {});
    return ctx;
  }

  /**
   * A tone that starts at once and dies away.
   * @param {number} at when it starts, in the context's seconds @param {number} freq in Hz @param {number} dur in seconds
   * @param {{to?: number, gain?: number, type?: OscillatorType}} [how] `to` slides the pitch there by the end
   */
  function tone(at, freq, dur, how = {}) {
    const a = /** @type {AudioContext} */ (ctx);
    const osc = a.createOscillator();
    const env = a.createGain();
    osc.type = how.type || "sine";
    osc.frequency.setValueAtTime(freq, at);
    if (how.to) osc.frequency.exponentialRampToValueAtTime(how.to, at + dur);
    env.gain.setValueAtTime(0, at);
    env.gain.linearRampToValueAtTime(how.gain || 0.5, at + 0.006);
    env.gain.exponentialRampToValueAtTime(0.0001, at + dur);
    osc.connect(env).connect(/** @type {GainNode} */ (master));
    osc.start(at);
    osc.stop(at + dur + 0.02);
  }

  /**
   * A puff of noise through a low-pass filter: the body of a blow.
   * @param {number} at @param {number} dur @param {number} cutoff the filter's frequency, in Hz @param {number} gain
   */
  function puff(at, dur, cutoff, gain) {
    const a = /** @type {AudioContext} */ (ctx);
    if (!hiss) {
      hiss = a.createBuffer(1, a.sampleRate / 2, a.sampleRate);
      const data = hiss.getChannelData(0);
      for (let i = 0; i < data.length; i++) data[i] = Math.random() * 2 - 1;
    }
    const src = a.createBufferSource();
    src.buffer = hiss;
    const filter = a.createBiquadFilter();
    filter.type = "lowpass";
    filter.frequency.setValueAtTime(cutoff, at);
    filter.frequency.exponentialRampToValueAtTime(Math.max(80, cutoff / 4), at + dur);
    const env = a.createGain();
    env.gain.setValueAtTime(gain, at);
    env.gain.exponentialRampToValueAtTime(0.0001, at + dur);
    src.connect(filter).connect(env).connect(/** @type {GainNode} */ (master));
    src.start(at);
    src.stop(at + dur + 0.02);
  }

  /** The effects, each given when to start. @type {Record<string, (t: number) => void>} */
  const SOUNDS = {
    // A unit steps: a soft knock. It is pitched to carry on small speakers.
    move: (t) => tone(t, 330, 0.09, { to: 190, gain: 0.5 }),
    // A blow lands.
    attack: (t) => {
      puff(t, 0.12, 1800, 0.5);
      tone(t, 150, 0.12, { to: 70, gain: 0.5 });
    },
    // A blow that destroys its target: the same, and a fall after it.
    kill: (t) => {
      SOUNDS.attack(t);
      tone(t + 0.09, 260, 0.28, { to: 80, gain: 0.35, type: "triangle" });
    },
    // The human's units were hit on the opponent's turn: duller than their own blows.
    hit: (t) => {
      puff(t, 0.16, 900, 0.45);
      tone(t, 110, 0.16, { to: 60, gain: 0.45 });
    },
    // Capture progress, and a city or site taken.
    capture: (t) => {
      tone(t, 520, 0.1, { gain: 0.3, type: "triangle" });
      tone(t + 0.08, 660, 0.14, { gain: 0.3, type: "triangle" });
    },
    captured: (t) => {
      [523, 659, 784].forEach((f, i) => tone(t + i * 0.09, f, 0.22, { gain: 0.32, type: "triangle" }));
    },
    // A unit built: a rising pop.
    build: (t) => tone(t, 300, 0.11, { to: 620, gain: 0.35 }),
    // A wagon loaded or unloaded: two soft knocks.
    carry: (t) => {
      tone(t, 260, 0.06, { to: 180, gain: 0.4 });
      tone(t + 0.08, 300, 0.07, { to: 200, gain: 0.4 });
    },
    // An order that was refused.
    error: (t) => tone(t, 140, 0.13, { to: 110, gain: 0.22, type: "square" }),
    // The turn passes to the opponent, and comes back.
    end: (t) => tone(t, 440, 0.16, { to: 330, gain: 0.25 }),
    turn: (t) => {
      tone(t, 660, 0.3, { gain: 0.28 });
      tone(t, 1320, 0.2, { gain: 0.08 });
    },
    // The game ends.
    win: (t) => {
      [523, 659, 784, 1047].forEach((f, i) => tone(t + i * 0.12, f, 0.4, { gain: 0.3, type: "triangle" }));
    },
    lose: (t) => {
      [392, 330, 262].forEach((f, i) => tone(t + i * 0.16, f, 0.45, { gain: 0.3, type: "triangle" }));
    },
    draw: (t) => {
      tone(t, 440, 0.3, { gain: 0.28, type: "triangle" });
      tone(t + 0.16, 440, 0.4, { gain: 0.28, type: "triangle" });
    },
  };

  /**
   * Plays an effect, if the volume isn't 0.
   * @param {string} name one of SOUNDS @param {number} [delay] how long to wait first, in seconds
   */
  function play(name, delay = 0) {
    if (!current || !SOUNDS[name]) return;
    const a = audio();
    if (a) SOUNDS[name](a.currentTime + 0.01 + delay);
  }

  // The volume slider of the settings dialog.

  if (typeof document !== "undefined") {
    const slider = /** @type {HTMLInputElement | null} */ (document.getElementById("volume"));
    const shown = document.getElementById("volume-value");
    if (slider) {
      const show = () => {
        slider.value = String(current);
        if (shown) shown.textContent = current ? current + "%" : "Off";
      };
      slider.oninput = () => {
        setVolume(Number(slider.value));
        show();
      };
      // On letting go, a sample at the new volume.
      slider.onchange = () => play("capture");
      show();
    }
  }

  return { volume, setVolume, play, names: Object.keys(SOUNDS) };
})();

if (typeof module !== "undefined") module.exports = BitbathSounds;
