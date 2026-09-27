import type { ConnectionStatus } from "../hooks/useTrainFeed";
import { clockTime } from "../lib/timing";

const STATUS_LABELS: Record<ConnectionStatus, string> = {
  connecting: "Connecting",
  live: "Live",
  reconnecting: "Reconnecting",
};

interface HeaderProps {
  stationName: string | null;
  now: number;
  status: ConnectionStatus;
  wsUrl: string;
  settingsOpen: boolean;
  onToggleSettings: () => void;
}

export function Header({ stationName, now, status, wsUrl, settingsOpen, onToggleSettings }: HeaderProps) {
  return (
    <header className="topbar">
      <div className="place">
        <span className="place-name">{stationName ?? "Past my window"}</span>
        <span className="place-sub">{stationName ? "past my window" : "connecting…"}</span>
      </div>
      <div className="topbar-right">
        <span className="clock mono">{clockTime(now, true)}</span>
        <span className={`connection ${status}`} title={`${STATUS_LABELS[status]} (${wsUrl})`}>
          <span className="dot" />
          <span className="label">{STATUS_LABELS[status]}</span>
        </span>
        <button className="icon-button" aria-label="Settings" aria-expanded={settingsOpen} onClick={onToggleSettings}>
          <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true">
            <path
              fill="currentColor"
              d="M12 15.5A3.5 3.5 0 1 1 12 8.5a3.5 3.5 0 0 1 0 7Zm7.43-2.53a7.8 7.8 0 0 0 0-1.94l2.03-1.58a.5.5 0 0 0 .12-.64l-1.92-3.32a.5.5 0 0 0-.61-.22l-2.39.96a7.3 7.3 0 0 0-1.68-.97l-.36-2.54A.5.5 0 0 0 14.13 2h-3.84a.5.5 0 0 0-.49.42l-.36 2.54c-.6.24-1.17.57-1.68.97l-2.39-.96a.5.5 0 0 0-.61.22L2.84 8.51a.5.5 0 0 0 .12.64l2.03 1.58a7.8 7.8 0 0 0 0 1.94l-2.03 1.58a.5.5 0 0 0-.12.64l1.92 3.32c.13.22.39.3.61.22l2.39-.96c.51.4 1.08.73 1.68.97l.36 2.54c.04.24.25.42.49.42h3.84c.24 0 .45-.18.49-.42l.36-2.54c.6-.24 1.17-.57 1.68-.97l2.39.96c.22.08.48 0 .61-.22l1.92-3.32a.5.5 0 0 0-.12-.64l-2.03-1.58Z"
            />
          </svg>
        </button>
      </div>
    </header>
  );
}
