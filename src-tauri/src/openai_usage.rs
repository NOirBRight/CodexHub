use crate::config;
use serde::{Deserialize, Deserializer, Serialize};
use serde_json::{json, Map, Value};
use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{SystemTime, UNIX_EPOCH};

const CODEX_ACCOUNT_USAGE_METHOD: &str = "account/usage/read";
const CACHE_REFRESH_INTERVAL_SECONDS: u64 = 12 * 60 * 60;
const DAY_SECONDS: u64 = 86_400;
const DEFAULT_WINDOW_DAYS: u64 = 365;
const RATE_LIMIT_LOG_FILE_LIMIT: usize = 64;
const RATE_LIMIT_LOG_MAX_BYTES: u64 = 8 * 1024 * 1024;
const USAGE_REFRESH_MAX_ATTEMPTS: usize = 3;

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct OpenAiUsageSnapshot {
    pub start_time: u64,
    pub end_time: u64,
    pub total_tokens: u64,
    pub input_tokens: u64,
    pub output_tokens: u64,
    pub input_cached_tokens: u64,
    pub num_model_requests: u64,
    pub peak_daily_tokens: Option<u64>,
    pub longest_running_turn_sec: Option<u64>,
    pub current_streak_days: Option<u64>,
    pub longest_streak_days: Option<u64>,
    pub limits: Vec<OpenAiUsageLimit>,
    pub buckets: Vec<OpenAiUsageBucket>,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct OpenAiUsageLimit {
    pub key: String,
    pub name: String,
    pub period: String,
    pub limit: Option<f64>,
    pub used: Option<f64>,
    pub remaining: Option<f64>,
    pub resets_at: Option<String>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct OpenAiUsageBucket {
    pub date: String,
    pub start_time: u64,
    pub end_time: u64,
    pub total_tokens: u64,
    pub input_tokens: u64,
    pub output_tokens: u64,
    pub input_cached_tokens: u64,
    pub num_model_requests: u64,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
struct CodexAccountUsageResponse {
    daily_usage_buckets: Option<Vec<CodexAccountUsageDailyBucket>>,
    #[serde(
        default,
        alias = "limits",
        alias = "usage_limits",
        alias = "subscriptionLimits",
        alias = "subscription_limits",
        alias = "rateLimits",
        alias = "rate_limits",
        alias = "limitStatus",
        alias = "limit_status",
        deserialize_with = "deserialize_optional_usage_limits"
    )]
    usage_limits: Option<Vec<CodexAccountUsageLimit>>,
    summary: CodexAccountUsageSummary,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
struct CodexAccountUsageDailyBucket {
    start_date: String,
    tokens: u64,
}

#[derive(Debug, Clone, Default, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
struct CodexAccountUsageSummary {
    current_streak_days: Option<u64>,
    lifetime_tokens: Option<u64>,
    longest_running_turn_sec: Option<u64>,
    longest_streak_days: Option<u64>,
    peak_daily_tokens: Option<u64>,
    #[serde(
        default,
        alias = "limits",
        alias = "usage_limits",
        alias = "usageLimits",
        alias = "subscriptionLimits",
        alias = "subscription_limits",
        alias = "rateLimits",
        alias = "rate_limits",
        alias = "limitStatus",
        alias = "limit_status",
        deserialize_with = "deserialize_optional_usage_limits"
    )]
    usage_limits: Option<Vec<CodexAccountUsageLimit>>,
}

#[derive(Debug, Clone, Default, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
struct CodexAccountUsageLimit {
    #[serde(default, deserialize_with = "deserialize_optional_stringish")]
    id: Option<String>,
    #[serde(
        default,
        alias = "type",
        alias = "kind",
        alias = "window",
        deserialize_with = "deserialize_optional_stringish"
    )]
    period: Option<String>,
    #[serde(
        default,
        alias = "label",
        alias = "title",
        alias = "periodName",
        alias = "displayName",
        deserialize_with = "deserialize_optional_stringish"
    )]
    name: Option<String>,
    #[serde(
        default,
        alias = "cap",
        alias = "max",
        alias = "maximum",
        alias = "total",
        deserialize_with = "deserialize_optional_f64"
    )]
    limit: Option<f64>,
    #[serde(
        default,
        alias = "usage",
        alias = "current",
        alias = "consumed",
        alias = "usedAmount",
        deserialize_with = "deserialize_optional_f64"
    )]
    used: Option<f64>,
    #[serde(
        default,
        alias = "available",
        alias = "remainingAmount",
        deserialize_with = "deserialize_optional_f64"
    )]
    remaining: Option<f64>,
    #[serde(
        default,
        alias = "resetAt",
        alias = "expiresAt",
        alias = "endsAt",
        alias = "endTime",
        alias = "periodEnd",
        alias = "periodEndTime",
        deserialize_with = "deserialize_optional_stringish"
    )]
    resets_at: Option<String>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
struct CodexAccountUsageCache {
    fetched_at: u64,
    usage: CodexAccountUsageResponse,
}

#[derive(Debug, Deserialize)]
struct CodexAppServerResponse {
    id: Option<Value>,
    result: Option<Value>,
    error: Option<CodexAppServerError>,
}

