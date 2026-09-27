import { headingSide, type Stage, type Timing } from "../lib/timing";
import type { ScreenSide, TimeSource, Train } from "../types";
import { Track } from "./Track";
import { TrainPhoto, photoFor } from "./TrainPhoto";

const SOURCE_LABELS: Record<TimeSource, { label: string; strong: boolean }> = {
  signalled: { label: "Seen by signalling", strong: true },
  actual: { label: "Actual time", strong: true },
  signalled_estimate: { label: "Signalling estimate", strong: false },
  estimated: { label: "Live estimate", strong: false },
  scheduled: { label: "Timetable", strong: false },
  interpolated: { label: "Estimated from timetable", strong: false },
};

function statusText(train: Train, timing: Timing): string {
  const labels: Partial<Record<Stage, string>> = {
    approaching: `In ${Math.max(1, Math.ceil(timing.secondsAway))} s`,
    passing: ["signalled", "actual"].includes(train.window_time_source) ? "Passing now" : "Due now",
    arriving: "Arriving",
    dwelling: "At the station now",
    departing: "Departing",
    passed: "Just passed",
  };
  return labels[timing.stage] ?? "";
}

const placeName = (place: Train["origin"]) => place?.name || place?.tiploc || "?";

function facts(train: Train): [string, string][] {
  const looks = train.appearance;
  const rows: [string, string | number | null | undefined][] = [];
  rows.push(["Coaches", looks?.cars ?? train.length]);
  const units = (looks?.units ?? []).map((unit) => unit.unit_number).filter(Boolean);
  if (units.length) rows.push([units.length > 1 ? "Units" : "Unit", units.join(" + ")]);
  rows.push(["Livery", looks?.livery?.description]);
  rows.push(["Seats", looks?.seats]);
  rows.push(["Based on", looks?.basis]);
  rows.push(["Booked", train.scheduled.wtp || train.scheduled.wta || train.scheduled.wtd]);
  if (train.train_kind !== "passenger") rows.push(["Max speed", train.max_speed_mph && `${train.max_speed_mph} mph`]);
  rows.push(["Delay reason", train.late_reason && `code ${train.late_reason}`]);
  return rows.filter((row): row is [string, string | number] => row[1] != null && row[1] !== "")
    .map(([label, value]) => [label, String(value)]);
}

interface TrainCardProps {
  train: Train;
  timing: Timing;
  directionAGoes: ScreenSide;
  trackEnds: Record<ScreenSide, string>;
}

export function TrainCard({ train, timing, directionAGoes, trackEnds }: TrainCardProps) {
  const looks = train.appearance;
  const source = SOURCE_LABELS[train.window_time_source];
  const atWindow = timing.stage !== "approaching";
  const hasPhoto = Boolean(photoFor(looks).src);

  const movement = [
    train.stops_at_station
      ? `Stopping${train.platform ? ` at platform ${train.platform}` : ""}`
      : train.passes_without_stopping
        ? "Passing through without stopping"
        : null,
    train.direction && `heading ${train.direction}`,
  ].filter(Boolean).join(", ");

  return (
    <article className={`card ${atWindow ? "phase-passing" : "phase-approaching"}${hasPhoto ? "" : " no-photo"}`}>
      <div className="card-top">
        <span className="status-pill">{statusText(train, timing)}</span>
        {source && <span className={`source-chip${source.strong ? " strong" : ""}`}>{source.label}</span>}
      </div>

      <div className="card-body">
        <TrainPhoto appearance={looks} alt={looks?.summary ?? `Train ${train.headcode ?? ""}`} />

        <div className="info">
          <div className="identity">
            <span className="headcode mono">{train.headcode ?? "????"}</span>
            <span className="kind">{[train.operator, train.category_description].filter(Boolean).join(" · ")}</span>
          </div>
          <div className="route">
            {placeName(train.origin)}
            <span className="arrow">→</span>
            {placeName(train.destination)}
          </div>

          <Track progress={timing.progress} heading={headingSide(train, directionAGoes)} ends={trackEnds} />
          {movement && <div className="direction">{movement}</div>}

          <p className="looks">
            <span className={`confidence${looks?.confidence === "exact" ? " exact" : ""}`}>
              {looks?.confidence ?? "unknown"}
            </span>
            {looks?.summary ?? "No rolling stock information for this train yet"}
          </p>

          <dl className="facts">
            {facts(train).map(([label, value]) => (
              <div key={label} className="fact">
                <dt>{label}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>

          {looks?.class_info?.extract && (
            <details className="class-info">
              <summary>About the {looks.class_info.title ?? "class"}</summary>
              <p>
                {looks.class_info.extract}{" "}
                {looks.class_info.url && (
                  <a href={looks.class_info.url} target="_blank" rel="noopener noreferrer">
                    Wikipedia
                  </a>
                )}
              </p>
            </details>
          )}
        </div>
      </div>
    </article>
  );
}
