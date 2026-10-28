import { Toggle as DSToggle } from "@suite/design-system";

/**
 * Thin adapter over the design-system Toggle (Radix Switch).
 * Maps legacy onChange(boolean) → onCheckedChange(boolean).
 * `label` is the accessible name (the switch has no visible text of its own).
 */
export function Toggle({ checked, onChange, disabled, label }) {
  return (
    <DSToggle
      checked={checked}
      onCheckedChange={onChange}
      disabled={disabled}
      aria-label={label}
    />
  );
}
