//! Package `app/templates/brief-maker/` into a Brief Maker template app.clan
//! using the SDK alone — the CLI is never required for this (whatever the CLI
//! can do, the SDK can do).
//!
//!     cargo run -p clan-sdk --example make_brief_maker -- [output.clan] [research.example.clan]
//!
//! Steps: create a base document seeded with the app schema → pack the app's
//! index.html as the human view → declare capability requirements (spec §22)
//! → embed the pipeline contract at `app/pipeline.yaml`, the member schemas at
//! `app/schemas/` and the two empty members → make_template with the AppInfo
//! block → reinstall the app's own context, which make_template replaced.
//! Then self-check: instantiate the template and assert the contracts, the
//! members and the context travelled into the instance (spec: instantiation
//! copies every member), and validate both artifacts.
//!
//! The pipeline is template-borne: every Brief Maker app instantiated from
//! this template carries the same declaration of the briefing pipeline it runs
//! through (the middleware, napkin.middleware/1 §10, resolves its tasks).
//!
//! A brief can also start from research. The app declares
//! `app.spinoff.upstream` (Contract 4 §5): a spin-off from a Research Tool
//! document carries it whole — its data frozen at `upstream.<research id>`,
//! its pins and findings merged into `shared/facts.yaml` and
//! `shared/findings.yaml` (which is why the template registers both, empty,
//! with the Research Tool's schemas), its chain, branches and assets. The
//! self-check spins the Research Tool's example off into a brief and asserts
//! all of it (`spin_off_the_example`). The example is
//! `app/public/apps/campaign-research.example.clan` unless a path is given,
//! and is built with `make_campaign_research` when it is not there yet.

use clan_sdk::hash::sha256_prefixed;
use clan_sdk::{
    create, instantiate, make_template, pack_html, patch_context, patch_requirements, spinoff,
    validate, AppInfo, ClanBuilder, ClanFile, CreateOptions, DecisionChain, FileEntry,
    InstantiateOptions, MakeTemplateOptions, SpinoffOptions, SpinoffSpec,
};
use serde_yaml::Value;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

#[path = "shared/app_ui.rs"]
mod app_ui;

