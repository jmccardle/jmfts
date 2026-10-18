import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPoller } from "./poller";

const INTERVAL = 1000;

/** A `read` whose every call returns a promise the test settles by hand. */
function manualRead<T>() {
  const calls: { id: number; resolve: (v: T) => void; reject: (e: unknown) => void }[] = [];
  const read = vi.fn(
    (id: number) =>
      new Promise<T>((resolve, reject) => {
        calls.push({ id, resolve, reject });
      })
  );
  return { read, calls };
}

/** Lets a settled promise's continuation run under fake timers. */
const flush = () => vi.advanceTimersByTimeAsync(0);

describe("createPoller", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("schedules nothing after stop() while a read is in flight", async () => {
    const { read, calls } = manualRead<string>();
    const onResult = vi.fn();
    const poller = createPoller({
      read,
      isTerminal: () => false,
      intervalMs: INTERVAL,
      onResult,
      onError: vi.fn(),
    });

    poller.start(7);
    expect(read).toHaveBeenCalledTimes(1);
    poller.stop(); // unmount, with the read still awaiting
    calls[0].resolve("stalled");
    await vi.advanceTimersByTimeAsync(10 * INTERVAL);

    expect(read).toHaveBeenCalledTimes(1);
    expect(onResult).not.toHaveBeenCalled();
  });

  it("runs one loop, not two, when started twice while a read is in flight", async () => {
    const { read, calls } = manualRead<string>();
    const poller = createPoller({
      read,
      isTerminal: () => false,
      intervalMs: INTERVAL,
      onResult: vi.fn(),
      onError: vi.fn(),
    });

    poller.start(7);
    poller.start(7); // e.g. `was_existing` on the id already being watched
    expect(read).toHaveBeenCalledTimes(2);
    calls[0].resolve("a"); // the superseded read lands first and must schedule nothing
    calls[1].resolve("b");
    await flush();

    const N = 5;
    for (let i = 0; i < N; i += 1) {
      await vi.advanceTimersByTimeAsync(INTERVAL);
      expect(read).toHaveBeenCalledTimes(3 + i);
      calls[calls.length - 1].resolve("again");
      await flush();
    }
    expect(read).toHaveBeenCalledTimes(2 + N);
  });

  it("reports no error from a stale read that fails after a restart", async () => {
    const { read, calls } = manualRead<string>();
    const onError = vi.fn();
    const onIdle = vi.fn();
    const poller = createPoller({
      read,
      isTerminal: () => false,
      intervalMs: INTERVAL,
      onResult: vi.fn(),
      onError,
      onIdle,
    });

    poller.start(7);
    poller.start(8);
    const idleBefore = onIdle.mock.calls.length;
    calls[0].reject(new Error("stale"));
    await flush();

    expect(onError).not.toHaveBeenCalled();
    expect(onIdle).toHaveBeenCalledTimes(idleBefore);

    // The current read's failure still reaches the caller, and stops the loop.
    calls[1].reject(new Error("current"));
    await flush();
    expect(onError).toHaveBeenCalledTimes(1);
    expect(onError).toHaveBeenCalledWith(8, new Error("current"));
    await vi.advanceTimersByTimeAsync(10 * INTERVAL);
    expect(read).toHaveBeenCalledTimes(2);
  });

  it("stops scheduling once the value is terminal", async () => {
    const { read, calls } = manualRead<string>();
    const onResult = vi.fn();
    const onIdle = vi.fn();
    const poller = createPoller({
      read,
      isTerminal: (v) => v === "settled",
      intervalMs: INTERVAL,
      onResult,
      onError: vi.fn(),
      onIdle,
    });

    poller.start(7);
    calls[0].resolve("running");
    await vi.advanceTimersByTimeAsync(INTERVAL);
    expect(read).toHaveBeenCalledTimes(2);
    calls[1].resolve("settled");
    await vi.advanceTimersByTimeAsync(10 * INTERVAL);

    expect(read).toHaveBeenCalledTimes(2);
    expect(onResult.mock.calls).toEqual([
      [7, "running"],
      [7, "settled"],
    ]);
    expect(onIdle).toHaveBeenCalled();
  });

  it("starts nothing after dispose(), so a start that lands after unmount is inert", async () => {
    const { read, calls } = manualRead<string>();
    const onBegin = vi.fn();
    const poller = createPoller({
      read,
      isTerminal: () => false,
      intervalMs: INTERVAL,
      onBegin,
      onResult: vi.fn(),
      onError: vi.fn(),
    });

    // Ingest.vue: the upload is awaited, the page unmounts, then the await resumes and starts.
    poller.dispose();
    poller.start(7);
    await vi.advanceTimersByTimeAsync(10 * INTERVAL);

    expect(read).not.toHaveBeenCalled();
    expect(onBegin).not.toHaveBeenCalled();
    expect(calls).toHaveLength(0);
  });

  it("disowns a read in flight at dispose(), and stays disposed", async () => {
    const { read, calls } = manualRead<string>();
    const onResult = vi.fn();
    const poller = createPoller({
      read,
      isTerminal: () => false,
      intervalMs: INTERVAL,
      onResult,
      onError: vi.fn(),
    });

    poller.start(7);
    poller.dispose();
    calls[0].resolve("stalled");
    poller.start(8);
    await vi.advanceTimersByTimeAsync(10 * INTERVAL);

    expect(read).toHaveBeenCalledTimes(1);
    expect(onResult).not.toHaveBeenCalled();
  });
});
