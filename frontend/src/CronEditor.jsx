import { useState } from "react";
import { Select } from "./Select";

function build(freq, hour, minute, dow) {
  const m = minute,
    h = hour;
  if (freq === "hourly") return `${m} * * * *`;
  if (freq === "daily") return `${m} ${h} * * *`;
  if (freq === "weekly") return `${m} ${h} * * ${dow}`;
  return `${m} ${h} * * *`;
}

// Returns true if `cron` is expressible by the guided editor (build()).
function matchesKnownPattern(cron) {
  if (!cron) return false;
  const parts = cron.trim().split(/\s+/);
  if (parts.length !== 5) return false;
  const [, h, dom, mon, dow] = parts;
  if (dom !== "*" || mon !== "*") return false;
  // hourly: M * * * *
  if (h === "*" && dow === "*") return true;
  // daily: M H * * *
  if (h !== "*" && dow === "*") return true;
  // weekly: M H * * D
  if (h !== "*" && dow !== "*") return true;
  return false;
}

// Parse a known-pattern cron string into { freq, hour, minute, dow }.
function parseCron(cron) {
  const parts = cron.trim().split(/\s+/);
  const [m, h, , , d] = parts;
  const minute = parseInt(m, 10);
  const hour = parseInt(h, 10);
  const dow = d === "*" ? 1 : parseInt(d, 10);
  let freq = "daily";
  if (h === "*") freq = "hourly";
  else if (d !== "*") freq = "weekly";
  return {
    freq,
    hour: isNaN(hour) ? 8 : hour,
    minute: isNaN(minute) ? 0 : minute,
    dow: isNaN(dow) ? 1 : dow,
  };
}

const FREQ_OPTIONS = [
  { value: "hourly", label: "Ogni ora" },
  { value: "daily", label: "Ogni giorno" },
  { value: "weekly", label: "Ogni settimana" },
];

const HOUR_OPTIONS = Array.from({ length: 24 }, (_, i) => ({
  value: i,
  label: `${i}:00`,
}));
const MINUTE_OPTIONS = [0, 15, 30, 45].map((m) => ({
  value: m,
  label: `:${String(m).padStart(2, "0")}`,
}));
const DOW_OPTIONS = [
  { value: 0, label: "Domenica" },
  { value: 1, label: "Lunedì" },
  { value: 2, label: "Martedì" },
  { value: 3, label: "Mercoledì" },
  { value: 4, label: "Giovedì" },
  { value: 5, label: "Venerdì" },
  { value: 6, label: "Sabato" },
];

export function CronEditor({ value, onChange }) {
  const parsed = value && matchesKnownPattern(value) ? parseCron(value) : null;

  const [freq, setFreq] = useState(parsed?.freq ?? "daily");
  const [hour, setHour] = useState(parsed?.hour ?? 8);
  const [minute, setMinute] = useState(parsed?.minute ?? 0);
  const [dow, setDow] = useState(parsed?.dow ?? 1);

  // apply() is called only on explicit user interaction, never at mount.
  const apply = (f, h, m, d) => onChange(build(f, h, m, d));

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-2">
        <div className="col-span-2">
          <label className="text-xs text-muted mb-1 block">Frequenza</label>
          <Select
            ariaLabel="Frequenza"
            value={freq}
            options={FREQ_OPTIONS}
            onChange={(v) => {
              setFreq(v);
              apply(v, hour, minute, dow);
            }}
          />
        </div>
        {freq !== "hourly" && (
          <div>
            <label className="text-xs text-muted mb-1 block">Ora</label>
            <Select
              ariaLabel="Ora"
              value={hour}
              options={HOUR_OPTIONS}
              onChange={(v) => {
                setHour(v);
                apply(freq, v, minute, dow);
              }}
            />
          </div>
        )}
        <div>
          <label className="text-xs text-muted mb-1 block">Minuto</label>
          <Select
            ariaLabel="Minuto"
            value={minute}
            options={MINUTE_OPTIONS}
            onChange={(v) => {
              setMinute(v);
              apply(freq, hour, v, dow);
            }}
          />
        </div>
        {freq === "weekly" && (
          <div className="col-span-2">
            <label className="text-xs text-muted mb-1 block">
              Giorno della settimana
            </label>
            <Select
              ariaLabel="Giorno della settimana"
              value={dow}
              options={DOW_OPTIONS}
              onChange={(v) => {
                setDow(v);
                apply(freq, hour, minute, v);
              }}
            />
          </div>
        )}
      </div>
    </div>
  );
}
