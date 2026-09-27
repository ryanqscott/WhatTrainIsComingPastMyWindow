import { clockTime, formatCountdown, parseTime } from "../lib/timing";
import type { Train } from "../types";

export function UpcomingList({ trains, now }: { trains: Train[]; now: number }) {
  return (
    <section className="upcoming">
      <h2 className="section-title">Coming up</h2>
      <ol className="upcoming-list">
        {trains.length === 0 && <li className="upcoming-empty">Nothing else known for the next 90 minutes yet.</li>}
        {trains.map((train) => {
          const at = parseTime(train.window_time);
          const tag = train.appearance?.train_class ? `Class ${train.appearance.train_class}` : train.category_description;
          const action = train.passes_without_stopping ? "passes" : train.stops_at_station ? "stops" : "";
          return (
            <li key={train.id} className={train.cancelled ? "cancelled" : undefined}>
              <span className="when mono">{clockTime(at)}</span>
              <span className="in">{formatCountdown((at - now) / 1000)}</span>
              <span className="code mono">{train.headcode}</span>
              <span className="where">
                {train.destination?.name ?? train.destination?.tiploc ?? "?"}
                <small>{[action, train.cancelled && "cancelled"].filter(Boolean).join(" · ")}</small>
              </span>
              <span className="tag">{tag}</span>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
