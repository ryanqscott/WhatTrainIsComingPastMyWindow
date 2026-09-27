import type { CSSProperties } from "react";
import type { ScreenSide } from "../types";

interface TrackProps {
  /** 0 = far side, 0.5 = at the window, 1 = gone; in the direction of travel. */
  progress: number;
  heading: ScreenSide | null;
  /** Where each end of the view leads, e.g. { left: "Clapham Junction", right: "Putney" } */
  ends: Record<ScreenSide, string>;
}

/** A strip showing the train crossing the view from the window. */
export function Track({ progress, heading, ends }: TrackProps) {
  const position = heading === "left" ? 1 - progress : progress;
  return (
    <div className="track" aria-hidden="true">
      <span className="rail" />
      <span className="window-marker" />
      <span className="track-end track-left">{ends.left}</span>
      <span className="track-end track-right">{ends.right}</span>
      {heading && (
        <span
          className={`train-glyph${heading === "left" ? " heading-left" : ""}`}
          style={{ "--progress": position.toFixed(3) } as CSSProperties}
        />
      )}
    </div>
  );
}
