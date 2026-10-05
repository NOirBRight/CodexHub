import { cx } from "../lib/format";

export interface SegmentedOption<T extends string> {
  value: T;
  label: string;
  description?: string;
  disabled?: boolean;
}

interface SegmentedSwitchProps<T extends string> {
  activeTone?: "default" | "foreign";
  ariaLabel: string;
  className?: string;
  disabled?: boolean;
  options: Array<SegmentedOption<T>>;
  pendingValue?: T | null;
  value: T | null;
  onChange?: (value: T) => void;
}

export function SegmentedSwitch<T extends string>({
  activeTone = "default",
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
        "ws-segmented grid rounded-panel bg-panel p-1 shadow-control",
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
                ? activeTone === "foreign"
                  ? "bg-panel text-muted shadow-control"
                  : "bg-action text-on-action shadow-raised"
                : pending
                  ? "bg-line/80 text-muted shadow-control"
                  : "text-muted hover:bg-surface",
              option.description && "text-left",
            )}
            disabled={disabled || option.disabled}
            aria-pressed={active}
            aria-busy={pending || undefined}
            onClick={() => onChange?.(option.value)}
          >
            <span className="block truncate">{option.label}</span>
            {option.description && (
              <span className={cx("block truncate text-[11px] font-medium", active && activeTone !== "foreign" ? "text-on-action/70" : "text-muted")}>
                {option.description}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}
