//! Package `app/templates/campaign-research/` into the Research Tool template
//! app.clan, and its filled example into an instance, using the SDK alone.
//!
//!     cargo run -p clan-sdk --example make_campaign_research -- [app.clan] [example.clan]
//!
//! Defaults: `campaign-research.app.clan` and `campaign-research.example.clan`.
//!
//! Same shape as `make_advertising_studio`, with what this document adds:
//!
//! 1. Two members of its own, `shared/facts.yaml` (role `pinned-facts`) and
//!    `shared/findings.yaml` (role `findings`), registered EMPTY in the
//!    template so every instance starts with them. They are separate members
//!    because a data-update pack replaces `shared/data.yaml` whole and would
//!    otherwise delete every pin silently (Contract 3, D2). No SDK verb
//!    registers a member under a role of its choosing, so they are pushed into
//!    the manifest registry here, exactly as the pipeline contract is.
//! 2. The member schemas travel at `app/schemas/`, beside the pipeline, so a
//!    backend continuing the document can check what it writes into them.
//! 3. `make_template` replaces `agent/context.md` with boilerplate, so the
//!    app's own context is reinstalled and asserted to survive into an
//!    instance.
//! 4. The example instance is built from `example/` with the manifest id the
//!    example's addresses already use, and its facts, findings and chain packed
//!    byte for byte — `projection.built_from` holds the hashes of the first two,
//!    and an SDK read-modify-write of the chain drops the Contract 4 §3 fields.

use clan_sdk::hash::sha256_prefixed;
use clan_sdk::{
    create, instantiate, make_template, pack_html, patch_context, patch_requirements, validate,
    AppInfo, ClanBuilder, ClanFile, CreateOptions, FileEntry, InstantiateOptions,
    MakeTemplateOptions,
};
use std::fs;
use std::path::{Path, PathBuf};

#[path = "shared/app_ui.rs"]
mod app_ui;

const APP_ID: &str = "ie.napkin.campaign-research";
const PIPELINE_PATH: &str = "app/pipeline.yaml";
const FACTS_PATH: &str = "shared/facts.yaml";
const FINDINGS_PATH: &str = "shared/findings.yaml";
const CHAIN_PATH: &str = "agent/decision-chain.yaml";
const DATA_PATH: &str = "shared/data.yaml";

/// The id every address in `example/` is written against.
const EXAMPLE_ID: &str = "7c1e9a42-5b3d-4f8e-9a6c-2d1f0e8b4a17";

/// Registered members the template adds: (id, archive path, role, source).
enum Source {
    File(&'static str),
    Bytes(&'static [u8]),
}
const MEMBERS: &[(&str, &str, &str, Source)] = &[
    (
        "pipeline-contract",
        PIPELINE_PATH,
        "pipeline-contract",
        Source::File("app/pipeline.yaml"),
    ),
    (
        "facts-schema",
        "app/schemas/facts.schema.json",
        "member-schema",
        Source::File("facts.schema.json"),
    ),
    (
        "findings-schema",
        "app/schemas/findings.schema.json",
        "member-schema",
        Source::File("findings.schema.json"),
    ),
    (
        "pinned-facts",
        FACTS_PATH,
        "pinned-facts",
        Source::Bytes(b"facts: []\n"),
    ),
    (
        "findings",
        FINDINGS_PATH,
        "findings",
        Source::Bytes(b"findings: []\n"),
    ),
];

fn template_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .join("app/templates/campaign-research")
}

fn content_type(path: &str) -> &'static str {
    match Path::new(path).extension().and_then(|e| e.to_str()) {
        Some("yaml") => "application/yaml",
        Some("json") => "application/json",
        Some("svg") => "image/svg+xml",
        _ => "text/plain",
    }
}

fn register(builder: &mut ClanBuilder, id: &str, path: &str, role: &str, bytes: Vec<u8>) {
    builder.add_entry(path, bytes);
    let files = &mut builder.manifest_mut().files;
    if let Some(entry) = files.iter_mut().find(|f| f.path == path) {
        entry.role = role.into();
    } else {
        files.push(FileEntry {
            id: id.into(),
            path: path.into(),
            role: role.into(),
            content_type: content_type(path).into(),
            priority: None,
            sha256: None,
        });
    }
}

