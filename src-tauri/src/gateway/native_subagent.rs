use serde::{Deserialize, Serialize};

/// Client-native model identities and capabilities, independent of the Gateway catalog.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct NativeSubagentOption {
    pub id: String,
    pub label: String,
    pub efforts: Vec<String>,
    pub default_effort: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct NativeSubagentSettings {
    pub model: String,
    pub effort: String,
    pub native: bool,
    pub options: Vec<NativeSubagentOption>,
}
