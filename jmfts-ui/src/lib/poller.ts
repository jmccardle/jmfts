// Read something every `intervalMs` until it says it is finished, and stop when told to.
//
// Out of `Ingest.vue` so it can be tested without a DOM. The view kept the loop as one timer
// handle and nothing else, and a timer handle cannot reach a read that is waiting on its
// `await`: unmount during an in-flight read cleared nothing, the read resumed and scheduled the
// next one, and a `stalled` ingestion — which is not terminal — then polled forever behind a
// dead component. Re-watching the same id mid-read was worse: the old read passed the id check,
// scheduled its successor, and orphaned a timer that nothing held, so two loops ran.
//
// The fix is a GENERATION, the same one `Search.vue` uses for its columns. Every `start` and
// every `stop` advances it; a read remembers the generation it began under and, once its
// `await` returns, does nothing at all if that is no longer current — no result, no error, no
// successor. The timer is cleared before one is scheduled, so there is never a second handle.

export interface PollerOptions<T> {
  /** One reading of `id`. A rejection stops the loop; see `onError`. */
  read: (id: number) => Promise<T>;
  /** True when nothing more will change, so no further reading is scheduled. */
  isTerminal: (value: T) => boolean;
  intervalMs: number;
  /** A reading of `id` has begun. */
  onBegin?: (id: number) => void;
  /** A current reading's value. Never called for a reading a later `start`/`stop` superseded. */
  onResult: (id: number, value: T) => void;
  /**
   * A current reading failed, and the loop has stopped. Not retried behind the caller's back;
   * `start` again is the retry. Never called for a superseded reading.
   */
  onError: (id: number, failure: unknown) => void;
  /** Nothing is scheduled any more: terminal, failed, or stopped. */
  onIdle?: () => void;
}

export interface Poller {
  /** Stop whatever is running and read `id` now, then every interval until terminal. */
  start(id: number): void;
  /** Stop. A reading already in flight is disowned: its outcome reaches no callback. */
  stop(): void;
}

export function createPoller<T>(options: PollerOptions<T>): Poller {
  const { read, isTerminal, intervalMs, onBegin, onResult, onError, onIdle } = options;
  let generation = 0;
  let timer: ReturnType<typeof setTimeout> | null = null;

  function clearTimer() {
    if (timer !== null) clearTimeout(timer);
    timer = null;
  }

  async function tick(id: number, mine: number) {
    timer = null;
    onBegin?.(id);
    try {
      const value = await read(id);
      if (mine !== generation) return;
      onResult(id, value);
      if (isTerminal(value)) {
        onIdle?.();
      } else {
        clearTimer();
        timer = setTimeout(() => void tick(id, mine), intervalMs);
      }
    } catch (failure) {
      // A superseded reading that fails is not this watch's failure, and must not write over
      // the error line or the polling flag of the watch that replaced it.
      if (mine !== generation) return;
      onError(id, failure);
      onIdle?.();
    }
  }

  function stop() {
    generation += 1;
    clearTimer();
    onIdle?.();
  }

  function start(id: number) {
    stop();
    void tick(id, generation);
  }

  return { start, stop };
}