#[derive(Debug, Deserialize)]
struct CodexAppServerError {
    message: Option<String>,
}

pub fn openai_usage_completions(
    start_time: Option<u64>,
    end_time: Option<u64>,
    force_refresh: Option<bool>,
) -> Result<OpenAiUsageSnapshot, String> {
    let (start_time, end_time) = usage_window(start_time, end_time)?;
    let paths = config::ConfigPaths::runtime()?;
    let cache_path = openai_usage_cache_path(&paths);
    openai_usage_completions_with_cache_and_rate_limit_dir(
        start_time,
        end_time,
        force_refresh.unwrap_or(false),
        &cache_path,
        Some(paths.codex_dir()),
        current_unix_time(),
        read_codex_account_usage,
    )
}

#[cfg(test)]
fn openai_usage_completions_with_cache<F>(
    start_time: u64,
    end_time: u64,
    force_refresh: bool,
    cache_path: &Path,
    now: u64,
    fetch_usage: F,
) -> Result<OpenAiUsageSnapshot, String>
where
    F: FnMut() -> Result<CodexAccountUsageResponse, String>,
{
    openai_usage_completions_with_cache_and_rate_limit_dir(
        start_time,
        end_time,
        force_refresh,
        cache_path,
        None,
        now,
        fetch_usage,
    )
}

fn openai_usage_completions_with_cache_and_rate_limit_dir<F>(
    start_time: u64,
    end_time: u64,
    force_refresh: bool,
    cache_path: &Path,
    rate_limit_dir: Option<&Path>,
    now: u64,
    mut fetch_usage: F,
) -> Result<OpenAiUsageSnapshot, String>
where
    F: FnMut() -> Result<CodexAccountUsageResponse, String>,
{
    let cached = read_usage_cache(cache_path).ok();
    let should_refresh = force_refresh
        || cached
            .as_ref()
            .map(|cache| now.saturating_sub(cache.fetched_at) >= CACHE_REFRESH_INTERVAL_SECONDS)
            .unwrap_or(true);
    let mut usage = if should_refresh {
        match read_codex_account_usage_with_retries(&mut fetch_usage) {
            Ok(usage) => {
                let cache = CodexAccountUsageCache {
                    fetched_at: now,
                    usage: usage.clone(),
                };
                let _ = write_usage_cache(cache_path, &cache);
                usage
            }
            Err(error) => cached.map(|cache| cache.usage).ok_or(error)?,
        }
    } else {
        cached
            .map(|cache| cache.usage)
            .ok_or_else(|| "OpenAI usage cache was unexpectedly unavailable.".to_string())?
    };
    enrich_usage_with_local_rate_limits(&mut usage, rate_limit_dir);
    snapshot_from_codex_account_usage(start_time, end_time, usage)
}

fn openai_usage_cache_path(paths: &config::ConfigPaths) -> PathBuf {
    paths.proxy_dir().join("openai-usage-cache.json")
}

fn read_usage_cache(path: &Path) -> Result<CodexAccountUsageCache, String> {
    let body = fs::read_to_string(path)
        .map_err(|error| format!("Failed to read OpenAI usage cache: {error}"))?;
    serde_json::from_str(&body)
        .map_err(|error| format!("Failed to parse OpenAI usage cache: {error}"))
}

fn write_usage_cache(path: &Path, cache: &CodexAccountUsageCache) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .map_err(|error| format!("Failed to create OpenAI usage cache directory: {error}"))?;
    }
    let body = serde_json::to_string(cache)
        .map_err(|error| format!("Failed to encode OpenAI usage cache: {error}"))?;
    fs::write(path, body).map_err(|error| format!("Failed to write OpenAI usage cache: {error}"))
}

fn enrich_usage_with_local_rate_limits(
    usage: &mut CodexAccountUsageResponse,
    rate_limit_dir: Option<&Path>,
) {
    if response_has_usage_limits(usage) {
        return;
    }
    let Some(codex_dir) = rate_limit_dir else {
        return;
    };
    if let Some(limits) = latest_local_rate_limit_usage_limits(codex_dir) {
        usage.usage_limits = Some(limits);
    }
}

fn response_has_usage_limits(usage: &CodexAccountUsageResponse) -> bool {
    usage
        .usage_limits
        .as_ref()
        .is_some_and(|limits| !limits.is_empty())
        || usage
            .summary
            .usage_limits
            .as_ref()
            .is_some_and(|limits| !limits.is_empty())
}

#[derive(Debug)]
struct RateLimitLogFile {
    path: PathBuf,
    modified: SystemTime,
    len: u64,
}

