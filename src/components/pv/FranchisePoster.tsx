import { Film } from "lucide-react";
import { useEffect, useState } from "react";
import {
  FRANCHISE_POSTER_INTERVAL_MS,
  franchisePosterAt,
} from "@/lib/movie-franchise-presentation";

/** One still member poster; no slideshow, new artwork, or durable rotation state. */
export function FranchisePoster({ name, posters }: { name: string; posters: string[] }) {
  const [time, setTime] = useState(() => Date.now());
  const [failed, setFailed] = useState<ReadonlySet<string>>(() => new Set());
  const rotates = posters.length > 1;
  useEffect(() => {
    if (!rotates) return;
    let timer: ReturnType<typeof setTimeout>;
    const schedule = () => {
      timer = setTimeout(
        () => {
          setTime(Date.now());
          schedule();
        },
        FRANCHISE_POSTER_INTERVAL_MS - (Date.now() % FRANCHISE_POSTER_INTERVAL_MS) + 1,
      );
    };
    schedule();
    return () => clearTimeout(timer);
  }, [rotates]);
  const poster = franchisePosterAt(posters, time, failed);
  return (
    <div className="aspect-[2/3] relative flex items-center justify-center bg-[#111115]">
      {poster ? (
        <img
          key={poster}
          src={poster}
          alt={`${name} artwork`}
          loading="lazy"
          className="absolute inset-0 h-full w-full object-cover"
          onError={() => setFailed((current) => new Set([...current, poster]))}
        />
      ) : (
        <Film size={40} style={{ color: "var(--pv-silver-dim)" }} />
      )}
    </div>
  );
}
