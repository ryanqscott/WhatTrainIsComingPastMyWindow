import { useMemo, useState } from "react";
import { Header } from "./components/Header";
import { IdlePanel } from "./components/IdlePanel";
import { SettingsPanel } from "./components/SettingsPanel";
import { TrainCard } from "./components/TrainCard";
import { UpcomingList } from "./components/UpcomingList";
import { useNow } from "./hooks/useNow";
import { useSettings } from "./hooks/useSettings";
import { useTrainFeed } from "./hooks/useTrainFeed";
import { isOnStage, otherScreenSide, parseTime, trainTiming } from "./lib/timing";
import type { ScreenSide } from "./types";

/** Up to two trains at once, e.g. crossing in opposite directions. */
const MAX_STAGE_CARDS = 2;
const UPCOMING_COUNT = 6;

export default function App() {
  const now = useNow();
  const { directionAGoes, setDirectionAGoes, wsUrl, setWsUrl } = useSettings();
  const { trains, status, location } = useTrainFeed(wsUrl);
  const [settingsOpen, setSettingsOpen] = useState(false);

  const timed = useMemo(
    () =>
      trains
        .filter((train) => !Number.isNaN(parseTime(train.window_time)))
        .map((train) => ({ train, timing: trainTiming(train, now) }))
        .sort((a, b) => a.timing.at - b.timing.at),
    [trains, now],
  );

  const onStage = timed.filter(({ train, timing }) => !train.cancelled && isOnStage(timing.stage)).slice(0, MAX_STAGE_CARDS);
  const stagedIds = new Set(onStage.map(({ train }) => train.id));
  const upcoming = timed
    .filter(({ train, timing }) => !stagedIds.has(train.id) && (timing.stage === "later" || timing.stage === "approaching"))
    .slice(0, UPCOMING_COUNT)
    .map(({ train }) => train);
  // Each end of the track is named after the direction trains leave the view that way
  const towards = (id: "a" | "b") => location?.sides.find((side) => side.id === id)?.towards ?? "";
  const trackEnds = {
    [directionAGoes]: towards("a"),
    [otherScreenSide(directionAGoes)]: towards("b"),
  } as Record<ScreenSide, string>;

  const next = timed.find(({ train, timing }) => !train.cancelled && timing.stage === "later")?.train;

  return (
    <>
      <Header
        stationName={location?.name ?? null}
        now={now}
        status={status}
        wsUrl={wsUrl}
        settingsOpen={settingsOpen}
        onToggleSettings={() => setSettingsOpen((open) => !open)}
      />
      {settingsOpen && (
        <SettingsPanel
          location={location}
          directionAGoes={directionAGoes}
          onDirectionAGoes={setDirectionAGoes}
          wsUrl={wsUrl}
          onWsUrl={setWsUrl}
        />
      )}
      <main>
        <section className={`stage${onStage.length > 1 ? " two" : ""}`} aria-live="polite">
          {onStage.length ? (
            onStage.map(({ train, timing }) => (
              <TrainCard
                key={train.id}
                train={train}
                timing={timing}
                directionAGoes={directionAGoes}
                trackEnds={trackEnds}
              />
            ))
          ) : (
            <IdlePanel next={next} now={now} connected={status === "live"} />
          )}
        </section>
        <UpcomingList trains={upcoming} now={now} />
      </main>
    </>
  );
}
