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

export function isChineseLocale(value?: string | null) {
  const normalizedLocale = value?.trim().toLowerCase().replace(/_/g, "-");
  return normalizedLocale === "zh" || normalizedLocale?.startsWith("zh-") === true;
}

export function resolveLocale(value?: string | null): AppLocale {
  const normalized = value?.trim();
  if (normalized === "zh-CN" || normalized === "en-US") {
    return normalized;
  }
  if (isChineseLocale(normalized)) {
    return "zh-CN";
  }
  return DEFAULT_LOCALE;
}

export function browserLocale(): AppLocale {
  const primaryLanguage = globalThis.navigator?.language?.trim();
  const primaryLanguageFromList = globalThis.navigator?.languages?.find((value) => value?.trim());
  return resolveLocale(primaryLanguage || primaryLanguageFromList);
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
