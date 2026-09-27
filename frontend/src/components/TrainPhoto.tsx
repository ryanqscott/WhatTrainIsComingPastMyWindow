import type { Appearance } from "../types";

/** Exact-unit photo if we have one, else the class in its usual livery, else the class's Wikipedia image. */
export function photoFor(appearance: Appearance | null | undefined) {
  const photo = appearance?.photo ?? null;
  const src = photo?.thumb_url || appearance?.class_info?.thumbnail || null;
  return { photo, src };
}

function badgeText(appearance: Appearance | null | undefined): string {
  const photo = appearance?.photo;
  if (!photo) return "Class photo";
  if (photo.match === "exact_unit") return "This unit";
  return appearance?.confidence === "exact" ? "Same class & livery" : "Typical for this service";
}

export function TrainPhoto({ appearance, alt }: { appearance: Appearance | null | undefined; alt: string }) {
  const { photo, src } = photoFor(appearance);
  if (!src) return null;
  return (
    <figure className="photo">
      <img src={src} alt={alt} loading="lazy" />
      <span className="photo-badge">{badgeText(appearance)}</span>
      <figcaption>
        {photo ? (
          <>
            {photo.page_url ? (
              <a href={photo.page_url} target="_blank" rel="noopener noreferrer">
                {photo.author || "Wikimedia Commons"}
              </a>
            ) : (
              photo.author
            )}
            {photo.license && ` · ${photo.license}`}
            {photo.date_taken && ` · ${photo.date_taken}`}
          </>
        ) : (
          "Wikipedia"
        )}
      </figcaption>
    </figure>
  );
}