fn latest_local_rate_limit_usage_limits(codex_dir: &Path) -> Option<Vec<CodexAccountUsageLimit>> {
    let mut files = Vec::new();
    collect_rate_limit_log_files(&codex_dir.join("sessions"), &mut files);
    collect_rate_limit_log_files(&codex_dir.join("archived_sessions"), &mut files);
    files.sort_by(|left, right| right.modified.cmp(&left.modified));
    files.truncate(RATE_LIMIT_LOG_FILE_LIMIT);

    let mut latest: Option<(String, Vec<CodexAccountUsageLimit>)> = None;
    for file in files {
        if file.len > RATE_LIMIT_LOG_MAX_BYTES {
            continue;
        }
        let Ok(body) = fs::read_to_string(&file.path) else {
            continue;
        };
        for line in body.lines() {
            if !line.contains("\"rate_limits\"") || line.contains("\"rate_limits\":null") {
                continue;
            }
            let Ok(value) = serde_json::from_str::<Value>(line) else {
                continue;
            };
            let Some(rate_limits) = value.pointer("/payload/rate_limits") else {
                continue;
            };
            if rate_limits.is_null() {
                continue;
            }
            let limits = json_value_as_usage_limits(rate_limits.clone());
            if limits.is_empty() {
                continue;
            }
            let timestamp = value
                .get("timestamp")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string();
            if latest
                .as_ref()
                .map(|(seen_timestamp, _)| timestamp > *seen_timestamp)
                .unwrap_or(true)
            {
                latest = Some((timestamp, limits));
            }
        }
    }
    latest.map(|(_, limits)| limits)
}

fn collect_rate_limit_log_files(root: &Path, files: &mut Vec<RateLimitLogFile>) {
    let Ok(entries) = fs::read_dir(root) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        let Ok(metadata) = entry.metadata() else {
            continue;
        };
        if metadata.is_dir() {
            collect_rate_limit_log_files(&path, files);
            continue;
        }
        if !metadata.is_file() || path.extension().and_then(|value| value.to_str()) != Some("jsonl") {
            continue;
        }
        files.push(RateLimitLogFile {
            path,
            modified: metadata.modified().unwrap_or(UNIX_EPOCH),
            len: metadata.len(),
        });
    }
}

fn read_codex_account_usage_with_retries<F>(
    fetch_usage: &mut F,
) -> Result<CodexAccountUsageResponse, String>
where
    F: FnMut() -> Result<CodexAccountUsageResponse, String>,
{
    let mut last_error = "Codex account usage is temporarily unavailable.".to_string();
    for _ in 0..USAGE_REFRESH_MAX_ATTEMPTS {
        match fetch_usage() {
            Ok(usage) => return Ok(usage),
            Err(error) => last_error = error,
        }
    }
    Err(last_error)
}

fn read_codex_account_usage() -> Result<CodexAccountUsageResponse, String> {
    let codex = find_codex_executable()?;
    let mut child = Command::new(&codex)
        .args(["app-server", "--stdio"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|error| {
            format!("Failed to start codex app-server for Codex account usage: {error}")
        })?;

    let mut stdin = child
        .stdin
        .take()
        .ok_or_else(|| "Failed to open codex app-server stdin.".to_string())?;
    write_json_line(
        &mut stdin,
        &json!({
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {
                    "name": "codexhub",
                    "title": "CodexHub",
                    "version": env!("CARGO_PKG_VERSION")
                },
                "capabilities": {
                    "experimentalApi": true,
                    "requestAttestation": false,
                    "optOutNotificationMethods": []
                }
            }
        }),
    )?;
    write_json_line(&mut stdin, &json!({ "method": "initialized" }))?;
    write_json_line(
        &mut stdin,
        &json!({
            "id": 2,
            "method": CODEX_ACCOUNT_USAGE_METHOD
        }),
    )?;
    stdin
        .flush()
        .map_err(|error| format!("Failed to flush codex app-server usage request: {error}"))?;

    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "Failed to open codex app-server stdout.".to_string())?;
    let mut reader = BufReader::new(stdout);
    let mut line = String::new();
    loop {
        line.clear();
        let bytes = reader
            .read_line(&mut line)
            .map_err(|error| format!("Failed to read codex app-server usage response: {error}"))?;
        if bytes == 0 {
            let _ = child.wait();
            return Err("Codex account usage did not return a response.".to_string());
        }
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        let message: CodexAppServerResponse = match serde_json::from_str(trimmed) {
            Ok(message) => message,
            Err(_) => continue,
        };
        if message.id != Some(json!(2)) {
            continue;
        }
        let _ = child.kill();
        let _ = child.wait();
        if let Some(error) = message.error {
            return Err(codex_app_server_error_message(
                error.message.as_deref().unwrap_or("request failed"),
            ));
        }
        let result = message
            .result
            .ok_or_else(|| "Codex account usage response did not include a result.".to_string())?;
        return serde_json::from_value(result)
            .map_err(|error| format!("Codex account usage response had unexpected JSON: {error}"));
    }
}

fn write_json_line(stdin: &mut impl Write, value: &Value) -> Result<(), String> {
    serde_json::to_writer(&mut *stdin, value)
        .map_err(|error| format!("Failed to encode codex app-server request: {error}"))?;
    stdin
        .write_all(b"\n")
        .map_err(|error| format!("Failed to write codex app-server request: {error}"))
}

fn find_codex_executable() -> Result<PathBuf, String> {
    if let Some(path) = std::env::var_os("CODEXHUB_CODEX_PATH")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
    {
        return Ok(path);
    }
    if let Some(path) = npm_codex_vendor_exe() {
        return Ok(path);
    }
    for candidate in codex_executable_candidates() {
        if let Ok(path) = which::which(candidate) {
            return Ok(path);
        }
    }
    Err("Codex account usage requires the Codex CLI to be installed and on PATH.".to_string())
}

