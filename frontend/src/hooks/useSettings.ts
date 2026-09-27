import { useCallback, useState } from "react";
import type { ScreenSide } from "../types";

export const DEFAULT_WS_URL = "ws://localhost:8767";

// localStorage can be unavailable (private windows, blocked storage), so never rely on it
function read(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key: string, value: string) {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* settings just won't persist */
  }
}

function usePersisted<T extends string>(key: string, initial: () => T) {
  const [value, setValue] = useState<T>(() => (read(key) as T | null) ?? initial());
  const update = useCallback(
    (next: T) => {
      setValue(next);
      write(key, next);
    },
    [key],
  );
  return [value, update] as const;
}

export function useSettings() {
  // Which way (seen from the window) trains heading in the location's direction "a" go
  const [directionAGoes, setDirectionAGoes] = usePersisted<ScreenSide>("directionAGoes", () => "left");
  const [wsUrl, setWsUrl] = usePersisted<string>(
    "wsUrl",
    () => new URLSearchParams(location.search).get("ws") ?? DEFAULT_WS_URL,
  );
  return { directionAGoes, setDirectionAGoes, wsUrl, setWsUrl };
}
