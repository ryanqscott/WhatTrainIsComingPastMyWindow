import { useState } from "react";
import { DEFAULT_WS_URL } from "../hooks/useSettings";
import type { ScreenSide, WindowLocation } from "../types";

interface SettingsPanelProps {
  location: WindowLocation | null;
  directionAGoes: ScreenSide;
  onDirectionAGoes: (side: ScreenSide) => void;
  wsUrl: string;
  onWsUrl: (url: string) => void;
}

export function SettingsPanel({ location, directionAGoes, onDirectionAGoes, wsUrl, onWsUrl }: SettingsPanelProps) {
  const directionA = location?.sides.find((side) => side.id === "a")?.towards ?? "direction A";
  const [draftUrl, setDraftUrl] = useState(wsUrl);
  return (
    <section className="settings">
      <div className="setting">
        <span className="setting-label">From my window, trains towards {directionA} go to my</span>
        <div className="segmented" role="radiogroup" aria-label={`Which way trains towards ${directionA} go`}>
          {(["left", "right"] as const).map((side) => (
            <button key={side} role="radio" aria-checked={directionAGoes === side} onClick={() => onDirectionAGoes(side)}>
              {side === "left" ? "Left" : "Right"}
            </button>
          ))}
        </div>
      </div>
      <form
        className="setting"
        onSubmit={(event) => {
          event.preventDefault();
          onWsUrl(draftUrl.trim() || DEFAULT_WS_URL);
        }}
      >
        <label className="setting-label" htmlFor="ws-url">Backend</label>
        <input
          id="ws-url"
          className="text-input mono"
          spellCheck={false}
          value={draftUrl}
          onChange={(event) => setDraftUrl(event.target.value)}
        />
        <button className="small-button" type="submit">Connect</button>
      </form>
    </section>
  );
}
