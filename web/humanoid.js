/* ══════════════════════════════════════════════════════════════════════
   The humanoid — Jarvish's presence, drawn as a field of particles.

   This is a *visualisation*, downstream of state that already exists. It
   reads `document.body.dataset.state` (the agent) and the persistent voice
   state, and owns no state machine of its own. Nothing here can start,
   stop, halt or influence anything the assistant does.

   Three engineering decisions are worth stating, because all three were
   forced:

   1. Raw WebGL2, no library. This project has zero JavaScript dependencies
      by design — no bundler, no npm install, `web/` is served as static
      files. Adding Three.js to draw one point cloud would be the largest
      dependency in the project. `gl.POINTS` with a custom shader is one
      draw call and does the same job.

   2. Its own canvas. A canvas can hold exactly one context type, and all
      three existing canvases already hold `2d` contexts driving live
      features. There is no way to attach WebGL to them without destroying
      something that works.

   3. Transform feedback for the physics. The figure is meant to have
      momentum: disturb it and the particles carry the push, overshoot, and
      settle back. That needs per-particle velocity surviving between
      frames, which a static buffer cannot hold and a JavaScript loop cannot
      afford at sixteen thousand points. WebGL2 can capture shader outputs
      straight back into a buffer *during* the same rasterising draw, so the
      integrator runs on the GPU beside the rendering: still one draw call,
      still no per-particle work on the CPU. Two buffer sets ping-pong,
      because a buffer cannot be read and written in the same pass.

   The split that keeps this stable is worth naming. Each particle has an
   *attractor* — where it wants to be — which is computed fresh every frame
   from procedural motion (breathing, drift, energy routes, assembly) and
   never accumulates. Physics runs only on the *offset* from that attractor,
   under a spring that always pulls it to zero. Forces can therefore be
   thrown at the body as hard as an effect wants: it deforms, carries the
   momentum, and reconstructs itself, because the only fixed point of the
   system is the silhouette.

   Per frame the CPU updates a handful of uniforms and issues one draw call.
   It never walks the particle array. That is what keeps this affordable on
   a machine already spending its memory on an 8B model.
   ══════════════════════════════════════════════════════════════════════ */

