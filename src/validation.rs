use anyhow::{bail, ensure, Context, Result};
use serde_json::{json, Value};

pub fn fields(v: &Value, allowed: &[&str], required: &[&str]) -> Result<()> {
    let map = v.as_object().context("Expected an object")?;
    ensure!(
        map.keys().all(|k| allowed.contains(&k.as_str())),
        "Unknown input field"
    );
    ensure!(
        required.iter().all(|k| map.contains_key(*k)),
        "Missing required field"
    );
    Ok(())
}
pub fn text(v: &Value) -> Result<&str> {
    let s = v.as_str().context("Expected text")?;
    ensure!(
        !s.is_empty() && s.len() <= 1_048_576,
        "Text must contain 1–1048576 bytes"
    );
    Ok(s)
}
pub fn validate_question(q: &Value) -> Result<()> {
    let kind = q["type"].as_str().context("Missing question type")?;
    let extra = match kind {
        "predicate" => None,
        "choice" => Some("choices"),
        "score" => Some("levels"),
        _ => bail!("Unknown question type"),
    };
    let mut allowed = vec!["type", "name", "instructions"];
    if let Some(e) = extra {
        allowed.push(e);
    }
    fields(q, &allowed, &allowed)?;
    text(&q["name"])?;
    text(&q["instructions"])?;
    if let Some(e) = extra {
        let options = q[e].as_array().context("Expected options array")?;
        ensure!(
            (2..=if kind == "choice" { 255 } else { 10 }).contains(&options.len()),
            "Invalid number of options"
        );
        let mut seen = Vec::new();
        for option in options {
            let key = if kind == "choice" { "value" } else { "label" };
            fields(option, &[key, "description"], &[key])?;
            if kind == "choice" {
                ensure!(
                    option[key].is_string() || option[key].is_boolean(),
                    "Choice values must be text or boolean"
                );
                ensure!(
                    !seen.contains(&&option[key]),
                    "Choice values must be unique"
                );
                seen.push(&option[key]);
            } else {
                ensure!(option[key].is_string(), "Level labels must be text");
            }
            if let Some(d) = option.get("description") {
                ensure!(d.is_string() || d.is_null(), "Description must be text");
            }
        }
    }
    Ok(())
}
pub fn request_for_tool(tool: &str, args: Value) -> Result<(Value, bool)> {
    let mut allowed = vec!["evidence", "use_cache"];
    let mut required = vec!["evidence"];
    match tool {
        "evaluate" => {
            allowed.push("questions");
            required.push("questions");
        }
        "check" => {
            allowed.extend(["statement", "name"]);
            required.push("statement");
        }
        "classify" => {
            allowed.extend(["instructions", "choices", "name"]);
            required.extend(["instructions", "choices"]);
        }
        "score" => {
            allowed.extend(["instructions", "levels", "name"]);
            required.extend(["instructions", "levels"]);
        }
        _ => bail!("Unknown tool"),
    }
    fields(&args, &allowed, &required)?;
    let use_cache = args
        .get("use_cache")
        .map(|v| v.as_bool().context("use_cache must be boolean"))
        .transpose()?
        .unwrap_or(false);
    ensure!(
        !args["evidence"]
            .as_array()
            .context("Expected evidence array")?
            .is_empty(),
        "Evidence must not be empty"
    );
    let mut questions = if tool == "evaluate" {
        args["questions"].clone()
    } else {
        let mut q = json!({"type": match tool {"check"=>"predicate", "classify"=>"choice", _=>"score"}, "name":args.get("name").cloned().unwrap_or(json!("result")), "instructions":args[if tool == "check" {"statement"} else {"instructions"}]});
        if tool == "classify" {
            q["choices"] = args["choices"].clone();
        }
        if tool == "score" {
            q["levels"] = args["levels"].clone();
        }
        json!([q])
    };
    if let Some(qs) = questions.as_array_mut() {
        for q in qs {
            for key in ["choices", "levels"] {
                if let Some(options) = q.get_mut(key).and_then(Value::as_array_mut) {
                    for option in options {
                        if option.get("description").is_some_and(Value::is_null) {
                            option
                                .as_object_mut()
                                .context("Expected option object")?
                                .remove("description");
                        }
                    }
                }
            }
        }
    }
    let qs = questions.as_array().context("Expected questions array")?;
    ensure!((1..=400).contains(&qs.len()), "Expected 1–400 questions");
    let mut seen = Vec::new();
    for q in qs {
        validate_question(q)?;
        ensure!(!seen.contains(&&q["name"]), "Question names must be unique");
        seen.push(&q["name"]);
    }
    Ok((
        json!({"evidence":args["evidence"],"questions":questions,"use_cache":use_cache}),
        use_cache,
    ))
}
fn number(v: &Value, low: f64, high: f64) -> Result<()> {
    let n = v.as_f64().context("Expected number")?;
    ensure!(
        n.is_finite() && (low..=high).contains(&n),
        "Number outside range"
    );
    Ok(())
}
pub fn validate_response(payload: &Value, questions: &Value) -> Result<()> {
    let answers = payload["answers"].as_array().context("Missing answers")?;
    let qs = questions.as_array().context("Missing questions")?;
    ensure!(answers.len() == qs.len(), "Answer count differs");
    for (a, q) in answers.iter().zip(qs) {
        ensure!(a["name"] == q["name"], "Answer name differs");
        if a["type"] == "refusal" {
            continue;
        }
        ensure!(a["type"] == q["type"], "Answer type differs");
        if q["type"] == "predicate" {
            number(&a["probability"], 0., 1.)?;
            continue;
        }
        number(&a["confidence"], 0., 1.)?;
        if q["type"] == "choice" {
            ensure!(
                q["choices"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .any(|c| c["value"] == a["choice"]),
                "Unexpected choice"
            );
        } else {
            number(
                &a["score"],
                0.,
                q["levels"].as_array().unwrap().len() as f64 - 1.,
            )?;
        }
        for p in a["probabilities"]
            .as_array()
            .context("Missing probabilities")?
        {
            number(&p["probability"], 0., 1.)?;
            if q["type"] == "choice" {
                ensure!(
                    q["choices"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .any(|c| c["value"] == p["value"]),
                    "Unexpected choice probability"
                );
            } else {
                let i = p["value"].as_u64().context("Invalid level index")? as usize;
                let levels = q["levels"].as_array().unwrap();
                ensure!(
                    i < levels.len() && p["label"] == levels[i]["label"],
                    "Invalid level probability"
                );
            }
        }
    }
    Ok(())
}
