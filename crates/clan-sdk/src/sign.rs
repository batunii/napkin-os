// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! App signing (spec §29, v1.2) — the trust gate for granting a CLAN app
//! direct host access in Napkin Studio OS.
//!
//! Trust is asymmetric and key-based, NOT hash-pinned: the publisher holds a
//! private ed25519 key and signs each app; the viewer ships only the public
//! key and verifies. New apps and updates signed with the same key verify
//! automatically — nothing in the viewer changes. Because the viewer (and its
//! embedded public key) is open source, this is safe: forging a signature
//! still requires the private key.
//!
//! The signature covers the app's **code** — the presentation entry, optional
//! stylesheet, the data schema — plus the app **identity** (`app_id` +
//! `version`). It deliberately excludes the data layer and decision chain, so
//! human edits and forks/instantiation (which copy code verbatim and carry the
//! signature) never invalidate it. Only tampering with the app code or its
//! identity breaks trust.

use base64::Engine;
use ed25519_dalek::{Signature as EdSignature, Signer, SigningKey, Verifier, VerifyingKey};

use crate::container::{ClanBuilder, ClanFile, MANIFEST_PATH};
use crate::error::{Error, Result};
use crate::hash::sha256_hex;
use crate::manifest::Signature;

const SIG_DOMAIN: &str = "napkin-app-sig-v1";

/// Candidate code entries, in canonical (sorted) order. Only those present in
/// the archive are signed.
const CODE_ENTRIES: &[&str] = &[
    "agent/output-schema.json",
    "human/index.html",
    "human/styles.css",
];

fn b64() -> base64::engine::general_purpose::GeneralPurpose {
    base64::engine::general_purpose::STANDARD
}

/// The exact paths [`sign_app`] would cover for this archive (present subset of
/// [`CODE_ENTRIES`], canonical order).
fn signed_paths(clan: &ClanFile) -> Vec<String> {
    CODE_ENTRIES
        .iter()
        .filter(|p| clan.has_entry(p))
        .map(|p| p.to_string())
        .collect()
}

/// Build the canonical message that gets signed/verified: a domain tag, the
/// app identity, and a content hash per signed path — order fixed by `paths`.
fn signing_message(clan: &ClanFile, paths: &[String]) -> Result<Vec<u8>> {
    let app = clan
        .manifest()
        .app
        .as_ref()
        .ok_or_else(|| Error::Signing("file has no app block — only apps are signed".into()))?;
    let mut msg = String::new();
    msg.push_str(SIG_DOMAIN);
    msg.push('\n');
    msg.push_str(&format!("app_id:{}\n", app.app_id));
    msg.push_str(&format!("version:{}\n", app.version));
    for p in paths {
        let bytes = clan
            .read_entry(p)
            .map_err(|e| Error::Signing(format!("cannot read signed entry {p}: {e}")))?;
        msg.push_str(&format!("{p}:{}\n", sha256_hex(&bytes)));
    }
    Ok(msg.into_bytes())
}

/// Generate a fresh ed25519 keypair. Returns `(private_seed_b64, public_b64)`.
/// Keep the private seed secret (never ship it); embed the public key in the
/// viewer.
pub fn generate_keypair() -> (String, String) {
    let signing = SigningKey::generate(&mut rand_core::OsRng);
    let verifying = signing.verifying_key();
    (
        b64().encode(signing.to_bytes()),
        b64().encode(verifying.to_bytes()),
    )
}

fn signing_key_from_b64(seed_b64: &str) -> Result<SigningKey> {
    let bytes = b64()
        .decode(seed_b64.trim())
        .map_err(|e| Error::Signing(format!("invalid private key base64: {e}")))?;
    let arr: [u8; 32] = bytes
        .try_into()
        .map_err(|_| Error::Signing("private key must be 32 bytes".into()))?;
    Ok(SigningKey::from_bytes(&arr))
}

fn verifying_key_from_b64(pub_b64: &str) -> Result<VerifyingKey> {
    let bytes = b64()
        .decode(pub_b64.trim())
        .map_err(|e| Error::Signing(format!("invalid public key base64: {e}")))?;
    let arr: [u8; 32] = bytes
        .try_into()
        .map_err(|_| Error::Signing("public key must be 32 bytes".into()))?;
    VerifyingKey::from_bytes(&arr).map_err(|e| Error::Signing(format!("invalid public key: {e}")))
}

/// Sign an app's code with the publisher's private key (base64 seed) and return
/// a new archive carrying the signature in its manifest.
pub fn sign_app(
    clan: &ClanFile,
    private_seed_b64: &str,
    key_id: Option<String>,
) -> Result<Vec<u8>> {
    let signing = signing_key_from_b64(private_seed_b64)?;
    let paths = signed_paths(clan);
    if paths.is_empty() {
        return Err(Error::Signing(
            "nothing to sign — app has no code entries (human/index.html, schema)".into(),
        ));
    }
    let msg = signing_message(clan, &paths)?;
    let sig = signing.sign(&msg);

    let mut manifest = clan.manifest().clone();
    manifest.signature = Some(Signature {
        sig: b64().encode(sig.to_bytes()),
        signed: paths,
        key_id,
    });

    let mut builder = ClanBuilder::new(manifest);
    for (path, bytes) in clan.read_all_entries()? {
        if path == MANIFEST_PATH {
            continue;
        }
        builder.add_entry(path, bytes);
    }
    builder.build()
}