const APP_ID: &str = "ie.napkin.brief-maker";
/// The one app a brief is spun off from, whole.
const RESEARCH_APP_ID: &str = "ie.napkin.campaign-research";
const PIPELINE_PATH: &str = "app/pipeline.yaml";
const SCHEMA_PATH: &str = "agent/output-schema.json";
const FACTS_PATH: &str = "shared/facts.yaml";
const FINDINGS_PATH: &str = "shared/findings.yaml";
const SOURCES_PATH: &str = "shared/sources.yaml";
const FACTS_SCHEMA_PATH: &str = "app/schemas/facts.schema.json";
const FINDINGS_SCHEMA_PATH: &str = "app/schemas/findings.schema.json";
const CHAIN_PATH: &str = "agent/decision-chain.yaml";
const DATA_PATH: &str = "shared/data.yaml";
const ASSET_PREFIX: &str = "human/assets/";

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
        FACTS_SCHEMA_PATH,
        "member-schema",
        Source::File("facts.schema.json"),
    ),
    (
        "findings-schema",
        FINDINGS_SCHEMA_PATH,
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

fn repo_root() -> PathBuf {
    // crates/clan-sdk → repo root
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
}

fn template_dir() -> PathBuf {
    repo_root().join("app/templates/brief-maker")
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

fn yaml(clan: &ClanFile, path: &str) -> Result<Value, Box<dyn std::error::Error>> {
    Ok(serde_yaml::from_slice(&clan.read_entry(path)?)?)
}

/// Every problem `instance` has against the JSON Schema `clan` holds at
/// `schema_path`. The instance is YAML, read as JSON.
fn schema_problems(
    clan: &ClanFile,
    schema_path: &str,
    instance: &Value,
) -> Result<Vec<String>, Box<dyn std::error::Error>> {
    let schema: serde_json::Value = serde_json::from_slice(&clan.read_entry(schema_path)?)?;
    let validator = jsonschema::validator_for(&schema)
        .map_err(|e| format!("{schema_path} does not compile: {e}"))?;
    let instance = serde_json::to_value(instance)?;
    Ok(validator
        .iter_errors(&instance)
        .map(|e| format!("{e} at {}", e.instance_path))
        .collect())
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let dir = template_dir();
    let mut args = std::env::args().skip(1);
    let output = args
        .next()
        .unwrap_or_else(|| "brief-maker.app.clan".to_string());
    let research = args
        .next()
        .map(PathBuf::from)
        .unwrap_or_else(|| repo_root().join("app/public/apps/campaign-research.example.clan"));

    let schema = fs::read_to_string(dir.join("schema.json"))?;
    // The agent figures and the OS's fields are inlined at build (one
    // snippet each, every app).
    let index_html = app_ui::inline_fields(
        &dir.join(".."),
        &app_ui::inline_figures(
            &dir.join(".."),
            &fs::read_to_string(dir.join("index.html"))?,
        )?,
    )?;
    let requirements = fs::read_to_string(dir.join("agent/requirements.yaml"))?;
    let context = fs::read_to_string(dir.join("context.md"))?;

    // 1. Base document, seeded with the brief schema so instances constrain agents.
    let base = create(CreateOptions {
        title: "Brief Maker".into(),
        brief: "Template app: creative-brief authoring on the napkin briefing pipeline".into(),
        document_type: None,
        no_render: false,
        schema: Some(schema),
    })?;
    let clan = ClanFile::from_bytes(base)?;

    // 2. The app's UI is the human view.
    let clan = ClanFile::from_bytes(pack_html(&clan, &index_html, None, None, None, None)?)?;

    // 3. Capability requirements (spec §22, layer 5) — first-class via the SDK.
    let clan = ClanFile::from_bytes(patch_requirements(&clan, &requirements)?)?;

    // 4. Pipeline contract, member schemas and the two empty members,
    //    registered. The members stay empty until a spin-off from research
    //    merges its pins and findings in (Contract 4 §5.2); into a member
    //    that holds no entry the research's bytes are written unchanged.
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
            name: "Brief Maker".into(),
            app_id: APP_ID.into(),
            // 0.6: a brief can start from research, carried whole.
            version: "0.6.0".into(),
            icon: Some(app_ui::ICON_PATH.into()),
            entry: "human/index.html".into(),
            schema: Some(SCHEMA_PATH.into()),
            prompt_templates: vec![],
            data_seed: None,
            spinoff: Some(SpinoffSpec {
                accepts: vec![RESEARCH_APP_ID.into()],
                // Carried whole under upstream.<research id>, so no map; and
                // no lift: the brief's fields are captured or drafted, never
                // seeded from research.
                upstream: true,
                ..Default::default()
            }),
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
    assert!(
        instance.has_entry("agent/requirements.yaml"),
        "agent/requirements.yaml did not travel into the instance"
    );
    assert!(
        instance
            .read_entry_string("agent/context.md")?
            .contains("A brief started from research"),
        "the app's context was lost — make_template's boilerplate won"
    );
    require_valid("template", &template);
    require_valid("instance", &instance);

    // 8. Self-check: the Research Tool's example, spun off into a brief.
    spin_off_the_example(&template, &research)?;

    fs::write(&output, &template_bytes)?;
    println!(
        "wrote {output} ({} bytes) — context, requirements, pipeline and the facts/findings \
         members verified in an instance; {} spun off whole",
        template_bytes.len(),
        research.display()
    );
    println!("install: copy into the Napkin Studio app library as {APP_ID}/app.clan");
    Ok(())
}

/// The Research Tool's example, built beside its template app by
/// `make_campaign_research` when it is not there yet (`npm run apps` packs
/// the brief first).
fn research_example(path: &Path) -> Result<ClanFile, Box<dyn std::error::Error>> {
    if !path.exists() {
        let out = path.parent().unwrap_or(Path::new("."));
        fs::create_dir_all(out)?;
        let cargo = std::env::var("CARGO").unwrap_or_else(|_| "cargo".into());
        let status = Command::new(cargo)
            .args([
                "run",
                "-q",
                "-p",
                "clan-sdk",
                "--example",
                "make_campaign_research",
                "--",
            ])
            .arg(out.join("campaign-research.app.clan"))
            .arg(path)
            .current_dir(repo_root())
            .status()?;
        if !status.success() {
            return Err(format!("make_campaign_research failed ({status})").into());
        }
    }
    Ok(ClanFile::from_bytes(fs::read(path)?)?)
}

/// Spin the Research Tool's example off into a brief and assert it arrived
/// whole (Contract 4 §5.2–§5.3): the data frozen at `upstream.<research id>`
/// and nothing else at the root, the members byte for byte under their roles
/// and valid against the brief's own member schemas, every asset, the whole
/// chain pinned and not re-addressed, the brief's own contracts and context,
/// `lineage.carried`, and data the brief schema accepts.
fn spin_off_the_example(
    template: &ClanFile,
    research_path: &Path,
) -> Result<(), Box<dyn std::error::Error>> {
    let research = research_example(research_path)?;
    let doc = research.manifest().document_id().to_string();
    let brief = ClanFile::from_bytes(spinoff(
        template,
        &research,
        SpinoffOptions {
            title: "Lúnasa 0.0 launch — brief".into(),
            ..Default::default()
        },
    )?)?;
    assert_eq!(
        brief.manifest().app.as_ref().map(|a| a.app_id.as_str()),
        Some(APP_ID),
        "the spin-off is not a Brief Maker document"
    );

    // The data: the research's, less its projection and its upstream, frozen
    // under its document id, and nothing else at the root.
    let data = yaml(&brief, DATA_PATH)?;
    let root: Vec<&str> = data
        .as_mapping()
        .ok_or("the brief's data is not a mapping")?
        .keys()
        .filter_map(Value::as_str)
        .collect();
    assert_eq!(
        root,
        ["upstream"],
        "the research's data reached the brief's root"
    );
    let mut frozen = yaml(&research, DATA_PATH)?;
    if let Some(m) = frozen.as_mapping_mut() {
        m.remove("projection");
        m.remove("upstream");
    }
    assert_eq!(
        data["upstream"][doc.as_str()],
        frozen,
        "upstream.{doc} is not the research's data, frozen"
    );

    // The members: the brief's were empty, so the research's bytes land
    // unchanged, under the brief's roles, and pass the brief's own member
    // schemas.
    for (path, role, schema) in [
        (FACTS_PATH, "pinned-facts", FACTS_SCHEMA_PATH),
        (FINDINGS_PATH, "findings", FINDINGS_SCHEMA_PATH),
    ] {
        assert_eq!(
            brief.read_entry(path)?,
            research.read_entry(path)?,
            "{path} was not carried byte for byte"
        );
        assert_eq!(role_of(&brief, path), Some(role), "{path} lost its role");
        let problems = schema_problems(&brief, schema, &yaml(&brief, path)?)?;
        assert!(
            problems.is_empty(),
            "{path} against {schema}: {problems:#?}"
        );
    }
    if research.has_entry(SOURCES_PATH) {
        assert_eq!(
            brief.read_entry(SOURCES_PATH)?,
            research.read_entry(SOURCES_PATH)?,
            "{SOURCES_PATH} was not carried byte for byte"
        );
        assert_eq!(role_of(&brief, SOURCES_PATH), Some("sources"));
    }

    // The assets, the extracted-text sidecars included.
    let assets: Vec<_> = research
        .read_all_entries()?
        .into_iter()
        .filter(|(p, _)| p.starts_with(ASSET_PREFIX))
        .collect();
    assert!(!assets.is_empty(), "the example carries the client's email");
    for (path, bytes) in &assets {
        assert_eq!(&brief.read_entry(path)?, bytes, "{path} was not carried");
        assert!(
            brief.manifest().file_by_path(path).is_some(),
            "{path} is not registered"
        );
    }

    // The chain: every research decision, pinned, none re-addressed.
    let theirs = DecisionChain::from_yaml(&research.read_entry(CHAIN_PATH)?)?;
    let ours = DecisionChain::from_yaml(&brief.read_entry(CHAIN_PATH)?)?;
    assert!(
        !theirs.decisions.is_empty(),
        "the example has a chain to carry"
    );
    for d in theirs.decisions.iter().filter(|d| d.id.is_some()) {
        let carried = ours
            .decisions
            .iter()
            .find(|c| c.id == d.id)
            .ok_or_else(|| format!("decision {:?} was not carried", d.id))?;
        assert!(carried.pinned, "decision {:?} is not pinned", d.id);
        assert_eq!(
            carried.targets, d.targets,
            "decision {:?} was re-addressed",
            d.id
        );
    }

    // The brief's own contracts and context, not the research's.
    for path in [
        PIPELINE_PATH,
        SCHEMA_PATH,
        FACTS_SCHEMA_PATH,
        FINDINGS_SCHEMA_PATH,
    ] {
        assert_eq!(
            brief.read_entry(path)?,
            template.read_entry(path)?,
            "{path} is not the brief's own"
        );
    }
    assert!(
        brief
            .read_entry_string("agent/context.md")?
            .contains("A brief started from research"),
        "the spun-off brief does not hold the brief's context"
    );

    // What the hop took.
    let carried = brief
        .manifest()
        .carried()
        .ok_or("the spin-off records no lineage.carried")?;
    assert_eq!(carried.document_id, doc);
    assert_eq!(
        carried.data_sha256,
        sha256_prefixed(&research.read_entry(DATA_PATH)?)
    );
    assert_eq!(
        carried.facts_sha256.as_deref(),
        Some(sha256_prefixed(&research.read_entry(FACTS_PATH)?).as_str())
    );
    assert_eq!(
        carried.findings_sha256.as_deref(),
        Some(sha256_prefixed(&research.read_entry(FINDINGS_PATH)?).as_str())
    );

    // The brief schema accepts the spun-off data, and the file validates.
    let problems = schema_problems(&brief, SCHEMA_PATH, &data)?;
    assert!(
        problems.is_empty(),
        "the spun-off data against the brief schema: {problems:#?}"
    );
    require_valid("spun-off brief", &brief);
    Ok(())
}
