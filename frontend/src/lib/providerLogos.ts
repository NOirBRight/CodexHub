import type { ReactNode } from "react";
import { createElement } from "react";
import ollamaLogo from "../assets/providers/ollama.svg";
import volcengineLogo from "../assets/providers/volcengine.svg";
import minimaxLogo from "../assets/providers/minimax.svg";
import kimiLogo from "../assets/providers/kimi.svg";
import xaiLogo from "../assets/providers/xai.svg";
import commandcodeLogo from "../assets/providers/commandcode.svg";
import opencodeLogo from "../assets/providers/opencode.svg";

const PROVIDER_LOGOS: Record<string, string> = {
  "ollama-cloud": ollamaLogo,
  volc: volcengineLogo,
  "minimax-cn": minimaxLogo,
  kimi: kimiLogo,
  "kimi-cn": kimiLogo,
  xai: xaiLogo,
  commandcode: commandcodeLogo,
  "opencode-go": opencodeLogo,
};

export function providerLogoSrc(providerId: string): string | null {
  return PROVIDER_LOGOS[providerId] ?? null;
}

export function ProviderLogo({ providerId }: { providerId: string }): ReactNode {
  const src = providerLogoSrc(providerId);
  if (!src) return null;
  return createElement(
    "span",
    { className: "grid h-6 w-6 shrink-0 place-items-center overflow-hidden" },
    createElement("img", {
      src,
      alt: "",
      className: "max-h-5 max-w-5 object-contain",
      "aria-hidden": "true",
    }),
  );
}