fn npm_codex_vendor_exe() -> Option<PathBuf> {
    let appdata = std::env::var_os("APPDATA")?;
    let path = PathBuf::from(appdata)
        .join("npm")
        .join("node_modules")
        .join("@openai")
        .join("codex")
        .join("node_modules")
        .join("@openai")
        .join("codex-win32-x64")
        .join("vendor")
        .join("x86_64-pc-windows-msvc")
        .join("bin")
        .join("codex.exe");
    path.is_file().then_some(path)
}

fn codex_executable_candidates() -> Vec<&'static str> {
    vec!["codex.cmd", "codex", "codex.exe"]
}

fn snapshot_from_codex_account_usage(
    start_time: u64,
    end_time: u64,
    response: CodexAccountUsageResponse,
) -> Result<OpenAiUsageSnapshot, String> {
    let mut buckets = Vec::new();
    for bucket in response.daily_usage_buckets.unwrap_or_default() {
        let bucket_start = parse_utc_date_start(&bucket.start_date)?;
        let bucket_end = bucket_start + DAY_SECONDS;
        let filter_start = start_time.saturating_sub(DAY_SECONDS);
        let filter_end = end_time.saturating_add(DAY_SECONDS);
        if bucket_end <= filter_start || bucket_start >= filter_end {
            continue;
        }
        buckets.push(OpenAiUsageBucket {
            date: bucket.start_date,
            start_time: bucket_start,
            end_time: bucket_end,
            total_tokens: bucket.tokens,
            input_tokens: 0,
            output_tokens: 0,
            input_cached_tokens: 0,
            num_model_requests: 0,
        });
    }
    buckets.sort_by_key(|bucket| bucket.start_time);
    let bucket_tokens = buckets.iter().map(|bucket| bucket.total_tokens).sum();
    let summary = response.summary;
    let limits = response.usage_limits.or_else(|| summary.usage_limits.clone());
    Ok(OpenAiUsageSnapshot {
        start_time,
        end_time,
        total_tokens: summary.lifetime_tokens.unwrap_or(bucket_tokens),
        input_tokens: 0,
        output_tokens: 0,
        input_cached_tokens: 0,
        num_model_requests: 0,
        peak_daily_tokens: summary.peak_daily_tokens,
        longest_running_turn_sec: summary.longest_running_turn_sec,
        current_streak_days: summary.current_streak_days,
        longest_streak_days: summary.longest_streak_days,
        limits: codex_usage_limits(limits),
        buckets,
    })
}

fn codex_usage_limits(limits: Option<Vec<CodexAccountUsageLimit>>) -> Vec<OpenAiUsageLimit> {
    limits
        .unwrap_or_default()
        .into_iter()
        .enumerate()
        .filter_map(|(index, limit)| {
            if limit.limit.is_none()
                && limit.used.is_none()
                && limit.remaining.is_none()
                && limit.resets_at.is_none()
            {
                return None;
            }
            let fallback = format!("limit-{}", index + 1);
            let period = limit
                .period
                .clone()
                .or_else(|| limit.id.clone())
                .or_else(|| limit.name.clone())
                .unwrap_or_else(|| fallback.clone());
            let key = limit.id.clone().unwrap_or_else(|| period.clone());
            let name = limit
                .name
                .clone()
                .unwrap_or_else(|| default_usage_limit_name(&period));
            Some(OpenAiUsageLimit {
                key,
                name,
                period,
                limit: limit.limit,
                used: limit.used,
                remaining: limit.remaining,
                resets_at: limit.resets_at,
            })
        })
        .collect()
}

fn default_usage_limit_name(period: &str) -> String {
    let normalized = period.trim().to_ascii_lowercase();
    if (normalized.contains('5') || normalized.contains("five"))
        && (normalized.contains('h') || normalized.contains("hour"))
    {
        return "5 hours".to_string();
    }
    if normalized.contains("week") {
        return "Weekly".to_string();
    }
    period.trim().to_string()
}

fn codex_app_server_error_message(message: &str) -> String {
    let lower = message.to_ascii_lowercase();
    if lower.contains("login") || lower.contains("auth") || lower.contains("unauthorized") {
        return "Codex account usage is unavailable because Codex is not signed in.".to_string();
    }
    "Codex account usage is temporarily unavailable.".to_string()
}

fn usage_window(start_time: Option<u64>, end_time: Option<u64>) -> Result<(u64, u64), String> {
    let resolved_end_time = end_time.unwrap_or_else(current_unix_time);
    let resolved_start_time = start_time
        .unwrap_or_else(|| resolved_end_time.saturating_sub(DEFAULT_WINDOW_DAYS * DAY_SECONDS));
    if resolved_start_time >= resolved_end_time {
        return Err("OpenAI usage start_time must be before end_time".to_string());
    }
    Ok((resolved_start_time, resolved_end_time))
}

fn current_unix_time() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}

fn deserialize_optional_f64<'de, D>(deserializer: D) -> Result<Option<f64>, D::Error>
where
    D: Deserializer<'de>,
{
    let value = Option::<Value>::deserialize(deserializer)?;
    Ok(value.and_then(json_value_as_f64))
}

