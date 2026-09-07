import { useState } from "react";
import {
  ChevronDown,
  Copy,
  Eye,
  EyeOff,
  RefreshCw,
  Search,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import type { GatewayStatus, Settings } from "../../lib/types";

export function GatewayConnectionPanel({
  draft,
  settings,
  status,
  onDraft,
  onCopy,
}: {
  draft: Settings;
  settings: Settings | null;
  status?: GatewayStatus | null;
  onDraft: (settings: Settings) => void;
  onCopy: (value: string) => Promise<void>;
}) {
  const { t } = useTranslation();
  const [showKey, setShowKey] = useState(false);
  const [query, setQuery] = useState("");
  const [source, setSource] = useState("");
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());
  const models = status?.official_models ?? [];
  const providerId = (model: (typeof models)[number]) =>
    model.source_kind === "official" ? "official" : model.id.split("/")[0];
  const sources = [...new Set(models.map(providerId))];
  const providerName = (id: string) => models.find((model) => providerId(model) === id)?.source ?? id;
  const filtered = models.filter(
    (model) =>
      (!source || providerId(model) === source) &&
      [model.id, model.display_name, model.source].some((value) =>
        value.toLowerCase().includes(query.trim().toLowerCase()),
      ),
  );
  const base =
    status?.endpoints.base_url ??
    `http://${settings?.gateway_bind_address || "127.0.0.1"}:${settings?.proxy_port ?? 9099}/v1`;
  const urls = [
    ["Models", status?.endpoints.models ?? `${base}/models`],
    ["Responses", status?.endpoints.responses ?? `${base}/responses`],
    [
      "Chat Completions",
      status?.endpoints.chat_completions ?? `${base}/chat/completions`,
    ],
  ];
  const copyButton = (value: string, label: string) => (
    <button
      type="button"
      className="ws-icon"
      aria-label={t("workspace.copyValue", { value: label })}
      onClick={() => void onCopy(value)}
    >
      <Copy size={14} />
    </button>
  );
  return (
    <section className="ws-connection-page">
      <div className="ws-connection-settings">
        <div className="ws-connection-fields">
          <label>
            {t("workspace.bindAddress")}
            <code>{draft.gateway_bind_address}</code>
          </label>
          <label>
            {t("common.port")}
            <input
              className="field"
              type="number"
              min={1024}
              max={65535}
              value={draft.proxy_port}
              onChange={(e) =>
                onDraft({ ...draft, proxy_port: Number(e.target.value) })
              }
            />
          </label>
          <label>
            {t("common.timeout")}
            <input
              className="field"
              type="number"
              min={5}
              max={600}
              value={draft.gateway_request_timeout_seconds}
              onChange={(e) =>
                onDraft({
                  ...draft,
                  gateway_request_timeout_seconds: Number(e.target.value),
                })
              }
            />
          </label>
          <div className="ws-connection-key">
            <label htmlFor="gateway-connection-key">{t("common.apiKey")}</label>
            <div className="ws-settings-key">
              <input
                id="gateway-connection-key"
                type={showKey ? "text" : "password"}
                autoComplete="off"
                value={draft.gateway_client_key}
                onChange={(e) =>
                  onDraft({ ...draft, gateway_client_key: e.target.value })
                }
              />
              <button
                type="button"
                className="ws-icon"
                aria-label={t(
                  showKey ? "common.hideApiKey" : "common.showApiKey",
                )}
                onClick={() => setShowKey(!showKey)}
              >
                {showKey ? <EyeOff size={14} /> : <Eye size={14} />}
              </button>
              {copyButton(
                draft.gateway_client_key,
                t("common.apiKey"),
              )}
              <button
                type="button"
                className="ws-icon"
                aria-label={t("gateway.regenerateApiKey")}
                onClick={() =>
                  onDraft({
                    ...draft,
                    gateway_client_key:
                      "ch-" + crypto.randomUUID().replace(/-/g, ""),
                  })
                }
              >
                <RefreshCw size={14} />
              </button>
            </div>
          </div>
        </div>
        <div className="ws-connection-endpoints">
          {urls.map(([label, url]) => (
            <div key={label}>
              <div>
                <span>{label}</span>
                <code title={url}>{url}</code>
              </div>
              {copyButton(url, label)}
            </div>
          ))}
        </div>
      </div>
      <div className="ws-connection-models">
        <header>
          <b>{t("workspace.availableModels")}</b>
          <span className="ws-muted">
            {filtered.length} / {models.length}
          </span>
          <div className="ws-connection-filter">
            <Search size={14} />
            <input
              aria-label={t("workspace.searchGatewayModels")}
              placeholder={t("workspace.searchGatewayModels")}
              value={query}
              onChange={(e) => {
                setQuery(e.target.value);
                setCollapsed(new Set());
              }}
            />
          </div>
          <select
            aria-label={t("workspace.filterProvider")}
            value={source}
            onChange={(e) => setSource(e.target.value)}
          >
            <option value="">{t("workspace.allProviders")}</option>
            {sources.map((value) => (
              <option key={value} value={value}>{providerName(value)} ({value})</option>
            ))}
          </select>
        </header>
        <div className="ws-connection-model-list">
          {sources.map((provider) => {
            const items = filtered.filter((model) => providerId(model) === provider);
            if (!items.length) return null;
            const expanded = !collapsed.has(provider);
            return (
              <section className="ws-connection-group" key={provider}>
                <button
                  type="button"
                  className="ws-connection-group-toggle"
                  aria-expanded={expanded}
                  onClick={() =>
                    setCollapsed((current) => {
                      const next = new Set(current);
                      if (next.has(provider)) next.delete(provider);
                      else next.add(provider);
                      return next;
                    })
                  }
                >
                  <ChevronDown
                    size={14}
                    className={expanded ? "" : "collapsed"}
                  />
                  <b>{providerName(provider)}</b>
                  <span>{provider}</span>
                  <span>{items.length}</span>
                </button>
                <div hidden={!expanded}>
                  {items.map((model) => (
                    <div className="ws-connection-model" key={model.id}>
                      <div>
                        <b>{model.display_name}</b>
                        <code>{model.id}</code>
                      </div>
                      <div className="ws-connection-formats">
                        {model.supports_responses && <span>Responses</span>}
                        {model.supports_chat_completions && <span>Chat</span>}
                      </div>
                      {copyButton(model.id, model.id)}
                    </div>
                  ))}
                </div>
              </section>
            );
          })}
          {!filtered.length && (
            <p className="ws-muted ws-connection-empty">
              {t(
                !status
                  ? "workspace.gatewayModelsUnavailable"
                  : models.length
                    ? "workspace.noMatchingModels"
                    : "workspace.noGatewayModels",
              )}
            </p>
          )}
        </div>
      </div>
    </section>
  );
}