fn copy_all(clan: &ClanFile) -> Result<ClanBuilder, Box<dyn std::error::Error>> {
    let mut builder = ClanBuilder::new(clan.manifest().clone());
    for (path, bytes) in clan.read_all_entries()? {
        builder.add_entry(path, bytes);
    }
    Ok(builder)
}

fn role_of<'a>(clan: &'a ClanFile, path: &str) -> Option<&'a str> {
    clan.manifest().file_by_path(path).map(|f| f.role.as_str())
}

fn require_valid(label: &str, file: &ClanFile) {
    let report = validate(file);
    assert!(
        report.is_content_valid(),
        "{label} does not validate strictly: {report:?}"
    );
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let dir = template_dir();
    let mut args = std::env::args().skip(1);
    let output = args
        .next()
        .unwrap_or_else(|| "campaign-research.app.clan".to_string());
    let example_output = args
        .next()
        .unwrap_or_else(|| "campaign-research.example.clan".to_string());

    let schema = fs::read_to_string(dir.join("schema.json"))?;
    // The agent figures are inlined at build (one snippet, both apps).
    let index_html = app_ui::inline_figures(
        &dir.join(".."),
        &fs::read_to_string(dir.join("index.html"))?,
    )?;
    let requirements = fs::read_to_string(dir.join("agent/requirements.yaml"))?;
    let context = fs::read_to_string(dir.join("context.md"))?;

    // 1. Base document, seeded with the campaign schema (→ agent/output-schema.json).
    let base = create(CreateOptions {
        title: "Research Tool".into(),
        brief: "Template app: campaign research — the ask, pinned facts and findings, on the \
                napkin campaign pipeline"
            .into(),
        document_type: None,
        no_render: false,
        schema: Some(schema),
    })?;
    let clan = ClanFile::from_bytes(base)?;

    // 2. The app's UI is the human view.
    let clan = ClanFile::from_bytes(pack_html(&clan, &index_html, None, None, None, None)?)?;

    // 3. Capability requirements (spec §22): the middleware.
    let clan = ClanFile::from_bytes(patch_requirements(&clan, &requirements)?)?;

    // 4. Pipeline contract, member schemas and the two empty members, registered.
    let mut builder = copy_all(&clan)?;
    for (id, path, role, source) in MEMBERS {
        let bytes = match source {
            Source::File(rel) => fs::read(dir.join(rel))?,
            Source::Bytes(b) => b.to_vec(),
        };
        register(&mut builder, id, path, role, bytes);
    }
    // The mark the home screen shows (app.icon names this member).
    register(
        &mut builder,
        "app-icon",
        app_ui::ICON_PATH,
        "app-icon",
        app_ui::icon_svg(&dir)?,
    );
    let clan = ClanFile::from_bytes(builder.build()?)?;

    // 5. Stamp the app block and flip to a template.
    let template_bytes = make_template(
        &clan,
        AppInfo {
            name: "Research Tool".into(),
            app_id: APP_ID.into(),
            // 0.2: the Plan-zone identity; figures from the shared snippet.
            version: "0.3.0".into(),
            icon: Some(app_ui::ICON_PATH.into()),
            entry: "human/index.html".into(),
            schema: Some("agent/output-schema.json".into()),
            prompt_templates: vec![],
            data_seed: None,
            spinoff: None,
        },
        MakeTemplateOptions::default(),
    )?;

    // 6. Reinstall the app's own context — make_template replaced it.
    let template = ClanFile::from_bytes(template_bytes)?;
    let template_bytes = patch_context(&template, &context, false)?;
    let template = ClanFile::from_bytes(template_bytes.clone())?;

    // 7. Self-check: a fresh instance carries every contract and both members,
    //    empty and under their roles, and the app's context — not boilerplate.
    let instance = ClanFile::from_bytes(instantiate(
        &template,
        InstantiateOptions {
            title: "smoke-instance".into(),
            ..Default::default()
        },
    )?)?;
    for (_, path, role, source) in MEMBERS {
        assert_eq!(
            role_of(&instance, path),
            Some(*role),
            "{path} did not travel into the instance as role {role}"
        );
        if let Source::Bytes(b) = source {
            assert_eq!(instance.read_entry(path)?, *b, "{path} is not empty");
        }
    }
    assert!(instance.has_entry("agent/requirements.yaml"));
    assert!(
        instance
            .read_entry_string("agent/context.md")?
            .contains("No form, no defaults."),
        "the app's context was lost — make_template's boilerplate won"
    );
    require_valid("template", &template);
    require_valid("instance", &instance);

    // 8. The example instance, under the id its addresses use.
    let example_bytes = build_example(&template, &dir.join("example"))?;

    fs::write(&output, &template_bytes)?;
    fs::write(&example_output, &example_bytes)?;
    println!(
        "wrote {output} ({} bytes) — context, requirements, pipeline and the facts/findings \
         members verified in an instance",
        template_bytes.len()
    );
    println!(
        "wrote {example_output} ({} bytes) — id {EXAMPLE_ID}, members byte-identical to example/",
        example_bytes.len()
    );
    println!("install: copy into the Napkin Studio app library as {APP_ID}/app.clan");
    Ok(())
}

