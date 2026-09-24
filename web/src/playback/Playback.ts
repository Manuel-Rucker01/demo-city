/** Play/pause/speed/scrub playback engine driven by a rAF loop with a fixed ticks/s rate. */

export type SpeedMultiplier = 1 | 2 | 5 | 10;

const BASE_TICKS_PER_SEC = 4; // 1x speed

export interface PlaybackState {
  tickIndex: number; // 0 = initial population, 1..maxTick = after that tick
  playing: boolean;
  speed: SpeedMultiplier;
}

export type PlaybackListener = (state: PlaybackState) => void;

export class Playback {
  private tickIndex = 0;
  private playing = false;
  private speed: SpeedMultiplier = 1;
  private accumulatorMs = 0;
  private lastFrameMs = 0;
  private rafHandle = 0;
  private listeners = new Set<PlaybackListener>();
  private maxTick: number;

  constructor(maxTick: number) {
    this.maxTick = maxTick;
  }

  setMaxTick(maxTick: number): void {
    this.maxTick = maxTick;
    if (this.tickIndex > maxTick) this.tickIndex = maxTick;
  }

  onChange(listener: PlaybackListener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private emit(): void {
    const state: PlaybackState = { tickIndex: this.tickIndex, playing: this.playing, speed: this.speed };
    // A listener exception must never propagate out of here: `loop()` calls emit() before
    // scheduling its next requestAnimationFrame, so an uncaught throw from one listener (e.g. a
    // chart choking on unusual data) would otherwise silently freeze all future playback.
    for (const l of this.listeners) {
      try {
        l(state);
      } catch (err) {
        console.error("Playback: listener threw, continuing playback", err);
      }
    }
  }

  play(): void {
    if (this.playing) return;
    this.playing = true;
    this.lastFrameMs = performance.now();
    this.loop();
    this.emit();
  }

  pause(): void {
    this.playing = false;
    cancelAnimationFrame(this.rafHandle);
    this.emit();
  }

  toggle(): void {
    this.playing ? this.pause() : this.play();
  }

  setSpeed(speed: SpeedMultiplier): void {
    this.speed = speed;
    this.emit();
  }

  seek(tickIndex: number): void {
    this.tickIndex = Math.max(0, Math.min(this.maxTick, Math.round(tickIndex)));
    this.accumulatorMs = 0;
    this.emit();
  }

  step(delta: number): void {
    this.seek(this.tickIndex + delta);
  }

  private loop = (): void => {
    if (!this.playing) return;
    const now = performance.now();
    const dt = now - this.lastFrameMs;
    this.lastFrameMs = now;
    this.accumulatorMs += dt * this.speed;
    const msPerTick = 1000 / BASE_TICKS_PER_SEC;
    let advanced = false;
    while (this.accumulatorMs >= msPerTick) {
      this.accumulatorMs -= msPerTick;
      if (this.tickIndex < this.maxTick) {
        this.tickIndex += 1;
        advanced = true;
      } else {
        this.playing = false;
        break;
      }
    }
    if (advanced || !this.playing) this.emit();
    if (this.playing) this.rafHandle = requestAnimationFrame(this.loop);
  };

  getState(): PlaybackState {
    return { tickIndex: this.tickIndex, playing: this.playing, speed: this.speed };
  }

  attachKeyboard(target: Window | HTMLElement = window): () => void {
    const handler = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return;
      switch (e.code) {
        case "Space":
          e.preventDefault();
          this.toggle();
          break;
        case "ArrowRight":
          e.preventDefault();
          this.step(1);
          break;
        case "ArrowLeft":
          e.preventDefault();
          this.step(-1);
          break;
      }
    };
    target.addEventListener("keydown", handler as EventListener);
    return () => target.removeEventListener("keydown", handler as EventListener);
  }
}
