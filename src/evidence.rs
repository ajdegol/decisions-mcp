use crate::validation::fields;
use anyhow::{bail, ensure, Context, Result};
use base64::{engine::general_purpose::STANDARD, Engine};
use serde_json::{json, Value};
use std::{fs::File, io::Read, path::PathBuf};
pub const MAX_FILE: usize = 20 * 1024 * 1024;
pub const MAX_BODY: usize = 32 * 1024 * 1024;
fn read_local(path: &str, roots: &[PathBuf]) -> Result<Vec<u8>> {
    let expanded = if let Some(rest) = path.strip_prefix("~/") {
        dirs::home_dir().context("Home unavailable")?.join(rest)
    } else {
        PathBuf::from(path)
    };
    let target = expanded
        .canonicalize()
        .context("Cannot resolve evidence file")?;
    ensure!(
        roots
            .iter()
            .filter_map(|r| r.canonicalize().ok())
            .any(|r| target.starts_with(r)),
        "Local file is outside allowed roots"
    );
    ensure!(
        target.metadata()?.is_file(),
        "Evidence must be a regular file"
    );
    let file = File::open(target).context("Cannot open evidence file")?;
    ensure!(
        file.metadata()?.is_file(),
        "Evidence must be a regular file"
    );
    let mut bytes = Vec::new();
    file.take(MAX_FILE as u64 + 1).read_to_end(&mut bytes)?;
    ensure!(bytes.len() <= MAX_FILE, "Local evidence exceeds 20 MiB");
    Ok(bytes)
}
fn mime(data: &[u8]) -> Result<&'static str> {
    if data.starts_with(b"\x89PNG\r\n\x1a\n") {
        Ok("image/png")
    } else if data.starts_with(b"\xff\xd8\xff") {
        Ok("image/jpeg")
    } else if data.starts_with(b"GIF87a") || data.starts_with(b"GIF89a") {
        Ok("image/gif")
    } else if data.starts_with(b"RIFF") && data.get(8..12) == Some(b"WEBP") {
        Ok("image/webp")
    } else {
        bail!("Supported image formats: PNG, JPEG, GIF, WebP")
    }
}
fn json_limit(v: &Value, depth: usize, count: &mut usize) -> Result<()> {
    *count += 1;
    ensure!(*count <= 10_000, "JSON exceeds 10000 values");
    if v.is_array() || v.is_object() {
        ensure!(depth < 32, "JSON exceeds 32 container levels");
        match v {
            Value::Array(a) => {
                for c in a {
                    json_limit(c, depth + 1, count)?
                }
            }
            Value::Object(o) => {
                for c in o.values() {
                    json_limit(c, depth + 1, count)?
                }
            }
            _ => {}
        }
    }
    Ok(())
}
pub fn prepare(evidence: &Value, roots: &[PathBuf]) -> Result<(Vec<Value>, bool)> {
    let mut parts = Vec::new();
    let mut cacheable = true;
    for item in evidence.as_array().context("Expected evidence array")? {
        let kind = item["type"].as_str().context("Missing evidence type")?;
        let (key, optional) = match kind {
            "text" => ("text", false),
            "json" => ("data", false),
            "text_path" => ("path", false),
            "image_path" => ("path", true),
            "image_url" => ("url", true),
            _ => bail!("Unknown evidence type"),
        };
        let mut allowed = vec!["type", key];
        if optional {
            allowed.push("detail");
        }
        fields(item, &allowed, &["type", key])?;
        let detail = item.get("detail").cloned().unwrap_or(json!("auto"));
        if optional {
            ensure!(
                ["auto", "low", "high", "original"]
                    .iter()
                    .any(|d| detail == *d),
                "Invalid image detail"
            );
        }
        let part = match kind {
            "text" => {
                json!({"type":"input_text","text":item[key].as_str().context("Expected text")?})
            }
            "json" => {
                let data = &item[key];
                ensure!(
                    data.is_object()
                        || data
                            .as_array()
                            .is_some_and(|a| a.iter().all(Value::is_object)),
                    "JSON evidence must be an object or array of objects"
                );
                ensure!(
                    serde_json::to_vec(data)?.len() <= 256 * 1024,
                    "JSON exceeds 256 KiB"
                );
                json_limit(data, 0, &mut 0)?;
                json!({"type":"input_json","data":data})
            }
            "text_path" => {
                let data = read_local(item[key].as_str().context("Expected path")?, roots)?;
                let text = std::str::from_utf8(&data)
                    .context("Text files must be UTF-8")?
                    .trim_start_matches('\u{feff}');
                json!({"type":"input_text","text":text})
            }
            "image_path" => {
                let data = read_local(item[key].as_str().context("Expected path")?, roots)?;
                json!({"type":"input_image","detail":detail,"image_url":format!("data:{};base64,{}",mime(&data)?,STANDARD.encode(&data))})
            }
            _ => {
                let url = item[key].as_str().context("Expected image URL")?;
                if url.starts_with("http://") || url.starts_with("https://") {
                    url::Url::parse(url).context("Invalid image URL")?;
                    cacheable = false;
                } else {
                    let (header, encoded) = url
                        .split_once(',')
                        .context("Expected base64 image data URL")?;
                    ensure!(encoded.len() <= MAX_FILE.div_ceil(3) * 4, "Image too large");
                    let data = STANDARD.decode(encoded).context("Invalid base64 image")?;
                    ensure!(
                        data.len() <= MAX_FILE && header == format!("data:{};base64", mime(&data)?),
                        "Invalid image data URL"
                    );
                }
                json!({"type":"input_image","detail":detail,"image_url":url})
            }
        };
        parts.push(part);
    }
    ensure!(
        serde_json::to_vec(&parts)?.len() <= MAX_BODY,
        "Evidence exceeds 32 MiB"
    );
    Ok((parts, cacheable))
}
