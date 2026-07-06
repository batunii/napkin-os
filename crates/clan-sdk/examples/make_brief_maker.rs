//! Package `app/templates/brief-maker/` into a Brief Maker template app.clan
//! using the SDK alone — the CLI is never required for this (whatever the CLI
//! can do, the SDK can do).
//!
//!     cargo run -p clan-sdk --example make_brief_maker -- [output.clan]
//!
//! Steps: create a base document seeded with the app schema → pack the app's
//! index.html as the human view → declare capability requirements (spec §22)
//! → embed the pipeline contract at `app/pipeline.yaml` → make_template with
//! the AppInfo block. Then self-check: instantiate the template and assert the
//! pipeline contract files travelled into the instance (spec: instantiation
//! copies every member), and validate both artifacts.
//!
//! The two contract files are why the pipeline is template-borne: every Brief
//! Maker app instantiated from this template carries the same declaration of
//! the briefing pipeline it runs through (engine/agent-server is the reference
//! backend).

use clan_sdk::{
    create, instantiate, make_template, pack_html, patch_requirements, validate, AppInfo,
    ClanBuilder, ClanFile, CreateOptions, FileEntry, InstantiateOptions, MakeTemplateOptions,
};
use std::fs;
use std::path::{Path, PathBuf};

const PIPELINE_PATH: &str = "app/pipeline.yaml";

fn template_dir() -> PathBuf {
    // crates/clan-sdk → repo root → app/templates/brief-maker
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .join("app/templates/brief-maker")
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let dir = template_dir();
    let output = std::env::args()
        .nth(1)
        .unwrap_or_else(|| "brief-maker.app.clan".to_string());

    let schema = fs::read_to_string(dir.join("schema.json"))?;
    let index_html = fs::read_to_string(dir.join("index.html"))?;
    let requirements = fs::read_to_string(dir.join("agent/requirements.yaml"))?;
    let pipeline = fs::read_to_string(dir.join("app/pipeline.yaml"))?;

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

    // 4. Pipeline contract as a registered member at app/pipeline.yaml.
    let mut builder = ClanBuilder::new(clan.manifest().clone());
    for (path, bytes) in clan.read_all_entries()? {
        builder.add_entry(path, bytes);
    }
    builder.add_entry(PIPELINE_PATH, pipeline.into_bytes());
    if clan.manifest().file_by_path(PIPELINE_PATH).is_none() {
        builder.manifest_mut().files.push(FileEntry {
            id: "pipeline-contract".into(),
            path: PIPELINE_PATH.into(),
            role: "pipeline-contract".into(),
            content_type: "application/yaml".into(),
            priority: None,
            sha256: None,
        });
    }
    let clan = ClanFile::from_bytes(builder.build()?)?;

    // 5. Stamp the app block and flip to a template.
    let template_bytes = make_template(
        &clan,
        AppInfo {
            name: "Brief Maker".into(),
            app_id: "ie.napkin.brief-maker".into(),
            version: "0.2.0".into(),
            icon: None,
            entry: "human/index.html".into(),
            schema: Some("agent/output-schema.json".into()),
            prompt_templates: vec![],
            data_seed: None,
        },
        MakeTemplateOptions::default(),
    )?;
    let template = ClanFile::from_bytes(template_bytes.clone())?;

    // 6. Self-check: the pipeline contract must travel into every instance.
    let instance = ClanFile::from_bytes(instantiate(
        &template,
        InstantiateOptions {
            title: "smoke-instance".into(),
            ..Default::default()
        },
    )?)?;
    for path in ["agent/requirements.yaml", PIPELINE_PATH] {
        assert!(
            instance.has_entry(path),
            "{path} did not travel into the instance"
        );
    }
    for (label, file) in [("template", &template), ("instance", &instance)] {
        let report = validate(file);
        if !report.is_valid() {
            eprintln!("[warn] {label} validation: {report:?}");
        }
    }

    fs::write(&output, &template_bytes)?;
    println!(
        "wrote {output} ({} bytes) — requirements + pipeline contract verified in instance",
        template_bytes.len()
    );
    println!("install: copy into the Napkin Studio app library as ie.napkin.brief-maker/app.clan");
    Ok(())
}
