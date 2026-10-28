import {
  Select as DSSelect,
  SelectTrigger,
  SelectValue,
  SelectContent,
  SelectItem,
} from "@suite/design-system";

/**
 * Thin adapter over the design-system Select.
 * Accepts the legacy API (options[], onChange, value, placeholder)
 * and forwards to the Radix-based DS component.
 */
export function Select({
  value,
  options,
  onChange,
  placeholder = "Seleziona…",
  id,
  ariaLabel,
}) {
  // DS uses string values; coerce numeric values to string for comparison.
  const strValue = value !== undefined && value !== null ? String(value) : "";

  return (
    <DSSelect
      value={strValue}
      onValueChange={(v) => {
        // Radix only emits values of real SelectItems, so a match always
        // exists; pass back the original typed value (preserves numbers).
        const match = options.find((o) => String(o.value) === v);
        if (match) onChange(match.value);
      }}
    >
      <SelectTrigger id={id} aria-label={ariaLabel}>
        <SelectValue placeholder={placeholder} />
      </SelectTrigger>
      <SelectContent>
        {options.map((o) => (
          <SelectItem key={o.value} value={String(o.value)}>
            {o.label}
          </SelectItem>
        ))}
      </SelectContent>
    </DSSelect>
  );
}
