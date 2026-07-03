import { cx } from "../lib/format";

export interface SegmentedOption<T extends string> {
  value: T;
  label: string;
  description?: string;
  disabled?: boolean;
}

interface SegmentedSwitchProps<T extends string> {
  ariaLabel: string;
  className?: string;
  disabled?: boolean;
  options: Array<SegmentedOption<T>>;
  value: T | null;
  onChange?: (value: T) => void;
}

export function SegmentedSwitch<T extends string>({
  ariaLabel,
  className,
  disabled,
  onChange,
  options,
  value,
}: SegmentedSwitchProps<T>) {
  return (
    <div
      className={cx(
        "grid rounded-md border border-line bg-panel p-1 shadow-subtle",
        className,
      )}
      role="group"
      aria-label={ariaLabel}
    >
      {options.map((option) => {
        const active = option.value === value;
        return (
          <button
            key={option.value}
            type="button"
            className={cx(
              "focus-ring min-h-8 rounded px-3 py-1.5 text-sm font-semibold transition-colors",
              active ? "bg-ink text-white shadow-subtle" : "text-slate-600 hover:bg-white",
              option.description && "text-left",
            )}
            disabled={disabled || option.disabled}
            aria-pressed={active}
            onClick={() => onChange?.(option.value)}
          >
            <span className="block truncate">{option.label}</span>
            {option.description && (
              <span className={cx("block truncate text-[11px] font-medium", active ? "text-white/70" : "text-slate-400")}>
                {option.description}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}
