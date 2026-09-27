import { clockTime, formatCountdown, parseTime } from "../lib/timing";
import type { Train } from "../types";

interface IdlePanelProps {
  next: Train | undefined;
  now: number;
  connected: boolean;
}

/** Shown when nothing is due within the minute: a big countdown to the next train. */
export function IdlePanel({ next, now, connected }: IdlePanelProps) {
  if (!next) {
    return (
      <div className="idle">
        <div className="idle-kicker">No trains known yet</div>
        <div className="idle-hint">
          {connected ? "Waiting for the backend's board…" : "Waiting for the backend connection…"}
        </div>
      </div>
    );
  }
  const at = parseTime(next.window_time);
  return (
    <div className="idle">
      <div className="idle-kicker">Next train past the window</div>
      <div className="idle-countdown">{formatCountdown((at - now) / 1000)}</div>
      <div className="idle-next">
        <span className="mono">{next.headcode}</span> {next.origin?.name ?? "?"} → {next.destination?.name ?? "?"} ·{" "}
        {next.passes_without_stopping ? "passing through" : "stopping"} at {clockTime(at)}
      </div>
      <div className="idle-hint">The card appears a minute before it reaches the window.</div>
    </div>
  );
}