/// Verify an app's signature against a publisher public key (base64). Returns
/// `true` only if the file carries a signature that validates over its current
/// code and identity.
pub fn verify_app(clan: &ClanFile, public_key_b64: &str) -> bool {
    let Ok(verifying) = verifying_key_from_b64(public_key_b64) else {
        return false;
    };
    let Some(sig_block) = &clan.manifest().signature else {
        return false;
    };
    let Ok(sig_bytes) = b64().decode(sig_block.sig.trim()) else {
        return false;
    };
    let Ok(sig_arr) = <[u8; 64]>::try_from(sig_bytes.as_slice()) else {
        return false;
    };
    let signature = EdSignature::from_bytes(&sig_arr);
    let Ok(msg) = signing_message(clan, &sig_block.signed) else {
        return false;
    };
    verifying.verify(&msg, &signature).is_ok()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::create::{create, CreateOptions};
    use crate::instantiate::{instantiate, make_template, InstantiateOptions, MakeTemplateOptions};
    use crate::manifest::AppInfo;
    use crate::patch::apply_patch_and_repack;

    fn template() -> ClanFile {
        let bytes = create(CreateOptions {
            title: "Brief Maker".into(),
            brief: "x".into(),
            document_type: None,
            no_render: false,
            schema: None,
        })
        .unwrap();
        let clan = ClanFile::from_bytes(bytes).unwrap();
        let app = AppInfo {
            name: "Brief Maker".into(),
            app_id: "ie.napkin.brief".into(),
            version: "1.0.0".into(),
            icon: None,
            entry: "human/index.html".into(),
            schema: Some("agent/output-schema.json".into()),
            prompt_templates: vec![],
            data_seed: None,
        };
        ClanFile::from_bytes(make_template(&clan, app, MakeTemplateOptions::default()).unwrap())
            .unwrap()
    }

    #[test]
    fn sign_then_verify_roundtrips() {
        let (priv_b64, pub_b64) = generate_keypair();
        let tpl = template();
        let signed =
            ClanFile::from_bytes(sign_app(&tpl, &priv_b64, Some("napkin".into())).unwrap())
                .unwrap();
        assert!(verify_app(&signed, &pub_b64), "valid signature must verify");
        assert!(signed.manifest().signature.is_some());
    }

    #[test]
    fn wrong_key_fails() {
        let (priv_b64, _) = generate_keypair();
        let (_, other_pub) = generate_keypair();
        let signed = ClanFile::from_bytes(sign_app(&template(), &priv_b64, None).unwrap()).unwrap();
        assert!(
            !verify_app(&signed, &other_pub),
            "a different key must NOT verify"
        );
    }

    #[test]
    fn unsigned_file_does_not_verify() {
        let (_, pub_b64) = generate_keypair();
        assert!(!verify_app(&template(), &pub_b64));
    }

    #[test]
    fn tampering_with_code_breaks_signature() {
        let (priv_b64, pub_b64) = generate_keypair();
        let signed = ClanFile::from_bytes(sign_app(&template(), &priv_b64, None).unwrap()).unwrap();
        assert!(verify_app(&signed, &pub_b64));
        // Rewrite the signed presentation entry → signature must fail.
        let mut b = ClanBuilder::new(signed.manifest().clone());
        for (p, by) in signed.read_all_entries().unwrap() {
            if p == "human/index.html" {
                continue;
            }
            b.add_entry(p, by);
        }
        b.add_entry("human/index.html", b"<h1>evil</h1>".to_vec());
        let tampered = ClanFile::from_bytes(b.build().unwrap()).unwrap();
        assert!(
            !verify_app(&tampered, &pub_b64),
            "tampered code must NOT verify"
        );
    }

    #[test]
    fn signature_survives_instantiate_and_data_edits() {
        let (priv_b64, pub_b64) = generate_keypair();
        let tpl = ClanFile::from_bytes(sign_app(&template(), &priv_b64, None).unwrap()).unwrap();

        // Forking to a working instance must preserve trust (code is copied).
        let inst = ClanFile::from_bytes(instantiate(&tpl, InstantiateOptions::default()).unwrap())
            .unwrap();
        assert!(
            verify_app(&inst, &pub_b64),
            "instance of a signed app must stay verified"
        );

        // A human edit to the data/patches layer must NOT break the signature.
        let edited = ClanFile::from_bytes(
            apply_patch_and_repack(&inst, "heading-0".into(), "Edited".into()).unwrap(),
        )
        .unwrap();
        assert!(
            verify_app(&edited, &pub_b64),
            "data/patch edits must not affect code signature"
        );
    }
}
