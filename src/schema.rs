pub fn tools() -> serde_json::Value {
    serde_json::from_str(include_str!("tools.json")).expect("embedded tool schemas")
}
