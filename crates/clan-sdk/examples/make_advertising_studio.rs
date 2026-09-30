//! Package `app/templates/advertising-studio/` into an Advertising Studio
//! template app.clan using the SDK alone.
//!
//!     cargo run -p clan-sdk --example make_advertising_studio -- [output.clan]
//!
//! Same shape as `make_brief_maker`, with two differences that matter:
//!
//! 1. The `AppInfo` declares a `spinoff` contract, so a finished Brief Maker
//!    document can be branched into a production with its data grafted under
//!    `brief:` and its decisions carried across. The *target* declares this
//!    because the target is what knows its own schema.
//! 2. `make_template` replaces `agent/context.md` with generic app boilerplate,
//!    so the studio's own context — the non-negotiable production rules — is
//!    reinstalled afterwards. Skipping this silently ships an app whose agents
//!    have never been told that approved revisions are never overwritten.

use clan_sdk::{
    create, instantiate, make_template, pack_html, patch_context, patch_requirements, spinoff,
    validate, AppInfo, ClanBuilder, ClanFile, CreateOptions, FileEntry, InstantiateOptions,
    MakeTemplateOptions, SpinoffOptions, SpinoffSpec,
};
use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

const PIPELINE_PATH: &str = "app/pipeline.yaml";

fn template_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .join("app/templates/advertising-studio")
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let dir = template_dir();
    let output = std::env::args()
        .nth(1)
        .unwrap_or_else(|| "advertising-studio.app.clan".to_string());

    let schema = fs::read_to_string(dir.join("schema.json"))?;
    let index_html = fs::read_to_string(dir.join("index.html"))?;
    let requirements = fs::read_to_string(dir.join("agent/requirements.yaml"))?;
    let pipeline = fs::read_to_string(dir.join("app/pipeline.yaml"))?;
    let context = fs::read_to_string(dir.join("context.md"))?;

    // 1. Base document, seeded with the production-record schema.
    let base = create(CreateOptions {
        title: "Advertising Studio".into(),
        brief: "Template app: film and advertising production on the napkin production pipeline"
            .into(),
        document_type: None,
        no_render: false,
        schema: Some(schema),
    })?;
    let clan = ClanFile::from_bytes(base)?;

    // 2. The app's UI is the human view.
    let clan = ClanFile::from_bytes(pack_html(&clan, &index_html, None, None, None, None)?)?;

    // 3. Capability requirements (spec §22).
    let clan = ClanFile::from_bytes(patch_requirements(&clan, &requirements)?)?;

    // 4. Pipeline contract as a registered member.
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

    // 5. Stamp the app block, declare the spin-off contract, flip to template.
    let mut lift = BTreeMap::new();
    lift.insert("project_name".to_string(), "project.name".to_string());
    lift.insert("client".to_string(), "project.client".to_string());
    let template_bytes = make_template(
        &clan,
        AppInfo {
            home: None,
            name: "Advertising Studio".into(),
            app_id: "ie.napkin.film".into(),
            version: "0.1.0".into(),
            icon: None,
            entry: "human/index.html".into(),
            schema: Some("agent/output-schema.json".into()),
            prompt_templates: vec![],
            data_seed: None,
            spinoff: Some(SpinoffSpec {
                accepts: vec!["ie.napkin.brief".into(), "ie.napkin.brief-maker".into()],
                map: Some("brief".into()),
                lift,
                pin_source_decisions: true,
                upstream: false,
            }),
        },
        MakeTemplateOptions::default(),
    )?;

    // 6. Reinstall the studio's own context — make_template replaced it.
    let template = ClanFile::from_bytes(template_bytes)?;
    let template_bytes = patch_context(&template, &context, false)?;
    let template = ClanFile::from_bytes(template_bytes.clone())?;

    // 7. Self-checks. Contracts must travel into a plain instance...
    let instance = ClanFile::from_bytes(instantiate(
        &template,
        InstantiateOptions {
            title: "smoke-instance".into(),
            ..Default::default()
        },
    )?)?;
    for path in ["agent/requirements.yaml", PIPELINE_PATH, "agent/context.md"] {
        assert!(
            instance.has_entry(path),
            "{path} did not travel into the instance"
        );
    }
    assert!(
        instance
            .read_entry_string("agent/context.md")?
            .contains("never overwrites an approved one"),
        "the studio's production rules were lost — make_template's boilerplate won"
    );

    // ...and a spun-off document must land the source's data where the app
    // says it goes, with the source's reasoning still attached.
    let source = ClanFile::from_bytes(instantiate(
        &brief_maker_stub()?,
        InstantiateOptions {
            title: "smoke-brief".into(),
            fresh_data: false,
            ..Default::default()
        },
    )?)?;
    let spun = ClanFile::from_bytes(spinoff(&template, &source, SpinoffOptions::default())?)?;
    let data: serde_yaml::Value = serde_yaml::from_slice(&spun.read_entry("shared/data.yaml")?)?;
    assert_eq!(
        data["brief"]["insight"],
        serde_yaml::Value::from("people forget"),
        "the brief did not land under `brief:`"
    );
    assert_eq!(
        data["project"]["name"],
        serde_yaml::Value::from("Smoke Co"),
        "project_name did not lift to project.name"
    );

    for (label, file) in [("template", &template), ("instance", &instance)] {
        let report = validate(file);
        if !report.is_valid() {
            eprintln!("[warn] {label} validation: {report:?}");
        }
    }

    fs::write(&output, &template_bytes)?;
    println!(
        "wrote {output} ({} bytes) — context, requirements, pipeline and spin-off graft verified",
        template_bytes.len()
    );
    println!("install: copy into the Napkin Studio app library as ie.napkin.film/app.clan");
    Ok(())
}

/// A minimal Brief Maker template, so the spin-off self-check exercises the
/// real path without depending on a document that happens to be on this disk.
fn brief_maker_stub() -> Result<ClanFile, Box<dyn std::error::Error>> {
    let base = create(CreateOptions {
        title: "Brief Maker".into(),
        brief: "smoke".into(),
        document_type: None,
        no_render: false,
        schema: None,
    })?;
    let clan = ClanFile::from_bytes(base)?;
    let mut builder = ClanBuilder::new(clan.manifest().clone());
    for (path, bytes) in clan.read_all_entries()? {
        if path == "shared/data.yaml" {
            continue;
        }
        builder.add_entry(path, bytes);
    }
    builder.add_entry(
        "shared/data.yaml",
        b"project_name: Smoke Co\ninsight: people forget\n".to_vec(),
    );
    let clan = ClanFile::from_bytes(builder.build()?)?;
    let bytes = make_template(
        &clan,
        AppInfo {
            home: None,
            name: "Brief Maker".into(),
            app_id: "ie.napkin.brief".into(),
            version: "1.0.0".into(),
            icon: None,
            entry: "human/index.html".into(),
            schema: None,
            prompt_templates: vec![],
            data_seed: None,
            spinoff: None,
        },
        MakeTemplateOptions::default(),
    )?;
    Ok(ClanFile::from_bytes(bytes)?)
}
