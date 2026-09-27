/** Shapes of the messages sent by backend/main.py (see window_tracker.py and appearance.py). */

export type TimeSource =
  | "signalled"
  | "signalled_estimate"
  | "actual"
  | "estimated"
  | "scheduled"
  | "interpolated";

export type TrainKind =
  | "passenger"
  | "empty_stock"
  | "freight"
  | "engineering"
  | "light_locomotive"
  | "parcels"
  | "bus"
  | "ship"
  | "other";

export interface Place {
  tiploc: string;
  name: string | null;
}

export interface Photo {
  title: string;
  thumb_url: string | null;
  full_url: string | null;
  page_url: string | null;
  author: string | null;
  license: string | null;
  license_url: string | null;
  date_taken: string | null;
  match?: "exact_unit" | "same_class_and_livery";
}

export interface Livery {
  code: string | null;
  description: string | null;
}

export interface ClassInfo {
  title: string | null;
  description: string | null;
  extract: string | null;
  url: string | null;
  thumbnail: string | null;
}

export interface Unit {
  unit_number: string;
  fleet_id?: string | null;
}

export interface Appearance {
  confidence: "exact" | "likely" | "timetable" | "unknown";
  source: string;
  summary: string;
  basis?: string;
  train_class: string | null;
  cars: number | null;
  units: Unit[];
  livery: Livery | null;
  photo: Photo | null;
  class_info: ClassInfo | null;
  seats?: number | null;
  likely_operator?: string | null;
}

export interface Train {
  id: string;
  rid: string | null;
  uid: string | null;
  headcode: string | null;
  toc: string | null;
  operator: string | null;
  category: string | null;
  category_description: string | null;
  train_kind: TrainKind | null;
  is_passenger: boolean | null;
  power_type: string | null;
  max_speed_mph: number | null;
  origin: Place | null;
  destination: Place | null;
  stops_at_station: boolean | null;
  passes_without_stopping: boolean | null;
  /** Local time without a zone, e.g. "2026-09-27T10:56:30" */
  window_time: string;
  window_time_source: TimeSource;
  arrival: string | null;
  departure: string | null;
  scheduled: { wta?: string; wtd?: string; wtp?: string; pta?: string; ptd?: string };
  /** e.g. "towards Putney" */
  direction: string | null;
  /** Which of the location's two directions the train is heading: "a" or "b" */
  direction_side: DirectionId | null;
  platform: string | null;
  length: string | null;
  cancelled: boolean;
  late_reason: string | null;
  appearance?: Appearance | null;
}

export type DirectionId = "a" | "b";

/** The station the window overlooks and its two directions (backend location.py). */
export interface WindowLocation {
  code: string;
  name: string;
  sides: { id: DirectionId; towards: string; label: string }[];
}

export type ServerMessage =
  | { type: "welcome"; message: string; location?: WindowLocation }
  | { type: "window_board"; data: Train[]; timestamp: string }
  | { type: "train_update"; data: Train; timestamp: string };

/** Left or right as seen from the window. */
export type ScreenSide = "left" | "right";
