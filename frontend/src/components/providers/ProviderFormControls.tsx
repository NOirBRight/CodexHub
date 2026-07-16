import { Eye, EyeOff } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { cx } from "../../lib/format";

export function HeaderRow({
  actions,
  subtitle,
  title,
  titleAccessory,
}: {
  actions?: React.ReactNode;
  subtitle?: string;
  title: string;
  titleAccessory?: React.ReactNode;
}) {
  return (
    <div className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-3">
      <div className="min-w-0">
        <div className="flex min-w-0 items-center gap-2">
          <h2 className="min-w-0 truncate text-base font-semibold">{title}</h2>
          {titleAccessory && <div className="shrink-0">{titleAccessory}</div>}
        </div>
        {subtitle && <p className="mt-1 truncate text-sm text-slate-500">{subtitle}</p>}
      </div>
      {actions && <div className="flex shrink-0 flex-nowrap items-center gap-2 whitespace-nowrap">{actions}</div>}
    </div>
  );
}

export function Field({
  children,
  className,
  label,
}: {
  children: React.ReactNode;
  className?: string;
  label: string;
}) {
  return (
    <label className={cx("grid gap-1 text-sm font-medium text-slate-700", className)}>
      {label}
      {children}
    </label>
  );
}

export function ApiKeyInput({
  onChange,
  value,
}: {
  onChange: (value: string) => void;
  value: string;
}) {
  const { t } = useTranslation();
  const [visible, setVisible] = useState(false);

  return (
    <div className="relative">
      <input
        className="field field-compact pr-10"
        type={visible ? "text" : "password"}
        autoComplete="off"
        spellCheck={false}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
      <button
        type="button"
        className="focus-ring absolute right-1 top-1 grid h-7 w-7 place-items-center rounded-md text-slate-500 hover:bg-panel hover:text-ink"
        onClick={() => setVisible((current) => !current)}
        title={visible ? t("common.hideApiKey") : t("common.showApiKey")}
        aria-label={visible ? t("common.hideApiKey") : t("common.showApiKey")}
      >
        {visible ? <EyeOff size={15} /> : <Eye size={15} />}
      </button>
    </div>
  );
}

export function IconButton({
  children,
  danger,
  disabled,
  onClick,
  title,
}: {
  children: React.ReactNode;
  danger?: boolean;
  disabled?: boolean;
  onClick: () => void;
  title: string;
}) {
  return (
    <button
      type="button"
      className={cx(
        "focus-ring grid h-9 w-9 place-items-center rounded-md border bg-panel",
        danger ? "border-danger/40 bg-red-50 text-danger" : "border-line text-ink hover:bg-slate-100",
      )}
      disabled={disabled}
      onClick={onClick}
      title={title}
    >
      {children}
    </button>
  );
}
