use decisions_mcp::{evidence::prepare, request_for_tool};
use serde_json::json;
#[test]
fn validation_preserves_typed_choices_and_rejects_unknown_fields() {
    let base = json!({"evidence":[{"type":"json","data":{"k":1}}],"instructions":"Choose","choices":[{"value":true},{"value":"true"}]});
    assert!(request_for_tool("classify", base.clone()).is_ok());
    let mut bad = base.clone();
    bad["choices"] = json!([{"value":true},{"value":true}]);
    assert!(request_for_tool("classify", bad).is_err());
    let mut bad = base;
    bad["api_key"] = json!("ignored?");
    assert!(request_for_tool("classify", bad).is_err());
    assert!(request_for_tool("check", json!({"evidence":[],"statement":"test"})).is_err());
    assert!(request_for_tool(
        "check",
        json!({"evidence":[{"type":"text","text":"x"}],"statement":""})
    )
    .is_err());
}
#[test]
fn local_bytes_and_symlink_targets_are_checked() {
    let root = tempfile::tempdir().unwrap();
    let outside = tempfile::tempdir().unwrap();
    let f = root.path().join("input.txt");
    std::fs::write(&f, b"\xef\xbb\xbfhello").unwrap();
    let (parts, cacheable) = prepare(
        &json!([{"type":"text_path","path":f}]),
        &[root.path().into()],
    )
    .unwrap();
    assert_eq!(parts[0]["text"], "hello");
    assert!(cacheable);
    std::fs::write(&f, b"changed").unwrap();
    let (p, _) = prepare(
        &json!([{"type":"text_path","path":f}]),
        &[root.path().into()],
    )
    .unwrap();
    assert_ne!(p, parts);
    let secret = outside.path().join("secret");
    std::fs::write(&secret, "test").unwrap();
    assert!(prepare(
        &json!([{"type":"text_path","path":secret}]),
        &[root.path().into()]
    )
    .is_err());
    #[cfg(unix)]
    {
        let link = root.path().join("link");
        std::os::unix::fs::symlink(secret, &link).unwrap();
        assert!(prepare(
            &json!([{"type":"text_path","path":link}]),
            &[root.path().into()]
        )
        .is_err());
    }
}
#[test]
fn remote_images_bypass_cache_and_invalid_inline_images_fail() {
    assert!(
        !prepare(
            &json!([{"type":"image_url","url":"https://example.com/image.png"}]),
            &[]
        )
        .unwrap()
        .1
    );
    assert!(prepare(
        &json!([{"type":"image_url","url":"data:image/png;base64,aGVsbG8="}]),
        &[]
    )
    .is_err());
    assert!(prepare(
        &json!([{"type":"image_url","url":"https://example.com/x","detail":"unlimited"}]),
        &[]
    )
    .is_err());
    assert!(prepare(&json!([{"type":"json","data":[1,2]}]), &[]).is_err());
    assert!(prepare(&json!([{"type":"text","text":false}]), &[]).is_err());
}
#[test]
fn batches_require_unique_names_and_bound_questions() {
    let q = json!({"type":"predicate","name":"same","instructions":"test"});
    assert!(request_for_tool(
        "evaluate",
        json!({"evidence":[{"type":"text","text":"x"}],"questions":[q,q]})
    )
    .is_err());
}