/// Instantiate the template as the example document and lay `example/` over
/// it verbatim.
fn build_example(
    template: &ClanFile,
    example: &Path,
) -> Result<Vec<u8>, Box<dyn std::error::Error>> {
    let instance = ClanFile::from_bytes(instantiate(
        template,
        InstantiateOptions {
            title: "Lúnasa 0.0 launch — EXAMPLE".into(),
            instance_id: Some(EXAMPLE_ID.into()),
            ..Default::default()
        },
    )?)?;

    let data = fs::read(example.join(DATA_PATH))?;
    let facts = fs::read(example.join(FACTS_PATH))?;
    let findings = fs::read(example.join(FINDINGS_PATH))?;
    let chain = fs::read(example.join(CHAIN_PATH))?;
    let email = fs::read(example.join("assets/client-email.txt"))?;

    // The materials index cites the attachment by `assets/<name>`, which the
    // host serves from `human/assets/<name>`; the upload path also caches the
    // extracted text as a sidecar, so the example carries one too.
    let mut builder = copy_all(&instance)?;
    builder.add_entry(DATA_PATH, data.clone());
    builder.add_entry(FACTS_PATH, facts.clone());
    builder.add_entry(FINDINGS_PATH, findings.clone());
    builder.add_entry(CHAIN_PATH, chain.clone());
    register(
        &mut builder,
        "human-assets-client-email.txt",
        "human/assets/client-email.txt",
        "human-asset",
        email.clone(),
    );
    register(
        &mut builder,
        "human-assets-.extracted-client-email.txt.txt",
        "human/assets/.extracted/client-email.txt.txt",
        "human-asset",
        email.clone(),
    );
    let m = builder.manifest_mut();
    m.created_at = "2026-09-21T08:58:40Z".into();
    m.updated_at = "2026-09-23T08:02:01Z".into();
    let bytes = builder.build()?;
    let clan = ClanFile::from_bytes(bytes.clone())?;

    // Self-check: id, verbatim members, and the hashes the data already claims.
    assert_eq!(clan.manifest().id, EXAMPLE_ID);
    for (path, want) in [
        (DATA_PATH, &data),
        (FACTS_PATH, &facts),
        (FINDINGS_PATH, &findings),
        (CHAIN_PATH, &chain),
    ] {
        assert_eq!(
            &clan.read_entry(path)?,
            want,
            "{path} was not packed verbatim"
        );
    }
    assert_eq!(role_of(&clan, FACTS_PATH), Some("pinned-facts"));
    assert_eq!(role_of(&clan, FINDINGS_PATH), Some("findings"));

    let parsed: serde_yaml::Value = serde_yaml::from_slice(&data)?;
    let built_from = &parsed["projection"]["built_from"];
    assert_eq!(
        built_from["facts_sha256"].as_str(),
        Some(sha256_prefixed(&facts).as_str()),
        "projection.built_from.facts_sha256 does not match shared/facts.yaml"
    );
    assert_eq!(
        built_from["findings_sha256"].as_str(),
        Some(sha256_prefixed(&findings).as_str()),
        "projection.built_from.findings_sha256 does not match shared/findings.yaml"
    );
    assert_eq!(
        parsed["materials"]["mat_email01"]["sha256"].as_str(),
        Some(sha256_prefixed(&email).as_str()),
        "materials.mat_email01.sha256 does not match the attachment"
    );
    require_valid("example", &clan);
    Ok(bytes)
}