fn deserialize_optional_stringish<'de, D>(deserializer: D) -> Result<Option<String>, D::Error>
where
    D: Deserializer<'de>,
{
    let value = Option::<Value>::deserialize(deserializer)?;
    Ok(value.and_then(json_value_as_string))
}

fn deserialize_optional_usage_limits<'de, D>(
    deserializer: D,
) -> Result<Option<Vec<CodexAccountUsageLimit>>, D::Error>
where
    D: Deserializer<'de>,
{
    let value = Option::<Value>::deserialize(deserializer)?;
    let limits = value.map(json_value_as_usage_limits).unwrap_or_default();
    Ok((!limits.is_empty()).then_some(limits))
}

fn json_value_as_usage_limits(value: Value) -> Vec<CodexAccountUsageLimit> {
    match value {
        Value::Array(items) => items
            .into_iter()
            .filter_map(|item| decode_usage_limit_value(item, None))
            .collect(),
        Value::Object(mut map) => {
            for key in [
                "usageLimits",
                "usage_limits",
                "limits",
                "data",
                "items",
                "subscriptionLimits",
                "rateLimits",
                "limitStatus",
            ] {
                if let Some(nested) = map.remove(key) {
                    let limits = json_value_as_usage_limits(nested);
                    if !limits.is_empty() {
                        return limits;
                    }
                }
            }

            let mut limits = Vec::new();
            for (key, item) in map {
                match item {
                    Value::Array(items) => {
                        limits.extend(
                            items
                                .into_iter()
                                .filter_map(|entry| decode_usage_limit_value(entry, Some(&key))),
                        );
                    }
                    value => {
                        if let Some(limit) = decode_usage_limit_value(value, Some(&key)) {
                            limits.push(limit);
                        }
                    }
                }
            }
            limits
        }
        _ => Vec::new(),
    }
}

fn decode_usage_limit_value(
    mut value: Value,
    fallback_key: Option<&str>,
) -> Option<CodexAccountUsageLimit> {
    if let Value::Object(map) = &mut value {
        normalize_usage_limit_object(map, fallback_key);
    }
    serde_json::from_value(value).ok()
}

fn normalize_usage_limit_object(map: &mut Map<String, Value>, fallback_key: Option<&str>) {
    if let Some(key) = fallback_key {
        if !map.contains_key("id") {
            map.insert("id".to_string(), Value::String(key.to_string()));
        }
    }

    let window_minutes = object_f64(map, &["windowMinutes", "window_minutes"]);
    let normalized_period = window_minutes.and_then(rate_limit_period_from_window_minutes);
    if !contains_any(map, &["period", "type", "kind", "window"]) {
        let period = normalized_period
            .or(fallback_key)
            .map(str::to_string)
            .unwrap_or_else(|| "limit".to_string());
        map.insert("period".to_string(), Value::String(period));
    }
    if !contains_any(map, &["name", "label", "title", "periodName", "displayName"]) {
        if let Some(period) = normalized_period {
            map.insert(
                "name".to_string(),
                Value::String(default_usage_limit_name(period)),
            );
        }
    }

    if !contains_any(
        map,
        &[
            "resetsAt",
            "resetAt",
            "expiresAt",
            "endsAt",
            "endTime",
            "periodEnd",
            "periodEndTime",
        ],
    ) {
        if let Some(value) = object_value(map, &["resets_at", "reset_at", "resetAt"]) {
            map.insert("resetsAt".to_string(), value);
        }
    }

    let used_percent = object_f64(map, &["usedPercent", "used_percent"]);
    if let Some(used_percent) = used_percent {
        if !contains_any(map, &["used", "current", "consumed", "usedAmount"]) {
            map.insert("used".to_string(), json!(used_percent));
        }
        if !contains_any(map, &["limit", "cap", "max", "maximum", "total"]) {
            map.insert("limit".to_string(), json!(100.0));
        }
        if !contains_any(map, &["remaining", "available", "remainingAmount"]) {
            map.insert(
                "remaining".to_string(),
                json!((100.0 - used_percent).clamp(0.0, 100.0)),
            );
        }
    }
}

fn rate_limit_period_from_window_minutes(window_minutes: f64) -> Option<&'static str> {
    if (window_minutes - 300.0).abs() < f64::EPSILON {
        return Some("five_hours");
    }
    if (window_minutes - 10_080.0).abs() < f64::EPSILON {
        return Some("week");
    }
    None
}

fn contains_any(map: &Map<String, Value>, keys: &[&str]) -> bool {
    keys.iter().any(|key| map.contains_key(*key))
}

fn object_value(map: &Map<String, Value>, keys: &[&str]) -> Option<Value> {
    keys.iter().find_map(|key| map.get(*key).cloned())
}

fn object_f64(map: &Map<String, Value>, keys: &[&str]) -> Option<f64> {
    object_value(map, keys).and_then(json_value_as_f64)
}

fn json_value_as_f64(value: Value) -> Option<f64> {
    match value {
        Value::Number(number) => number.as_f64().filter(|value| value.is_finite()),
        Value::String(text) => text
            .trim()
            .trim_end_matches('%')
            .parse::<f64>()
            .ok()
            .filter(|value| value.is_finite()),
        _ => None,
    }
}

