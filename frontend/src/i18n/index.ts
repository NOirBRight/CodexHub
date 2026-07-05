import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import enUS from "./locales/en-US";
import zhCN from "./locales/zh-CN";

export const SUPPORTED_LOCALES = ["zh-CN", "en-US"] as const;
export type AppLocale = (typeof SUPPORTED_LOCALES)[number];

export const DEFAULT_LOCALE: AppLocale = "en-US";

export const localeResources = {
  "en-US": enUS,
  "zh-CN": zhCN,
} satisfies Record<AppLocale, typeof enUS>;

export function resolveLocale(value?: string | null): AppLocale {
  const normalized = value?.trim();
  if (normalized === "zh-CN" || normalized === "en-US") {
    return normalized;
  }
  if (normalized?.toLowerCase().startsWith("zh")) {
    return "zh-CN";
  }
  return DEFAULT_LOCALE;
}

export function browserLocale(): AppLocale {
  const candidates = [
    globalThis.navigator?.language,
    ...(globalThis.navigator?.languages ?? []),
  ];
  return resolveLocale(candidates.find(Boolean));
}

export async function changeAppLocale(locale: string | null | undefined) {
  const nextLocale = resolveLocale(locale);
  if (i18n.language !== nextLocale) {
    await i18n.changeLanguage(nextLocale);
  }
  return nextLocale;
}

void i18n.use(initReactI18next).init({
  fallbackLng: DEFAULT_LOCALE,
  interpolation: {
    escapeValue: false,
  },
  lng: browserLocale(),
  resources: Object.fromEntries(
    Object.entries(localeResources).map(([locale, translation]) => [
      locale,
      { translation },
    ]),
  ),
});

export default i18n;
