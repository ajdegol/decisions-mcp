use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use std::{env, fs, path::PathBuf};

pub const DEFAULT_BASE_URL: &str = "https://api.openai.com/v1";
const DEFAULT_MODEL: &str = "gpt-6-luna";

#[derive(Clone, Serialize)]
pub struct Settings {
    pub base_url: String,
    pub model: String,
    pub roots: Vec<PathBuf>,
    pub cache_path: PathBuf,
    pub cache_ttl: u64,
}

#[derive(Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct FileConfig {
    base_url: Option<String>,
    model: Option<String>,
    roots: Option<Vec<PathBuf>>,
    cache_path: Option<PathBuf>,
    cache_ttl: Option<u64>,
}

pub fn config_path() -> Result<PathBuf> {
    if let Some(path) = env::var_os("DECISIONS_CONFIG") {
        return Ok(PathBuf::from(path));
    }
    let base = env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .or_else(|| dirs::home_dir().map(|p| p.join(".config")))
        .context("Could not determine config directory; set DECISIONS_CONFIG")?;
    Ok(base.join("decisions/config.toml"))
}

fn read_config() -> Result<FileConfig> {
    let path = config_path()?;
    match fs::read_to_string(path) {
        Ok(text) => toml::from_str(&text).map_err(|_| {
            anyhow::anyhow!("Invalid Decisions config; expected supported TOML settings")
        }),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(FileConfig::default()),
        Err(_) => bail!("Could not read Decisions config"),
    }
}

pub fn normalize_base_url(raw: &str) -> Result<String> {
    let url = url::Url::parse(raw).map_err(|_| anyhow::anyhow!("Invalid base URL"))?;
    if !matches!(url.scheme(), "http" | "https")
        || url.host_str().is_none()
        || !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
    {
        bail!("Base URL must be HTTP(S), without credentials, query, or fragment");
    }
    let local = matches!(url.host_str(), Some("localhost" | "127.0.0.1" | "[::1]"));
    if url.scheme() == "http" && !local {
        bail!("Use HTTPS for remote gateways; HTTP is allowed only on localhost");
    }
    Ok(url.as_str().trim_end_matches('/').to_owned())
}

impl Settings {
    pub fn load() -> Result<Self> {
        let file = read_config()?;
        let base_url = normalize_base_url(
            &env::var("DECISIONS_BASE_URL")
                .ok()
                .or(file.base_url)
                .unwrap_or_else(|| DEFAULT_BASE_URL.into()),
        )?;
        let model = env::var("DECISIONS_MODEL")
            .ok()
            .or(file.model)
            .unwrap_or_else(|| DEFAULT_MODEL.into());
        if model.trim().is_empty() {
            bail!("Model must not be empty");
        }
        let roots = if let Some(raw) = env::var_os("DECISIONS_ALLOWED_ROOTS") {
            env::split_paths(&raw)
                .filter(|p| !p.as_os_str().is_empty())
                .collect()
        } else {
            file.roots.unwrap_or(vec![
                env::current_dir().context("Could not determine working directory")?
            ])
        };
        if roots.is_empty() {
            bail!("At least one allowed root is required");
        }
        let cache_path = env::var_os("DECISIONS_CACHE_PATH")
            .map(PathBuf::from)
            .or(file.cache_path)
            .unwrap_or_else(|| {
                dirs::cache_dir()
                    .unwrap_or_else(env::temp_dir)
                    .join("decisions/results.sqlite3")
            });
        let cache_ttl = match env::var("DECISIONS_CACHE_TTL_SECONDS") {
            Ok(value) => value
                .parse::<u64>()
                .context("Cache TTL must be a nonnegative integer")?,
            Err(_) => file.cache_ttl.unwrap_or(86400),
        };
        Ok(Self {
            base_url,
            model,
            roots,
            cache_path,
            cache_ttl,
        })
    }

    fn entry(&self) -> Result<keyring::Entry> {
        keyring::Entry::new("decisions", &self.base_url)
            .map_err(|_| anyhow::anyhow!("Could not access the OS credential store"))
    }

    pub fn credential(&self) -> Result<String> {
        // Generic keys are explicit overrides for the selected endpoint. An OpenAI
        // environment key must never silently follow a base URL change to a gateway.
        for name in ["DECISIONS_API_KEY", "API_KEY"] {
            if let Ok(key) = env::var(name) {
                if !key.trim().is_empty() {
                    return Ok(key);
                }
            }
        }
        if self.base_url == DEFAULT_BASE_URL {
            if let Ok(key) = env::var("OPENAI_API_KEY") {
                if !key.trim().is_empty() {
                    return Ok(key);
                }
            }
        }
        self.entry()?.get_password().map_err(|_| anyhow::anyhow!("No credential available for this endpoint. Run decisions auth login or set DECISIONS_API_KEY"))
    }

    pub fn login(&self, key: &str) -> Result<()> {
        if key.trim().is_empty() || key.contains(['\r', '\n']) {
            bail!("API key must be a nonempty single line");
        }
        self.entry()?
            .set_password(key)
            .map_err(|_| anyhow::anyhow!("Could not save credential in the OS credential store"))
    }

    pub fn logout(&self) -> Result<()> {
        match self.entry()?.delete_credential() {
            Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
            Err(_) => bail!("Could not remove credential from the OS credential store"),
        }
    }
}

pub fn set_config(key: &str, value: &str) -> Result<()> {
    let mut file = read_config()?;
    match key {
        "base_url" => file.base_url = Some(normalize_base_url(value)?),
        "model" if !value.trim().is_empty() => file.model = Some(value.to_owned()),
        _ => bail!("Supported settings: base_url and model (nonempty values)"),
    }
    let path = config_path()?;
    if let Some(parent) = path.parent().filter(|p| !p.as_os_str().is_empty()) {
        fs::create_dir_all(parent).context("Could not create config directory")?;
    }
    let text = toml::to_string_pretty(&file).context("Could not encode config")?;
    fs::write(path, text).context("Could not write config")
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn canonical_endpoint_identity() {
        assert_eq!(
            normalize_base_url("https://API.OPENAI.COM:443/v1/").unwrap(),
            DEFAULT_BASE_URL
        );
        assert_eq!(
            normalize_base_url("http://localhost:4000/v1").unwrap(),
            "http://localhost:4000/v1"
        );
        for bad in [
            "https://user:secret@example.com/v1",
            "https://example.com/v1?key=secret",
            "file:///tmp/key",
            "http://gateway.example/v1",
        ] {
            assert!(normalize_base_url(bad).is_err());
        }
    }
}