fn json_value_as_string(value: Value) -> Option<String> {
    match value {
        Value::String(text) => {
            let trimmed = text.trim();
            (!trimmed.is_empty()).then(|| trimmed.to_string())
        }
        Value::Number(number) => Some(number.to_string()),
        Value::Bool(value) => Some(value.to_string()),
        _ => None,
    }
}

fn parse_utc_date_start(value: &str) -> Result<u64, String> {
    let mut parts = value.split('-');
    let year = parts
        .next()
        .and_then(|part| part.parse::<i32>().ok())
        .ok_or_else(|| format!("Invalid Codex usage date: {value}"))?;
    let month = parts
        .next()
        .and_then(|part| part.parse::<u32>().ok())
        .ok_or_else(|| format!("Invalid Codex usage date: {value}"))?;
    let day = parts
        .next()
        .and_then(|part| part.parse::<u32>().ok())
        .ok_or_else(|| format!("Invalid Codex usage date: {value}"))?;
    if parts.next().is_some() || !valid_date(year, month, day) {
        return Err(format!("Invalid Codex usage date: {value}"));
    }
    Ok(days_from_civil(year, month, day) as u64 * DAY_SECONDS)
}

fn valid_date(year: i32, month: u32, day: u32) -> bool {
    if !(1..=12).contains(&month) {
        return false;
    }
    let max_day = match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if is_leap_year(year) => 29,
        2 => 28,
        _ => 0,
    };
    (1..=max_day).contains(&day)
}

fn is_leap_year(year: i32) -> bool {
    (year % 4 == 0 && year % 100 != 0) || year % 400 == 0
}

