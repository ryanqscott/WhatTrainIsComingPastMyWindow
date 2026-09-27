import { useEffect, useReducer, useState } from "react";
import { parseTime, trainTiming } from "../lib/timing";
import type { ServerMessage, Train, WindowLocation } from "../types";

export type ConnectionStatus = "connecting" | "live" | "reconnecting";

type Trains = Map<string, Train>;

function reducer(trains: Trains, message: ServerMessage): Trains {
  const now = Date.now();
  if (message.type === "window_board") {
    // The board is the full picture; keep trains heard about since that it doesn't cover yet
    const next: Trains = new Map(message.data.map((train) => [train.id, train]));
    for (const [id, train] of trains) {
      if (!next.has(id) && trainTiming(train, now).stage !== "passed") next.set(id, train);
    }
    return next;
  }
  if (message.type === "train_update") {
    const previous = trains.get(message.data.id);
    const next = new Map(trains);
    // Updates can arrive before the appearance lookup has finished; keep what we had
    next.set(message.data.id, {
      ...previous,
      ...message.data,
      appearance: message.data.appearance ?? previous?.appearance ?? null,
    });
    // Drop trains well past the window
    for (const [id, train] of next) {
      if (now - parseTime(train.window_time) > 10 * 60_000) next.delete(id);
    }
    return next;
  }
  return trains;
}

/** Live trains from the backend WebSocket, reconnecting with backoff. */
export function useTrainFeed(url: string) {
  const [trains, dispatch] = useReducer(reducer, new Map<string, Train>());
  const [status, setStatus] = useState<ConnectionStatus>("connecting");
  const [location, setLocation] = useState<WindowLocation | null>(null);

  useEffect(() => {
    let socket: WebSocket | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let delay = 1000;
    let stopped = false;

    const connect = () => {
      setStatus(delay === 1000 ? "connecting" : "reconnecting");
      try {
        socket = new WebSocket(url);
      } catch {
        setStatus("reconnecting");
        return;
      }
      socket.onopen = () => {
        delay = 1000;
        setStatus("live");
      };
      socket.onmessage = (event) => {
        let message: ServerMessage;
        try {
          message = JSON.parse(event.data) as ServerMessage;
        } catch {
          return; // ignore malformed messages
        }
        if (message.type === "welcome" && message.location) setLocation(message.location);
        dispatch(message);
      };
      socket.onclose = () => {
        if (stopped) return;
        setStatus("reconnecting");
        retryTimer = setTimeout(connect, delay);
        delay = Math.min(delay * 2, 15_000);
      };
      socket.onerror = () => socket?.close();
    };

    connect();
    return () => {
      stopped = true;
      clearTimeout(retryTimer);
      socket?.close();
    };
  }, [url]);

  return { trains: [...trains.values()], status, location };
}
