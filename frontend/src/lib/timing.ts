import type { ScreenSide, TimeSource, Train } from "../types";

/** A train gets a card this long before it reaches the window. */
export const APPROACH_SECONDS = 60;

/**
 * How long before/after its time a train counts as "at the window". Timetable
 * estimates are rougher than signalling sightings, so they get wider margins.
 */
const MARGINS: Record<TimeSource, { before: number; after: number }> = {
  signalled: { before: 5, after: 20 },
  actual: { before: 5, after: 25 },
  signalled_estimate: { before: 15, after: 30 },
  estimated: { before: 20, after: 40 },
  scheduled: { before: 30, after: 60 },
  interpolated: { before: 45, after: 75 },
};

/** Default dwell when a stopping train has no departure time. */
const DEFAULT_DWELL_MS = 30_000;

export type Stage =
  | "later"        // more than a minute away
  | "approaching"  // within the minute
  | "passing"      // non-stopping train going past
  | "arriving"     // stopping train pulling in
  | "dwelling"     // stopping train standing at the platform
  | "departing"    // stopping train pulling out
  | "passed";

export interface Timing {
  stage: Stage;
  /** Seconds until the train reaches the window (negative once it has). */
  secondsAway: number;
  /** Position along the track in the direction of travel: 0 far side, 0.5 the window, 1 gone. */
  progress: number;
  /** When the train is at the window (arrival for stopping trains). */
  at: number;
}

/** Backend times are local wall-clock times without a zone, which Date parses as local. */
export const parseTime = (iso: string | null | undefined): number => (iso ? new Date(iso).getTime() : NaN);

const clamp = (value: number, min = 0, max = 1) => Math.min(max, Math.max(min, value));

export function trainTiming(train: Train, now: number): Timing {
  const margin = MARGINS[train.window_time_source] ?? MARGINS.estimated;
  const stops = Boolean(train.stops_at_station);
  const at = parseTime(stops && train.arrival ? train.arrival : train.window_time);
  const secondsAway = (at - now) / 1000;
  const start = at - margin.before * 1000;

  if (Number.isNaN(at)) return { stage: "later", secondsAway: Infinity, progress: 0, at };

  if (now < start - APPROACH_SECONDS * 1000) return { stage: "later", secondsAway, progress: 0, at };
  if (now < start) {
    const fraction = 1 - (start - now) / (APPROACH_SECONDS * 1000);
    return { stage: "approaching", secondsAway, progress: 0.45 * fraction, at };
  }

  if (!stops) {
    const end = at + margin.after * 1000;
    if (now > end) return { stage: "passed", secondsAway, progress: 1, at };
    return { stage: "passing", secondsAway, progress: 0.45 + 0.55 * clamp((now - start) / (end - start)), at };
  }

  // Stopping trains: pull in, stand at the window until departure, then pull out
  let departure = parseTime(train.departure);
  if (Number.isNaN(departure) || departure < at) departure = at + DEFAULT_DWELL_MS;
  const leaveBy = departure + margin.after * 1000;
  if (now < at) return { stage: "arriving", secondsAway, progress: 0.45 + 0.05 * clamp((now - start) / (at - start)), at };
  if (now <= departure) return { stage: "dwelling", secondsAway, progress: 0.5, at };
  if (now <= leaveBy) return { stage: "departing", secondsAway, progress: 0.5 + 0.5 * clamp((now - departure) / (leaveBy - departure)), at };
  return { stage: "passed", secondsAway, progress: 1, at };
}

export const isOnStage = (stage: Stage) => stage !== "later" && stage !== "passed";

export const otherScreenSide = (side: ScreenSide): ScreenSide => (side === "left" ? "right" : "left");

/** Which way the train crosses the view, given which way trains in direction "a" go. */
export function headingSide(train: Train, directionAGoes: ScreenSide): ScreenSide | null {
  if (!train.direction_side) return null;
  return train.direction_side === "a" ? directionAGoes : otherScreenSide(directionAGoes);
}

export function formatCountdown(seconds: number): string {
  if (!Number.isFinite(seconds)) return "";
  if (seconds <= 0) return "now";
  if (seconds < 60) return `${Math.ceil(seconds)} s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 10) return `${minutes} min ${Math.floor(seconds % 60)} s`;
  if (minutes < 60) return `${minutes} min`;
  return `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

const pad = (n: number) => String(n).padStart(2, "0");
export const clockTime = (ms: number, seconds = false) => {
  const d = new Date(ms);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}${seconds ? `:${pad(d.getSeconds())}` : ""}`;
};