fn days_from_civil(year: i32, month: u32, day: u32) -> i64 {
    let year = year - i32::from(month <= 2);
    let era = if year >= 0 { year } else { year - 399 } / 400;
    let year_of_era = year - era * 400;
    let month = month as i32;
    let day_of_year = (153 * (month + if month > 2 { -3 } else { 9 }) + 2) / 5 + day as i32 - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    (era * 146_097 + day_of_era - 719_468) as i64
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::Cell;
    use std::fs;
    use std::path::PathBuf;

    #[test]
    fn codex_account_usage_response_maps_subscription_summary_and_daily_buckets() {
        let response: CodexAccountUsageResponse = serde_json::from_str(
            r#"{
              "summary": {
                "lifetimeTokens": 18072262610,
                "peakDailyTokens": 1123766916,
                "longestRunningTurnSec": 57442,
                "currentStreakDays": 16,
                "longestStreakDays": 20
              },
              "dailyUsageBuckets": [
                {"startDate": "2026-07-05", "tokens": 100},
                {"startDate": "2026-07-06", "tokens": 250}
              ]
            }"#,
        )
        .expect("codex usage response parses");

        let snapshot = snapshot_from_codex_account_usage(1_783_209_600, 1_783_382_400, response)
            .expect("codex usage maps");

        assert_eq!(snapshot.total_tokens, 18_072_262_610);
        assert_eq!(snapshot.peak_daily_tokens, Some(1_123_766_916));
        assert_eq!(snapshot.longest_running_turn_sec, Some(57_442));
        assert_eq!(snapshot.current_streak_days, Some(16));
        assert_eq!(snapshot.longest_streak_days, Some(20));
        assert_eq!(snapshot.num_model_requests, 0);
        assert_eq!(snapshot.buckets.len(), 2);
        assert_eq!(snapshot.buckets[0].date, "2026-07-05");
        assert_eq!(snapshot.buckets[0].start_time, 1_783_209_600);
        assert_eq!(snapshot.buckets[0].total_tokens, 100);
        assert_eq!(snapshot.buckets[1].date, "2026-07-06");
        assert_eq!(snapshot.buckets[1].total_tokens, 250);
    }

    #[test]
    fn codex_account_usage_response_maps_usage_limits() {
        let response: CodexAccountUsageResponse = serde_json::from_str(
            r#"{
              "summary": { "lifetimeTokens": 123 },
              "usageLimits": [
                {
                  "period": "five_hours",
                  "periodName": "5 hours",
                  "limit": 300,
                  "used": 120,
                  "remaining": 180,
                  "resetsAt": "2026-07-07T15:00:00Z"
                },
                {
                  "period": "week",
                  "name": "Weekly",
                  "limit": 1000,
                  "used": 250,
                  "remaining": 750,
                  "resetAt": "2026-07-13T00:00:00Z"
                }
              ]
            }"#,
        )
        .expect("codex usage response parses");

        let snapshot = snapshot_from_codex_account_usage(1_783_296_000, 1_783_382_400, response)
            .expect("codex usage maps");

        assert_eq!(snapshot.limits.len(), 2);
        assert_eq!(snapshot.limits[0].period, "five_hours");
        assert_eq!(snapshot.limits[0].name, "5 hours");
        assert_eq!(snapshot.limits[0].limit, Some(300.0));
        assert_eq!(snapshot.limits[0].used, Some(120.0));
        assert_eq!(snapshot.limits[0].remaining, Some(180.0));
        assert_eq!(
            snapshot.limits[0].resets_at.as_deref(),
            Some("2026-07-07T15:00:00Z"),
        );
        assert_eq!(snapshot.limits[1].period, "week");
        assert_eq!(
            snapshot.limits[1].resets_at.as_deref(),
            Some("2026-07-13T00:00:00Z"),
        );
    }

    #[test]
    fn codex_account_usage_response_maps_usage_limit_objects() {
        let response: CodexAccountUsageResponse = serde_json::from_str(
            r#"{
              "summary": { "lifetimeTokens": 123 },
              "usageLimits": {
                "five_hours": {
                  "limit": "300",
                  "used": "120",
                  "remaining": "180",
                  "resetsAt": 1783436400
                },
                "weekly": {
                  "limit": 1000,
                  "used": 250,
                  "remaining": 750,
                  "resetsAt": "2026-07-13T00:00:00Z"
                }
              }
            }"#,
        )
        .expect("codex usage object response parses");

        let snapshot = snapshot_from_codex_account_usage(1_783_296_000, 1_783_382_400, response)
            .expect("codex usage maps");

        assert_eq!(snapshot.limits.len(), 2);
        assert_eq!(snapshot.limits[0].period, "five_hours");
        assert_eq!(snapshot.limits[0].limit, Some(300.0));
        assert_eq!(
            snapshot.limits[0].resets_at.as_deref(),
            Some("1783436400"),
        );
        assert_eq!(snapshot.limits[1].period, "weekly");
    }

    #[test]
    fn codex_account_usage_response_maps_rate_limit_windows() {
        let response: CodexAccountUsageResponse = serde_json::from_str(
            r#"{
              "summary": { "lifetimeTokens": 123 },
              "rate_limits": {
                "limit_id": "codex",
                "primary": {
                  "used_percent": 26,
                  "window_minutes": 300,
                  "resets_at": 1783406493
                },
                "secondary": {
                  "used_percent": 4,
                  "window_minutes": 10080,
                  "resets_at": 1783993293
                }
              }
            }"#,
        )
        .expect("codex rate limits response parses");

        let snapshot = snapshot_from_codex_account_usage(1_783_296_000, 1_783_382_400, response)
            .expect("codex usage maps");

        assert_eq!(snapshot.limits.len(), 2);
        assert_eq!(snapshot.limits[0].period, "five_hours");
        assert_eq!(snapshot.limits[0].name, "5 hours");
        assert_eq!(snapshot.limits[0].limit, Some(100.0));
        assert_eq!(snapshot.limits[0].used, Some(26.0));
        assert_eq!(snapshot.limits[0].remaining, Some(74.0));
        assert_eq!(snapshot.limits[0].resets_at.as_deref(), Some("1783406493"));
        assert_eq!(snapshot.limits[1].period, "week");
        assert_eq!(snapshot.limits[1].name, "Weekly");
        assert_eq!(snapshot.limits[1].used, Some(4.0));
        assert_eq!(snapshot.limits[1].remaining, Some(96.0));
        assert_eq!(snapshot.limits[1].resets_at.as_deref(), Some("1783993293"));
    }

    #[test]
    fn local_codex_rate_limits_enrich_cached_account_usage() {
        let root = temp_root("openai-usage-local-rate-limits");
        let cache_path = root.join("proxy").join("usage-cache.json");
        let sessions_dir = root.join("sessions").join("2026").join("07").join("07");
        fs::create_dir_all(&sessions_dir).unwrap();
        fs::write(
            sessions_dir.join("rollout.jsonl"),
            r#"{"timestamp":"2026-07-07T03:55:58.964Z","type":"event_msg","payload":{"type":"token_count","rate_limits":{"limit_id":"codex","primary":{"used_percent":26,"window_minutes":300,"resets_at":1783406493},"secondary":{"used_percent":4,"window_minutes":10080,"resets_at":1783993293},"plan_type":"pro"}}}"#,
        )
        .unwrap();
        write_test_cache(
            &cache_path,
            10_000,
            r#"{
              "summary": { "lifetimeTokens": 41 },
              "dailyUsageBuckets": [
                {"startDate": "2026-07-06", "tokens": 41}
              ]
            }"#,
        );

        let snapshot = openai_usage_completions_with_cache_and_rate_limit_dir(
            1_783_296_000,
            1_783_382_400,
            false,
            &cache_path,
            Some(&root),
            10_000,
            || panic!("fresh cache should not refresh"),
        )
        .expect("fresh cached usage with local rate limits");

        assert_eq!(snapshot.total_tokens, 41);
        assert_eq!(snapshot.limits.len(), 2);
        assert_eq!(snapshot.limits[0].period, "five_hours");
        assert_eq!(snapshot.limits[0].used, Some(26.0));
        assert_eq!(snapshot.limits[1].period, "week");
        assert_eq!(snapshot.limits[1].remaining, Some(96.0));
    }

    #[test]
    fn stale_cache_is_returned_when_refresh_fails_after_three_attempts() {
        let root = temp_root("openai-usage-stale-cache");
        let cache_path = root.join("usage-cache.json");
        write_test_cache(
            &cache_path,
            1_000,
            r#"{
              "summary": { "lifetimeTokens": 99 },
              "dailyUsageBuckets": [
                {"startDate": "2026-07-06", "tokens": 99}
              ]
            }"#,
        );
        let attempts = Cell::new(0);

        let snapshot = openai_usage_completions_with_cache(
            1_783_296_000,
            1_783_382_400,
            false,
            &cache_path,
            1_000 + CACHE_REFRESH_INTERVAL_SECONDS + 1,
            || {
                attempts.set(attempts.get() + 1);
                Err("temporary outage".to_string())
            },
        )
        .expect("cached usage survives refresh failures");

        assert_eq!(attempts.get(), 3);
        assert_eq!(snapshot.total_tokens, 99);
        assert_eq!(snapshot.buckets[0].date, "2026-07-06");
    }

    #[test]
    fn fresh_cache_skips_refresh_until_twice_daily_window() {
        let root = temp_root("openai-usage-fresh-cache");
        let cache_path = root.join("usage-cache.json");
        write_test_cache(
            &cache_path,
            10_000,
            r#"{
              "summary": { "lifetimeTokens": 41 },
              "dailyUsageBuckets": [
                {"startDate": "2026-07-06", "tokens": 41}
              ]
            }"#,
        );

        let snapshot = openai_usage_completions_with_cache(
            1_783_296_000,
            1_783_382_400,
            false,
            &cache_path,
            10_000 + CACHE_REFRESH_INTERVAL_SECONDS - 1,
            || panic!("fresh cache should not refresh"),
        )
        .expect("fresh cached usage");

        assert_eq!(snapshot.total_tokens, 41);
    }

    #[test]
    fn force_refresh_updates_cache_before_ttl() {
        let root = temp_root("openai-usage-force-refresh");
        let cache_path = root.join("usage-cache.json");
        write_test_cache(
            &cache_path,
            10_000,
            r#"{
              "summary": { "lifetimeTokens": 41 },
              "dailyUsageBuckets": [
                {"startDate": "2026-07-06", "tokens": 41}
              ]
            }"#,
        );

        let snapshot = openai_usage_completions_with_cache(
            1_783_296_000,
            1_783_468_800,
            true,
            &cache_path,
            10_300,
            || {
                serde_json::from_str(
                    r#"{
                      "summary": { "lifetimeTokens": 84 },
                      "dailyUsageBuckets": [
                        {"startDate": "2026-07-07", "tokens": 84}
                      ]
                    }"#,
                )
                .map_err(|error| error.to_string())
            },
        )
        .expect("forced refresh");

        assert_eq!(snapshot.total_tokens, 84);
        assert_eq!(snapshot.buckets[0].date, "2026-07-07");
        let cache = fs::read_to_string(&cache_path).expect("cache written");
        assert!(cache.contains(r#""fetched_at":10300"#));
        assert!(cache.contains("2026-07-07"));
    }

    #[test]
    fn local_account_dates_are_not_filtered_out_by_utc_end_time() {
        let response: CodexAccountUsageResponse = serde_json::from_str(
            r#"{
              "summary": { "lifetimeTokens": 777 },
              "dailyUsageBuckets": [
                {"startDate": "2026-07-07", "tokens": 777}
              ]
            }"#,
        )
        .expect("codex usage response parses");

        let snapshot = snapshot_from_codex_account_usage(1_783_296_000, 1_783_353_600, response)
            .expect("local date maps");

        assert_eq!(snapshot.buckets.len(), 1);
        assert_eq!(snapshot.buckets[0].date, "2026-07-07");
        assert_eq!(snapshot.buckets[0].total_tokens, 777);
    }

    #[test]
    fn codex_usage_errors_do_not_reference_admin_api_keys() {
        let message = codex_app_server_error_message("auth failed with token secret");
        let admin_key_name = ["OPENAI", "ADMIN", "KEY"].join("_");

        assert!(message.contains("Codex account usage"));
        assert!(!message.contains(&admin_key_name));
        assert!(!message.contains("token secret"));
    }

    #[test]
    fn parse_utc_date_start_rejects_invalid_dates() {
        assert_eq!(parse_utc_date_start("1970-01-01").unwrap(), 0);
        assert!(parse_utc_date_start("2026-02-30").is_err());
        assert!(parse_utc_date_start("not-a-date").is_err());
    }

    #[test]
    fn codex_executable_candidates_put_shims_before_windows_app_alias() {
        let candidates = codex_executable_candidates();
        let cmd_position = candidates
            .iter()
            .position(|candidate| *candidate == "codex.cmd");
        let exe_position = candidates
            .iter()
            .position(|candidate| *candidate == "codex.exe");

        assert!(cmd_position < exe_position);
    }

    fn write_test_cache(path: &PathBuf, fetched_at: u64, usage_json: &str) {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).unwrap();
        }
        let usage: CodexAccountUsageResponse = serde_json::from_str(usage_json).unwrap();
        let cache = CodexAccountUsageCache { fetched_at, usage };
        fs::write(path, serde_json::to_string(&cache).unwrap()).unwrap();
    }

    fn temp_root(name: &str) -> PathBuf {
        let mut root = std::env::temp_dir();
        root.push(format!(
            "codexhub-{name}-{}",
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        root
    }
}