(function () {
  "use strict";

  const TAU = Math.PI * 2;

  /* Quality tiers. Chosen from what the device reports rather than assumed:
     an integrated laptop GPU running a local LLM has better things to do than
     shade twenty thousand points. */
  function pickCount() {
    const cores = navigator.hardwareConcurrency || 4;
    const memory = navigator.deviceMemory || 4;
    const small = Math.min(screen.width, screen.height) < 800;
    if (small || cores <= 4 || memory <= 4) return 4500;      // LOW
    if (cores <= 8 || memory <= 8) return 9000;               // MEDIUM
    return 16000;                                             // HIGH
  }

  /* ── the layers ───────────────────────────────────────────────────────
     The body is not one cloud. Six populations share the same silhouette
     and behave differently: they have their own spring stiffness, their own
     drift, their own colour and their own reason to exist.

     Four of them are not tagged by hand — they are derived from where a
     particle actually landed inside its volume, so "surface" means surface
     and "inner" means inner. That is what gives the figure depth instead of
     a flat scatter, and it costs nothing: the envelopes, counts and
     silhouette are exactly what they were. */
  const STRUCT = 0;   // holds the silhouette; stiffest, quietest
  const SURFACE = 1;  // the outer volume
  const RIM = 2;      // the boundary shell, where the rim light lives
  const INNER = 3;    // beneath the surface, seen through the cloud
  const FLOW = 4;     // energy in transit through the body
  const GHOST = 5;    // faint displaced matter around the figure
  const ORBIT = 6;    // the shell the field holds around it
  const LAYERED = -1; // sentinel: derive the layer from radial position

  /* ── the body ──────────────────────────────────────────────────────────
     A deterministic point cloud, built once. Anatomy is assembled from a few
     primitives rather than a mesh: there is no model to download, and the
     silhouette is what carries the recognition, not surface detail.

     Units: metres-ish, y up, origin between the feet. A standing figure is
     about 1.8 tall, which the camera framing below assumes. */

  // A tiny deterministic PRNG. The body must be identical on every load —
  // a humanoid that reshuffles itself on refresh reads as noise, not as a
  // presence that was always there.
  function rng(seed) {
    let s = seed >>> 0;
    return function () {
      s = (s * 1664525 + 1013904223) >>> 0;
      return s / 4294967296;
    };
  }

  function buildBody(count) {
    const target = new Float32Array(count * 3);
    const scatter = new Float32Array(count * 3);
    const seed = new Float32Array(count);
    const group = new Float32Array(count);
    const random = rng(20260902);

    let i = 0;
    function put(x, y, z, g) {
      if (i >= count) return;
      target[i * 3] = x;
      target[i * 3 + 1] = y;
      target[i * 3 + 2] = z;
      seed[i] = random();
      group[i] = g;
      i += 1;
    }

    // `r` is how far out in its own volume the particle landed, 0 at the
    // centre and 1 at the shell. Reading the layer off it means the inner
    // layer is genuinely inside the body and the rim layer is genuinely on
    // its boundary, rather than a tag sprinkled at random.
    function layerAt(r) {
      if (r < 0.55) return INNER;
      if (r < 0.80) return STRUCT;
      if (r < 0.93) return SURFACE;
      return RIM;
    }

    function blob(cx, cy, cz, rx, ry, rz, n, g, shellBias) {
      for (let k = 0; k < n && i < count; k++) {
        const u = random() * 2 - 1;
        const theta = random() * TAU;
        const r = Math.pow(random(), shellBias === undefined ? 0.55 : shellBias);
        const s = Math.sqrt(1 - u * u);
        put(cx + rx * r * s * Math.cos(theta),
            cy + ry * r * u,
            cz + rz * r * s * Math.sin(theta),
            g === LAYERED ? layerAt(r) : g);
      }
    }

    function limb(x1, y1, z1, x2, y2, z2, r1, r2, n, g) {
      for (let k = 0; k < n && i < count; k++) {
        const t = random();
        const shell = Math.sqrt(random());
        const r = (r1 + (r2 - r1) * t) * shell;
        const a = random() * TAU;
        put(x1 + (x2 - x1) * t + r * Math.cos(a),
            y1 + (y2 - y1) * t,
            z1 + (z2 - z1) * t + r * Math.sin(a),
            g === LAYERED ? layerAt(shell) : g);
      }
    }

    const N = count;

    // A bust, cut just below the chest. Removing the legs frees roughly a
    // fifth of the budget, and all of it goes where recognition actually
    // lives: the skull, the shoulder line and the chest. A half figure at
    // this framing reads far better than a whole one shrunk to fit.
    // The head is deliberately featureless — no eyes, no brow, no jaw line.
    // An earlier version placed eye wells and a jaw contour here on the theory
    // that a little structure would read as intentional. It read as a face
    // instead, which is worse: a suggested face invites you to look at it, and
    // an abstract presence is the thing this is meant to be. The budget those
    // features used has gone back into the skull, so the head is smooth and
    // dense rather than detailed.
    //
    // Every envelope, count and bias below is unchanged. Only the layer
    // argument is new, and it partitions what was already there.
    blob(0, 1.63, 0, 0.122, 0.152, 0.126, Math.round(N * 0.190), LAYERED, 0.42); // skull
    blob(0, 1.63, 0, 0.128, 0.158, 0.132, Math.round(N * 0.070), RIM, 3.0);      // skull rim

    limb(0, 1.52, 0, 0, 1.43, 0, 0.050, 0.066, Math.round(N * 0.035), LAYERED);  // neck

    // Shoulder line — the widest thing in frame, so it carries the pose.
    blob(0, 1.372, 0, 0.238, 0.058, 0.090, Math.round(N * 0.105), LAYERED, 0.35);
    blob(0, 1.24, 0, 0.186, 0.140, 0.088, Math.round(N * 0.150), LAYERED, 0.48); // chest
    blob(0, 1.10, 0, 0.150, 0.070, 0.076, Math.round(N * 0.070), LAYERED, 0.50); // ribs

    // Upper arms only. They end at the cut and dissolve with everything else.
    for (const side of [-1, 1]) {
      limb(side * 0.212, 1.360, 0, side * 0.268, 1.075, 0.012,
           0.058, 0.046, Math.round(N * 0.055), LAYERED);
      limb(side * 0.268, 1.075, 0.012, side * 0.286, 0.965, 0.020,
           0.046, 0.040, Math.round(N * 0.022), LAYERED);
    }

    // The cut. Instead of a flat edge, a shelf of particles that thins as it
    // descends — the body reads as still materialising rather than sawn off.
    for (let k = 0; k < Math.round(N * 0.075) && i < count; k++) {
      const fade = Math.pow(random(), 1.9);          // dense at the top
      const y = 1.06 - fade * 0.34;
      const spread = 0.16 + fade * 0.20;
      const theta = random() * TAU;
      const r = spread * Math.sqrt(random());
      put(r * Math.cos(theta), y, r * Math.sin(theta) * 0.62,
          fade > 0.55 ? RIM : SURFACE);
    }

    // What remains funds the layers that are not anatomy: energy in transit,
    // the faint displaced shell, and the orbital field.
    while (i < count) {
      const roll = random();
      if (roll < 0.26) {
        // Sub-surface energy, seeded through the torso and skull.
        blob(0, 1.32, 0, 0.17, 0.30, 0.11, 1, INNER, 0.9);
      } else if (roll < 0.40) {
        // Energy in transit. Its resting place is the spine; the shader
        // routes it, so this only has to be somewhere sensible.
        put((random() - 0.5) * 0.08, 1.05 + random() * 0.60,
            (random() - 0.5) * 0.06, FLOW);
      } else if (roll < 0.56) {
        // The ghost layer: displaced matter just outside the silhouette.
        const u = random() * 2 - 1, theta = random() * TAU;
        const r = 1.18 + random() * 0.30;
        const sn = Math.sqrt(1 - u * u);
        put(r * 0.22 * sn * Math.cos(theta),
            1.30 + r * u * 0.30,
            r * 0.20 * sn * Math.sin(theta), GHOST);
      } else if (roll < 0.68) {
        // A base ring at the cut: the plinth a hologram stands on.
        const theta = random() * TAU;
        const r = 0.34 + random() * 0.16;
        put(r * Math.cos(theta), 0.90 + random() * 0.05,
            r * Math.sin(theta) * 0.55, ORBIT);
      } else {
        const u = random() * 2 - 1, theta = random() * TAU;
        const r = 0.62 + random() * 0.52;
        const sn = Math.sqrt(1 - u * u);
        put(r * sn * Math.cos(theta), 1.34 + r * u * 0.52,
            r * sn * Math.sin(theta), ORBIT);
      }
    }

    for (let k = 0; k < count; k++) {
      const u = random() * 2 - 1, theta = random() * TAU;
      const r = 1.9 + random() * 2.6;
      const s = Math.sqrt(1 - u * u);
      scatter[k * 3] = r * s * Math.cos(theta);
      scatter[k * 3 + 1] = 1.30 + r * u * 0.70;
      scatter[k * 3 + 2] = r * s * Math.sin(theta);
    }

    return { target, scatter, seed, group };
  }

  /* ── shaders ───────────────────────────────────────────────────────────
     Everything animated happens here. The CPU sends time, a few state
     scalars, the cursor and at most one event; the GPU does the rest for
     every particle, and hands its own velocity back for the next frame. */

  const VERTEX = `#version 300 es
  precision highp float;

  in vec3  aTarget;
  in vec3  aScatter;
  in float aSeed;
  in float aGroup;
  in vec3  aOffset;      // displacement from the attractor, carried forward
  in vec3  aVel;         // velocity, carried forward
  in float aCharge;      // how recently this particle was excited

  uniform float uTime;
  uniform float uDt;
  uniform float uAssembly;    // 0 dispersed .. 1 fully formed
  uniform float uListening;
  uniform float uProcessing;
  uniform float uPlanning;
  uniform float uExecuting;
  uniform float uSpeaking;
  uniform float uConfirming;
  uniform float uError;
  uniform float uDissolve;
  uniform float uEnergy;      // live audio level, 0..1
  uniform float uCohesion;    // how tightly the field is holding the body
  uniform float uShock;       // a blow, decaying: 1 at the instant, 0 after
  uniform vec2  uCursor;      // normalised device coords
  uniform vec2  uCursorVel;   // how fast, and which way, it is moving
  uniform float uCursorOn;
  uniform vec3  uRipple;      // xy origin, z age in seconds (<0 = none)
  uniform vec4  uEvent;       // kind, age, seed, strength
  uniform float uAspect;
  uniform float uPixel;
  uniform float uStage;       // stage height against its full height
  uniform float uCalm;        // 1 when the viewer asked for reduced motion

  out vec3  vOffsetOut;       // captured straight back into the buffer
  out vec3  vVelOut;
  out float vChargeOut;

  out float vGlow;
  out float vGroup;
  out float vFade;
  out float vRim;
  out float vNear;
  out vec2  vStreak;

  const float TAU = 6.2831853;

  // The anatomy the energy system routes between. These are the branch
  // points of the body, not anatomical landmarks — the places where a
  // travelling pulse has somewhere to go next.
  const vec3 CORE  = vec3( 0.000, 1.245, 0.0);
  const vec3 NECK  = vec3( 0.000, 1.470, 0.0);
  const vec3 SKULL = vec3( 0.000, 1.630, 0.0);
  const vec3 SHR   = vec3( 0.212, 1.360, 0.0);
  const vec3 SHL   = vec3(-0.212, 1.360, 0.0);

  float hash11(float n) { return fract(sin(n) * 43758.5453123); }
  // A divergence-free-ish drift field. Three sines at frequencies that do not
  // divide each other, seeded per particle and multiplied in pairs, so no two
  // particles share a phase and the field never returns to a previous state.
  //
  // This replaced a value-noise lattice that cost thirty-two transcendentals
  // per particle. On the reference machine that alone was the difference
  // between 43fps and the frame budget this is supposed to fit in, and at
  // these amplitudes nothing about the motion reads differently.
  vec3 drift(vec3 p, float t, float s) {
    float a = sin(p.y * 3.1 + t * 0.31 + s * 19.0);
    float b = sin(p.x * 2.7 - t * 0.24 + s * 11.0);
    float c = sin(p.z * 3.5 + t * 0.19 + s * 27.0);
    return vec3(b * c, c * a, a * b);
  }

  mat2 rot(float a) { float c = cos(a), s = sin(a); return mat2(c, -s, s, c); }

  void main() {
    float t = uTime;
    float s = aSeed;
    float g = aGroup;
    float motion = mix(1.0, 0.25, uCalm);
    float busy = max(uProcessing, max(uPlanning, uExecuting));

    // ── ASSEMBLY. Each particle arrives on its own beat, so the body
    // gathers rather than snapping into place all at once.
    float delay = s * 0.55;
    float a = clamp((uAssembly - delay) / (1.0 - delay + 0.0001), 0.0, 1.0);
    a = a * a * (3.0 - 2.0 * a);

    // ── THE ATTRACTOR. Where this particle wants to be, recomputed from
    // scratch every frame. Nothing here accumulates, which is what lets the
    // physics below be violent without the body ever drifting away.
    vec3 home = mix(aScatter, aTarget, a);

    // Breathing: the whole figure swells very slightly. Subtle on purpose —
    // this is what stops it reading as a still image.
    float breath = sin(t * 0.55 + s * 0.4) * 0.006 * motion;
    home.xz *= 1.0 + breath;
    home.y  += breath * 0.35;

    // Drift. Every particle is on its own phase, so the body never visibly
    // repeats and no two particles move together.
    vec3 nf = drift(home, t, s) * 0.5;
    float driftAmt = (g > 5.5) ? 0.055
                   : (g > 4.5) ? 0.048
                   : (g > 3.5) ? 0.020
                   : (g > 2.5) ? 0.016 : 0.009;
    home += nf * driftAmt * motion * (0.65 + busy * 0.85);

    // The orbital shell turns, wobbles, and does not turn at a constant rate.
    if (g > 5.5) {
      float rate = 0.09 + hash11(s * 3.3) * 0.11;
      float ang = t * rate * motion + s * TAU + sin(t * 0.17 + s * 9.0) * 0.35;
      home.xz = rot(ang) * home.xz;
      home.y += sin(t * 0.31 + s * TAU) * 0.045 * motion;
    }

    // ── INTERNAL ENERGY FLOW. The transit layer does not sit in the body,
    // it travels through it: core to skull, core out along each shoulder,
    // and one route running back down. Each particle re-rolls its route
    // every cycle, so streams appear, run, fade and reappear elsewhere
    // instead of looping on a fixed track.
    float flowFade = 1.0;
    if (g > 3.5 && g < 4.5) {
      float period = 2.2 + hash11(s * 5.1) * 2.8;
      float phase = t / period + s * 3.0;
      float cycle = floor(phase);
      float u = fract(phase);
      float lane = floor(hash11(cycle * 7.3 + s * 41.0) * 4.0);
      float jitter = hash11(cycle * 31.7 + s * 13.0);

      vec3 from = CORE;
      vec3 to = SKULL;
      if (lane > 2.5)      { from = SKULL; to = CORE + vec3(0.0, -0.18, 0.0); }
      else if (lane > 1.5) { to = SHL + vec3(-0.070, -0.30, 0.02); }
      else if (lane > 0.5) { to = SHR + vec3( 0.070, -0.30, 0.02); }

      // Ease, so the pulse accelerates away and decelerates on arrival.
      float e = u * u * (3.0 - 2.0 * u);
      // Bow the path. A straight line between two points reads as a wire.
      vec3 bend = vec3(sin(u * 3.14159) * (jitter - 0.5) * 0.17,
                       sin(u * 6.28318) * 0.02,
                       cos(u * 3.14159 + jitter * 6.0) * 0.055);
      home = mix(aScatter, mix(from, to, e) + bend, a);
      flowFade = smoothstep(0.0, 0.14, u) * smoothstep(1.0, 0.80, u);
    }

    // The cut. Particles below the chest thin out and drift upward, so the
    // bust reads as a figure still condensing out of the field rather than
    // one that has been sliced off at the waist.
    float below = smoothstep(1.06, 0.72, aTarget.y);
    home.y += below * (0.03 + sin(t * 0.8 + s * 24.0) * 0.02) * motion;

    // ── THE NERVOUS SYSTEM. Distance from the core, used as the length of
    // the path a signal has to travel. Wavefronts sweep outward through it,
    // so the head lights after the chest rather than with it — the delay is
    // the whole effect. Branch points brighten as a front passes them.
    float gd = distance(aTarget, CORE);
    float wave1 = smoothstep(0.115, 0.0, abs(gd - fract(t * 0.19) * 1.05));
    float wave2 = smoothstep(0.080, 0.0, abs(gd - fract(t * 0.11 + 0.41) * 1.05));
    float nd = min(min(distance(aTarget, NECK), distance(aTarget, SHR)),
                   min(distance(aTarget, SHL), distance(aTarget, SKULL)));
    float node = smoothstep(0.085, 0.0, nd);
    float nerve = wave1 * 0.75 + wave2 * 0.45;
    nerve += node * (0.22 + nerve * 1.3);
    // Extremely subtle at rest; the network only comes up when there is
    // something to compute.
    nerve *= 0.14 + busy * 1.05 + uSpeaking * 0.45 + uConfirming * 0.30;

    // ── ENERGY EVENTS. One at a time, rare, and each one propagates from
    // the core outward at a finite speed rather than lighting the body at
    // once. The front is where the wave has reached; gd is how far this
    // particle is from the source.
    float ev = 0.0;
    vec3 evForce = vec3(0.0);
    if (uEvent.x > 0.5) {
      float age = uEvent.y;
      float kind = uEvent.x;
      float front = age * 1.30;
      float band = smoothstep(0.17, 0.0, abs(gd - front));
      float life = exp(-age * 0.85);
      vec3 away = normalize(home - CORE + 0.0001);

      if (kind < 1.5) {                    // surge: a band climbing the body
        ev = smoothstep(0.11, 0.0, abs(aTarget.y - (0.92 + age * 0.62))) * life * 1.7;
      } else if (kind < 2.5) {             // neural burst: the branches fire
        ev = node * exp(-age * 1.7) * 2.4 + band * life * 0.55;
      } else if (kind < 3.5) {             // field wave: outward from the chest
        ev = band * life * 1.5;
        evForce = away * band * life * 1.20;
      } else if (kind < 4.5) {             // cascade: the shoulders let go
        float sh = smoothstep(0.32, 0.0,
                              min(distance(aTarget, SHR), distance(aTarget, SHL)));
        ev = sh * life * 1.15;
        evForce = vec3(0.0, -1.0, 0.10) * sh * exp(-age * 2.2) * 1.8;
      } else if (kind < 5.5) {             // core ignition
        ev = band * life * 2.0 + smoothstep(0.30, 0.0, gd) * exp(-age * 1.05) * 1.5;
        evForce = away * band * life * 0.45;
      } else {                             // reassembly: pull hard, then hold
        ev = band * life * 0.75;
      }
      ev *= uEvent.w;
      evForce *= uEvent.w;
    }

    // ══ PHYSICS ══════════════════════════════════════════════════════════
    // Only the offset from the attractor is integrated. The spring's fixed
    // point is the silhouette, so however hard the body is hit it always has
    // somewhere to reconstruct itself to.
    vec3 off = aOffset;
    vec3 vel = aVel;
    float dt = uDt;

    float stiffBase = (g > 5.5) ?  5.0     // orbital: loosely held
                    : (g > 4.5) ?  3.0     // ghost: barely held at all
                    : (g > 3.5) ?  6.5     // in transit
                    : (g > 2.5) ?  9.0     // inner
                    : (g > 1.5) ? 16.0     // rim
                    : (g > 0.5) ? 14.0     // surface
                    :             18.0;    // structural
    float stiff = stiffBase * (0.40 + uCohesion * 1.00);
    float damp = 3.2 + uCohesion * 2.2 + uConfirming * 5.0;

    vec3 force = -off * stiff - vel * damp;
    force += evForce;

    // Where this particle's attractor lands on screen. Needed before the
    // cursor can be asked how far away it is.
    vec3 view = home - vec3(0.0, 1.34, 0.0);
    float spin = 0.13 * sin(t * 0.16) * motion;
    mat2 R = rot(spin);
    view.xz = R * view.xz;
    float depth = 2.25 + view.z;
    vec2 screenHome = vec2(view.x / uAspect, view.y) / (depth * 0.19);

    // ── THE CURSOR AS A BODY IN THE FIELD. Four things decide the push:
    // how near it is, how fast it is moving, which way it is moving, and
    // what the particle is already doing. Momentum and the return are not
    // written here at all — they fall out of the integrator, which is the
    // point of doing this with real physics instead of an offset.
    if (uCursorOn > 0.5) {
      vec2 d2 = screenHome - uCursor;
      float dist = length(d2);
      float fall = exp(-dist * dist * 20.0);
      vec2 radial = normalize(d2 + 0.0001);
      vec2 dir2 = normalize(radial * 0.60 + uCursorVel * 2.4 + 0.0001);
      float strength = fall * (1.5 + length(uCursorVel) * 7.0);
      // Back out of screen space: undo the aspect, the perspective divide
      // and the camera spin, so the push is a force on the body and not a
      // slide of the picture.
      vec3 push = vec3(dir2.x * uAspect, dir2.y, 0.0) * depth * 0.19;
      push.xz = rot(-spin) * push.xz;
      force += push * strength;
    }

    // A click sends one expanding ring through the field.
    if (uRipple.z >= 0.0) {
      float radius = uRipple.z * 1.5;
      float ring = smoothstep(0.20, 0.0,
                              abs(length(screenHome - uRipple.xy) - radius));
      vec2 outward2 = normalize(screenHome - uRipple.xy + 0.0001);
      vec3 push = vec3(outward2.x * uAspect, outward2.y, 0.0) * depth * 0.19;
      push.xz = rot(-spin) * push.xz;
      force += push * ring * exp(-uRipple.z * 2.0) * 4.5;
    }

    // ── STATE AS PHYSICS. Each state changes what the particles *do*, not
    // just what colour they are.
    vec3 turb = vec3(hash11(s * 3.1 + floor(t * 3.0)) - 0.5,
                     hash11(s * 7.7 + floor(t * 3.0)) - 0.5,
                     hash11(s * 5.3 + floor(t * 3.0)) - 0.5);
    // ERROR turbulence; ANALYSING agitation; PLANNING clustering.
    force += turb * (uError * 1.6 + uShock * 11.0
                     + uProcessing * 2.2 + uPlanning * 2.0) * motion;

    // PLANNING gathers particles into temporary structures that dissolve:
    // a slowly moving set of attracting points, so shapes form and go.
    if (uPlanning > 0.01) {
      float cell = floor(t * 0.5) + floor(s * 6.0);
      vec3 knot = vec3(hash11(cell * 2.7) - 0.5,
                       1.05 + hash11(cell * 5.9) * 0.62,
                       hash11(cell * 9.1) - 0.5) * vec3(0.42, 1.0, 0.30);
      force += (knot - home) * uPlanning * 1.0;
    }

    // LISTENING draws the field inward and gathers it at the upper body, and
    // the loose outer layers close in — the whole field leaning in to hear.
    vec3 attend = vec3(0.0, 1.38, 0.0);
    force += -off * uListening * 9.0;
    force += normalize(attend - home + 0.0001) * uListening * 0.8 * smoothstep(0.0, 0.9, gd);
    if (g > 4.5) {
      force += normalize(attend - home + 0.0001) * uListening * 1.5;
    }

    // EXECUTING propagates outward directionally, in pulses, along the body.
    force += normalize(home - CORE + 0.0001)
             * uExecuting * sin(t * 2.4 - gd * 5.5) * 1.1;

    // SPEAKING pushes from the throat and chest with the voice.
    float voice = smoothstep(1.10, 1.70, aTarget.y);
    force += normalize(home - vec3(0.0, 1.25, 0.0) + 0.0001)
             * uSpeaking * voice * (0.5 + 0.5 * sin(t * 9.0 - aTarget.y * 6.0))
             * mix(0.55, 1.0, uEnergy) * 1.3;

    // DISSOLUTION: cohesion is already gone by the time this matters, so a
    // gentle lift is all it takes for the body to return to the field.
    force += vec3(nf.x * 0.35, 1.0, nf.z * 0.35) * uDissolve * 0.8;

    // Semi-implicit Euler, damped, with a hard cap. The cap is what makes
    // the body deform instead of exploding when something hits it hard.
    vel += force * dt;
    vel *= exp(-dt * 0.55);
    off += vel * dt;
    float mag = length(off);
    if (mag > 0.40) { off *= 0.40 / mag; vel *= 0.55; }

    // ── VISUAL MEMORY. Excitement decays instead of resetting, so a
    // particle a pulse has just passed through keeps a little heat and
    // settles rather than snapping back to its resting brightness.
    float excite = ev + nerve * 0.55 + length(vel) * 0.9;
    float charge = max(aCharge * exp(-dt * 1.25), clamp(excite, 0.0, 1.6));

    vOffsetOut = off;
    vVelOut = vel;
    vChargeOut = charge;

    // ══ APPEARANCE ═══════════════════════════════════════════════════════
    vec3 pos = home + off;

    vec3 view2 = pos - vec3(0.0, 1.34, 0.0);
    view2.xz = R * view2.xz;
    float depth2 = max(0.35, 2.25 + view2.z);
    vec2 screen = vec2(view2.x / uAspect, view2.y) / (depth2 * 0.19);
    gl_Position = vec4(screen, 0.0, 1.0);

    // -- RIM. Not an outline: a real orientation test. The outward vector is
    // direction this particle faces away from the body's axis; once the
    // camera has turned it, particles whose outward direction is
    // perpendicular to the view are the ones on the silhouette. So the rim
    // travels around the figure as the camera drifts, and a slow band in y
    // decides whether the shoulder, the chest or the head is carrying it.
    float radial = length(aTarget.xz);
    vec2 ox = R * normalize(aTarget.xz + vec2(0.0001));
    float rim = pow(clamp(1.0 - abs(ox.y), 0.0, 1.0), 2.2);
    rim *= smoothstep(0.02, 0.11, radial);
    rim *= 0.42 + 0.58 * (0.5 + 0.5 * sin(t * 0.21 + aTarget.y * 4.2));
    rim *= (g > 1.5 && g < 2.5) ? 1.15 : 0.45;    // the rim layer carries it
    rim *= 0.75 + busy * 0.5 + uConfirming * 0.4;

    // The cursor lights the rim it is nearest, which is what makes the
    // figure feel like it has a surface being touched.
    if (uCursorOn > 0.5) {
      rim += exp(-dot(screen - uCursor, screen - uCursor) * 14.0) * 0.55;
    }

    // ── DEPTH. Nearer particles are brighter, larger and less fogged. This
    // is the whole difference between a point cloud and a volume.
    float near = clamp((2.55 - depth2) / 0.85, 0.0, 1.0);
    float fog = exp(-max(0.0, depth2 - 2.15) * 0.34);

    float glow = 0.44 + hash11(s * 12.7) * 0.17;
    glow += nerve * 0.60;
    glow += ev;
    glow += charge * 0.32;
    glow += rim * 0.55;

    // Sub-surface: the inner layer surfaces and submerges on its own slow
    // clock, so energy reads as being *inside* the body rather than painted
    // brighter than it.
    if (g > 2.5 && g < 3.5) {
      glow *= 0.78 + (0.5 + 0.5 * sin(t * 0.7 + s * TAU)) * 0.62;
    }

    glow += uListening * (0.12 + smoothstep(1.44, 1.78, aTarget.y)
            * (0.5 + 0.5 * sin(t * 3.0 - aTarget.y * 9.0)) * 0.50 + uEnergy * 0.45);
    glow += uSpeaking * voice * (0.5 + 0.5 * sin(t * 9.0 - aTarget.y * 6.0))
            * mix(0.55, 1.0, uEnergy) * 0.80;
    glow += uConfirming * (node * 0.55 + 0.08);
    glow += uError * hash11(s + floor(t * 6.0)) * 0.35;

    // A scan sweep climbing the body every few seconds. One clean band, not a
    // strobe — this is the single most "assembled by a machine" cue there is.
    float sweep = fract(t * 0.14);
    glow += smoothstep(0.05, 0.0, abs(aTarget.y - (0.75 + sweep * 1.15)))
            * 0.5 * mix(1.0, 0.3, uCalm);

    glow *= 0.88 + near * 0.30;

    // ── SIZE and STREAK. Fast particles are drawn slightly larger and the
    // fragment shader stretches them along their own velocity, so quick
    // energy leaves a short soft tail. Only the fast ones — this is a few
    // particles at a time, not a trail behind the cursor.
    float sizeBase = (g > 5.5) ? 1.5
                   : (g > 4.5) ? 1.4
                   : (g > 3.5) ? 2.7
                   : (g > 2.5) ? 1.9
                   : (g > 1.5) ? 2.2
                   : (g > 0.5) ? 2.1 : 2.0;
    float speed = length(vel);
    float fast = (g > 3.5 && g < 4.5) ? 1.0 : 0.0;
    float stretch = fast * clamp((speed - 0.12) * 0.60, 0.0, 0.75);
    gl_PointSize = uPixel * sizeBase * (3.5 / depth2) * (0.55 + a * 0.65)
                   * uStage * (1.0 + stretch * 0.9);

    vec3 vv = vel;
    vv.xz = R * vv.xz;
    vec2 vs = vec2(vv.x / uAspect, vv.y);
    vStreak = (length(vs) > 0.0001) ? normalize(vs) * stretch : vec2(0.0);

    float dissolve = (1.0 - below * 0.85) * flowFade * (1.0 - uDissolve * 0.40);
    vGlow = glow * (0.25 + a * 0.75) * fog;
    vGroup = g;
    vFade = dissolve;
    vRim = rim;
    vNear = near;
  }`;

  const FRAGMENT = `#version 300 es
  precision highp float;

  in float vGlow;
  in float vGroup;
  in float vFade;
  in float vRim;
  in float vNear;
  in vec2  vStreak;
  uniform float uError;
  out vec4 outColour;

  void main() {
    vec2 d = gl_PointCoord - 0.5;

    // A moving particle is drawn as a short streak rather than a disc, by
    // squashing the sprite's coordinates along its own direction of travel.
    // It costs nothing and it is the only way a point sprite can show speed.
    float sl = length(vStreak);
    if (sl > 0.001) {
      vec2 dir = vStreak / sl;
      d = vec2(dot(d, dir) / (1.0 + sl * 1.5), dot(d, vec2(-dir.y, dir.x)));
    }

    // Round, soft-edged point. Discarding the corners is what stops the
    // cloud looking like a grid of squares.
    float r = dot(d, d);
    if (r > 0.25) discard;
    float falloff = smoothstep(0.25, 0.0, r);

    // Cyan is the identity. White is heat. Crimson is only ever an accent,
    // and only when something has actually gone wrong.
    vec3 cyan   = vec3(0.13, 0.83, 0.93);
    vec3 deep   = vec3(0.10, 0.32, 0.78);
    vec3 hot    = vec3(0.85, 0.98, 1.00);
    vec3 accent = vec3(0.96, 0.25, 0.35);

    vec3 base = mix(deep, cyan, clamp(vGlow * 1.5, 0.0, 1.0));
    base = mix(base, hot, clamp((vGlow - 0.72) * 1.8, 0.0, 1.0));

    // Each layer reads differently, which is what stops six populations
    // sharing a silhouette from looking like one cloud.
    float alpha = falloff * clamp(0.26 + vGlow * 0.92, 0.0, 1.0) * vFade;
    if (vGroup > 5.5) {                       // orbital
      alpha *= 0.65;
    } else if (vGroup > 4.5) {                // ghost: barely there at all
      base = mix(base, deep, 0.45);
      alpha *= 0.20;
    } else if (vGroup > 3.5) {                // energy in transit
      base = mix(base, hot, 0.50);
    } else if (vGroup > 2.5) {                // sub-surface
      base = mix(base, deep, 0.26);
      alpha *= 0.92;
    } else if (vGroup > 1.5) {                // rim
      base = mix(base, hot, clamp(vRim, 0.0, 0.40));
    }

    base = mix(base, accent, clamp(uError * 0.78, 0.0, 1.0));

    // A hot centre inside each point. Uniform discs read as dots; a bright
    // core with a soft halo reads as light. Near particles get more of it,
    // which is most of what sells the depth.
    float core = smoothstep(0.055, 0.0, r);
    base += hot * core * (0.35 + vNear * 0.35) * clamp(vGlow, 0.0, 1.2);

    outColour = vec4(base * (0.62 + vGlow), alpha);
  }`;

  /* ── the renderer ──────────────────────────────────────────────────── */

  // The three things the GPU hands back to itself each frame.
  const CARRIED = [
    { attr: "aOffset", out: "vOffsetOut", size: 3 },
    { attr: "aVel", out: "vVelOut", size: 3 },
    { attr: "aCharge", out: "vChargeOut", size: 1 },
  ];

  function compile(gl, type, source) {
    const shader = gl.createShader(type);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      const log = gl.getShaderInfoLog(shader);
      gl.deleteShader(shader);
      throw new Error("shader: " + log);
    }
    return shader;
  }

  function create(canvas) {
    const gl = canvas.getContext("webgl2", {
      alpha: true, antialias: false, depth: false,
      premultipliedAlpha: false, powerPreference: "low-power",
    });
    if (!gl) return null;

    const program = gl.createProgram();
    const vs = compile(gl, gl.VERTEX_SHADER, VERTEX);
    const fs = compile(gl, gl.FRAGMENT_SHADER, FRAGMENT);
    gl.attachShader(program, vs);
    gl.attachShader(program, fs);
    // Must be declared before linking, or the outputs are optimised away.
    gl.transformFeedbackVaryings(program, CARRIED.map((c) => c.out),
                                 gl.SEPARATE_ATTRIBS);
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      throw new Error("link: " + gl.getProgramInfoLog(program));
    }
    gl.deleteShader(vs);
    gl.deleteShader(fs);

    const count = pickCount();
    const body = buildBody(count);
    const buffers = [];

    function make(data, usage) {
      const buffer = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
      gl.bufferData(gl.ARRAY_BUFFER, data, usage);
      buffers.push(buffer);
      return buffer;
    }

    const fixed = [
      { name: "aTarget", buffer: make(body.target, gl.STATIC_DRAW), size: 3 },
      { name: "aScatter", buffer: make(body.scatter, gl.STATIC_DRAW), size: 3 },
      { name: "aSeed", buffer: make(body.seed, gl.STATIC_DRAW), size: 1 },
      { name: "aGroup", buffer: make(body.group, gl.STATIC_DRAW), size: 1 },
    ];

    // Two sets of carried state. The body starts exactly on its attractors
    // with no velocity, so the first frame is the silhouette at rest.
    const sets = [[], []];
    for (const set of sets) {
      for (const carried of CARRIED) {
        set.push(make(new Float32Array(count * carried.size), gl.DYNAMIC_COPY));
      }
    }

    function bind(name, buffer, size) {
      const location = gl.getAttribLocation(program, name);
      if (location < 0) return;
      gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
      gl.enableVertexAttribArray(location);
      gl.vertexAttribPointer(location, size, gl.FLOAT, false, 0, 0);
    }

    // One vertex array per set, so the pass can read one and write the other
    // without a buffer ever being both source and destination.
    const vaos = [];
    const feedbacks = [];
    for (const which of [0, 1]) {
      const vao = gl.createVertexArray();
      gl.bindVertexArray(vao);
      for (const attr of fixed) bind(attr.name, attr.buffer, attr.size);
      for (const index of [0, 1, 2]) {
        bind(CARRIED[index].attr, sets[which][index], CARRIED[index].size);
      }
      gl.bindVertexArray(null);
      vaos.push(vao);

      // Reading set `which` means writing the other one.
      const feedback = gl.createTransformFeedback();
      gl.bindTransformFeedback(gl.TRANSFORM_FEEDBACK, feedback);
      for (const index of [0, 1, 2]) {
        gl.bindBufferBase(gl.TRANSFORM_FEEDBACK_BUFFER, index,
                          sets[1 - which][index]);
      }
      feedbacks.push(feedback);
    }
    gl.bindTransformFeedback(gl.TRANSFORM_FEEDBACK, null);
    gl.bindBuffer(gl.ARRAY_BUFFER, null);

    const uniforms = {};
    for (const name of ["uTime", "uDt", "uAssembly", "uListening", "uProcessing",
                        "uPlanning", "uExecuting", "uSpeaking", "uConfirming",
                        "uError", "uDissolve", "uEnergy", "uCohesion", "uShock",
                        "uCursor", "uCursorVel", "uCursorOn", "uRipple",
                        "uEvent", "uAspect", "uPixel", "uStage", "uCalm"]) {
      uniforms[name] = gl.getUniformLocation(program, name);
    }

    return { gl, program, vaos, feedbacks, buffers, uniforms, count };
  }

  /* ── the public object ─────────────────────────────────────────────── */

  // Rare, procedural, and weighted by what the assistant is doing. Nothing
  // here decides anything: an event is a lighting cue with a start time.
  const EVENTS = { SURGE: 1, NEURAL: 2, WAVE: 3, CASCADE: 4, IGNITION: 5, REBUILD: 6 };

  const humanoid = {
    ready: false,
    canvas: null,
    ctx: null,
    frame: null,
    started: 0,
    assembly: 0,
    targetAssembly: 1,
    energy: 0,
    cohesion: 0.9,
    shock: 0,
    cursor: { x: 0, y: 0, on: 0, vx: 0, vy: 0, lx: 0, ly: 0, moved: 0 },
    ripple: { x: 0, y: 0, age: -1 },
    // The current event, and the clock that decides when the next one fires.
    // `pulse` is the only thing the rest of the HUD reads: the surrounding
    // field echoes it, so the environment answers the body.
    pulse: { kind: 0, at: -1, strength: 0, seq: 0 },
    nextEvent: 6,
    // Each visual state has a level that eases toward 0 or 1, so transitions
    // are crossfades rather than switches.
    levels: {
      listening: 0, processing: 0, planning: 0, executing: 0,
      speaking: 0, confirming: 0, error: 0, dissolving: 0,
    },
    fps: 0,
    _frames: 0,
    _fpsAt: 0,

    init(canvas) {
      this.canvas = canvas;
      try {
        this.ctx = create(canvas);
      } catch (error) {
        console.warn("[humanoid] disabled:", error.message);
        return false;
      }
      if (!this.ctx) {
        console.warn("[humanoid] WebGL2 unavailable; leaving the HUD as it was.");
        return false;
      }

      this.calm = window.matchMedia &&
                  window.matchMedia("(prefers-reduced-motion: reduce)").matches;

      this._resize = () => this.resize();
      this._move = (event) => {
        const box = canvas.getBoundingClientRect();
        const x = ((event.clientX - box.left) / box.width) * 2 - 1;
        const y = -(((event.clientY - box.top) / box.height) * 2 - 1);
        // Velocity in the same units the shader measures distance in, so
        // "how fast" and "how near" are comparable quantities.
        this.cursor.vx = x - this.cursor.x;
        this.cursor.vy = y - this.cursor.y;
        this.cursor.x = x;
        this.cursor.y = y;
        this.cursor.on = 1;
        this.cursor.moved = 1;
      };
      this._down = (event) => {
        const box = canvas.getBoundingClientRect();
        this.ripple.x = ((event.clientX - box.left) / box.width) * 2 - 1;
        this.ripple.y = -(((event.clientY - box.top) / box.height) * 2 - 1);
        this.ripple.age = 0;
      };
      this._leave = () => { this.cursor.on = 0; };
      this._visibility = () => {
        // A hidden tab should not burn GPU on a machine this tight.
        if (document.hidden) this.pause(); else this.play();
      };

      addEventListener("resize", this._resize);
      // The stage is not a fixed box: it collapses when a conversation starts.
      // Listening only for window resizes left the drawing buffer at its old
      // height, and the browser scaled a 400px-tall render into a 143px box —
      // the bust arrived vertically squashed. Watch the element, not the window.
      if (window.ResizeObserver) {
        this._observer = new ResizeObserver(() => this.resize());
        this._observer.observe(canvas);
      }
      addEventListener("pointermove", this._move, { passive: true });
      addEventListener("pointerdown", this._down, { passive: true });
      canvas.addEventListener("pointerleave", this._leave);
      document.addEventListener("visibilitychange", this._visibility);

      this.resize();
      this.started = performance.now();
      this.ready = true;
      this.play();
      return true;
    },

    resize() {
      if (!this.ctx) return;
      const dpr = Math.min(devicePixelRatio || 1, 2);
      const box = this.canvas.getBoundingClientRect();
      const w = Math.max(1, Math.round(box.width * dpr));
      const h = Math.max(1, Math.round(box.height * dpr));
      if (this.canvas.width !== w || this.canvas.height !== h) {
        this.canvas.width = w;
        this.canvas.height = h;
      }
      this.ctx.gl.viewport(0, 0, w, h);
      this.dpr = dpr;
    },

    /* The only way state enters. Names come from the rest of the HUD; this
       maps them, it does not decide them. */
    setState(agentState, voiceState, hasPending) {
      const v = voiceState || "off";
      const a = agentState || "idle";
      this.want = {
        listening: v === "listening" ? 1 : 0,
        // Analysing and verifying are the same posture: reading, not acting.
        processing: (v === "processing" || a === "analyzing" || a === "verifying") ? 1 : 0,
        planning: (a === "planning") ? 1 : 0,
        executing: (a === "executing" || a === "recovering") ? 1 : 0,
        speaking: (v === "speaking" || a === "speaking") ? 1 : 0,
        confirming: (v === "confirming" || hasPending || a === "awaiting") ? 1 : 0,
        error: (v === "error" || a === "error") ? 1 : 0,
        // Halted is not an error: the body lets go and returns to the field
        // rather than tearing itself apart.
        dissolving: a === "stopped" ? 1 : 0,
      };
    },

    setEnergy(level) {
      this.energy = Math.max(0, Math.min(1, level || 0));
    },

    assemble() { this.targetAssembly = 1; },
    disperse() { this.targetAssembly = 0; },

    /* Fire a procedural event. Weighted by what is happening, so a burst of
       neural activity is likelier while thinking and a field wave is likelier
       while speaking — but every one of them is a light, never a decision. */
    _schedule(seconds) {
      const L = this.levels;
      const roll = Math.random();
      let kind = EVENTS.SURGE;
      if (L.error > 0.4 || L.dissolving > 0.4) kind = EVENTS.REBUILD;
      else if (L.confirming > 0.4) kind = EVENTS.IGNITION;
      else if (L.processing + L.planning > 0.5) {
        kind = roll < 0.5 ? EVENTS.NEURAL : (roll < 0.8 ? EVENTS.SURGE : EVENTS.WAVE);
      } else if (L.speaking > 0.4) {
        kind = roll < 0.6 ? EVENTS.WAVE : EVENTS.SURGE;
      } else if (L.executing > 0.4) {
        kind = roll < 0.5 ? EVENTS.SURGE : EVENTS.IGNITION;
      } else {
        // Idle: rare, and the quieter kinds.
        kind = roll < 0.35 ? EVENTS.WAVE
             : roll < 0.60 ? EVENTS.SURGE
             : roll < 0.85 ? EVENTS.NEURAL : EVENTS.CASCADE;
      }
      this.pulse.kind = kind;
      this.pulse.at = seconds;
      this.pulse.strength = 0.55 + Math.random() * 0.5;
      this.pulse.seq += 1;
      const busy = Math.max(this.levels.processing, this.levels.executing);
      // Randomised, and never on a beat you could count.
      this.nextEvent = seconds + (busy > 0.4 ? 5 : 11) + Math.random() * 14;
    },

    play() {
      if (this.frame || !this.ready) return;
      this.last = performance.now();
      const loop = (now) => {
        this.frame = requestAnimationFrame(loop);
        this.draw(now);
      };
      this.frame = requestAnimationFrame(loop);
    },

    pause() {
      if (this.frame) cancelAnimationFrame(this.frame);
      this.frame = null;
    },

    draw(now) {
      const { gl, program, vaos, feedbacks, uniforms, count } = this.ctx;
      const dt = Math.min(0.05, (now - (this.last || now)) / 1000);
      this.last = now;
      const seconds = (now - this.started) / 1000;

      // Ease every level. Errors decay on their own so the figure always
      // reconstructs itself without needing to be told to.
      const want = this.want || {};
      for (const key of Object.keys(this.levels)) {
        const goal = want[key] ? 1 : 0;
        const rate = key === "error" ? 2.2 : 3.2;
        this.levels[key] += (goal - this.levels[key]) * Math.min(1, dt * rate);
      }
      this.assembly += (this.targetAssembly - this.assembly) *
                       Math.min(1, dt * (this.targetAssembly ? 0.85 : 1.8));

      // Cohesion: how hard the field is holding the body together. It is
      // never constant — even at rest it breathes on two slow clocks that do
      // not share a period, so the body is always slightly reorganising.
      const L = this.levels;
      const busy = Math.max(L.processing, Math.max(L.planning, L.executing));
      // A fault throws the body once. `shock` is that blow, and it decays
      // whether or not the fault clears, so the figure always reconstructs.
      if (want.error && !this._wasError) this.shock = 1;
      this._wasError = Boolean(want.error);
      this.shock *= Math.exp(-dt * 0.85);

      let hold = 0.90;
      if (L.dissolving > 0.3) hold = 0.05;
      // Cohesion returns as the shock fades: loose at the instant of the
      // fault, most of the way back a few seconds later.
      else if (L.error > 0.3) hold = 0.16 + (1 - this.shock) * 0.56;
      else if (L.confirming > 0.3) hold = 1.30;
      else if (L.listening > 0.3) hold = 1.08;
      else if (busy > 0.3) hold = 0.60;
      hold += Math.sin(seconds * 0.23) * 0.06 + Math.sin(seconds * 0.091) * 0.05;
      hold = Math.max(0.04, hold);
      this.cohesion += (hold - this.cohesion) * Math.min(1, dt * 1.6);

      // Speaking without a live amplitude still needs to look alive.
      if (this.levels.speaking > 0.05 && this.energy < 0.02) {
        this.energy = 0.35 + Math.sin(now / 190) * 0.22;
      } else {
        this.energy *= 0.92;
      }

      // The cursor's velocity decays whenever it stops moving, so a parked
      // pointer stops pushing and the body settles under it.
      if (!this.cursor.moved) { this.cursor.vx *= 0.82; this.cursor.vy *= 0.82; }
      this.cursor.moved = 0;
      if (this.ripple.age >= 0) {
        this.ripple.age += dt;
        if (this.ripple.age > 1.6) this.ripple.age = -1;
      }
      if (seconds >= this.nextEvent) this._schedule(seconds);
      const age = this.pulse.at >= 0 ? seconds - this.pulse.at : -1;
      const live = age >= 0 && age < 4.5;

      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.enable(gl.BLEND);
      gl.blendFunc(gl.SRC_ALPHA, gl.ONE);        // additive: particles add light
      gl.useProgram(program);

      gl.uniform1f(uniforms.uTime, seconds);
      gl.uniform1f(uniforms.uDt, Math.max(0.004, dt));
      gl.uniform1f(uniforms.uAssembly, this.assembly);
      gl.uniform1f(uniforms.uListening, L.listening);
      gl.uniform1f(uniforms.uProcessing, L.processing);
      gl.uniform1f(uniforms.uPlanning, L.planning);
      gl.uniform1f(uniforms.uExecuting, L.executing);
      gl.uniform1f(uniforms.uSpeaking, L.speaking);
      gl.uniform1f(uniforms.uConfirming, L.confirming);
      gl.uniform1f(uniforms.uError, L.error);
      gl.uniform1f(uniforms.uDissolve, L.dissolving);
      gl.uniform1f(uniforms.uEnergy, this.energy);
      gl.uniform1f(uniforms.uCohesion, this.cohesion);
      gl.uniform1f(uniforms.uShock, this.shock);
      gl.uniform2f(uniforms.uCursor, this.cursor.x, this.cursor.y);
      gl.uniform2f(uniforms.uCursorVel, this.cursor.vx, this.cursor.vy);
      gl.uniform1f(uniforms.uCursorOn, this.cursor.on);
      gl.uniform3f(uniforms.uRipple, this.ripple.x, this.ripple.y, this.ripple.age);
      gl.uniform4f(uniforms.uEvent, live ? this.pulse.kind : 0,
                   live ? age : 0, this.pulse.seq % 97,
                   live ? this.pulse.strength : 0);
      gl.uniform1f(uniforms.uAspect,
                   this.canvas.width / Math.max(1, this.canvas.height));
      gl.uniform1f(uniforms.uPixel, this.dpr || 1);
      // Square root, not linear: coverage goes as the square of the radius, so
      // this holds apparent density steady rather than apparent size.
      gl.uniform1f(uniforms.uStage,
                   Math.sqrt(Math.min(1, this.canvas.height / (400 * (this.dpr || 1)))));
      gl.uniform1f(uniforms.uCalm, this.calm ? 1 : 0);

      // Read one state set, write the other, and rasterise — all in the same
      // pass. The pair swaps every frame.
      const which = this._which || 0;
      gl.bindVertexArray(vaos[which]);
      gl.bindTransformFeedback(gl.TRANSFORM_FEEDBACK, feedbacks[which]);
      gl.beginTransformFeedback(gl.POINTS);
      gl.drawArrays(gl.POINTS, 0, count);
      gl.endTransformFeedback();
      gl.bindTransformFeedback(gl.TRANSFORM_FEEDBACK, null);
      gl.bindVertexArray(null);
      this._which = 1 - which;

      this._frames += 1;
      if (now - this._fpsAt > 1000) {
        this.fps = Math.round(this._frames * 1000 / (now - this._fpsAt));
        this._frames = 0;
        this._fpsAt = now;
      }
    },

    /* Release everything. A page that keeps its buffers after teardown is a
       leak on a machine already short of memory. */
    destroy() {
      this.pause();
      if (this._observer) { this._observer.disconnect(); this._observer = null; }
      removeEventListener("resize", this._resize);
      removeEventListener("pointermove", this._move);
      removeEventListener("pointerdown", this._down);
      if (this.canvas) this.canvas.removeEventListener("pointerleave", this._leave);
      document.removeEventListener("visibilitychange", this._visibility);
      if (this.ctx) {
        const { gl, program, vaos, feedbacks, buffers } = this.ctx;
        for (const buffer of buffers) gl.deleteBuffer(buffer);
        for (const vao of vaos) gl.deleteVertexArray(vao);
        for (const feedback of feedbacks) gl.deleteTransformFeedback(feedback);
        gl.deleteProgram(program);
        const lose = gl.getExtension("WEBGL_lose_context");
        if (lose) lose.loseContext();
      }
      this.ctx = null;
      this.ready = false;
    },
  };

  window.jarvishHumanoid = humanoid;
})();
