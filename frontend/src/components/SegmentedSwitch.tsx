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
  pendingValue?: T | null;
  value: T | null;
  onChange?: (value: T) => void;
}

export function SegmentedSwitch<T extends string>({
  ariaLabel,
  className,
  disabled,
  onChange,
  options,
  pendingValue,
  value,
}: SegmentedSwitchProps<T>) {
  return (
    <div
      className={cx(
        "grid rounded-panel bg-panel p-1 shadow-control",
        className,
      )}
      role="group"
      aria-label={ariaLabel}
    >
      {options.map((option) => {
        const active = option.value === value;
        const pending = !active && option.value === pendingValue;
        return (
          <button
            key={option.value}
            type="button"
            className={cx(
              "focus-ring min-h-8 rounded-control px-3 py-1.5 text-sm font-semibold transition-[box-shadow,background-color,color,transform] duration-150 ease-out active:scale-[0.96]",
              active
                ? "bg-ink text-white shadow-raised"
                : pending
                  ? "bg-slate-200/80 text-slate-500 shadow-control"
                  : "text-slate-600 hover:bg-surface",
              option.description && "text-left",
            )}
            disabled={disabled || option.disabled}
            aria-pressed={active}
            aria-busy={pending || undefined}
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
