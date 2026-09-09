// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! The briefing prompt, split the way the model is billed.
//!
//! Two halves. The **system** half — framing, the output schema, the craft
//! rules, the knowledge digests — is byte-identical on every call, so it sits
//! behind a cache breakpoint and is paid for once. The **user** half carries
//! everything that changes: the brief so far, the decision history, what the
//! human just typed. Put one byte of the second in the first and the cache is
//! gone, which is most of what a draft costs.
//!
//! This lives in the host rather than in a backend because every shell needs
//! it: the desktop proxies inference, a server proxies it, and a browser makes
//! the call itself with the user's own key. One copy, no drift.

use serde_json::Value;

/// Paraphrased pattern notes per pack, written offline by
/// `engine/scripts/distil_pack.py` and committed. Embedded rather than read
/// from disk: a browser has no `engine/packs_dist` to read.
const DIGESTS: &[(&str, &str)] = &[
    (
        "briefing-template",
        include_str!("../../../engine/packs_dist/briefing-template/digest.md"),
    ),
    (
        "cannes",
        include_str!("../../../engine/packs_dist/cannes/digest.md"),
    ),
    (
        "dandad",
        include_str!("../../../engine/packs_dist/dandad/digest.md"),
    ),
    (
        "ipa",
        include_str!("../../../engine/packs_dist/ipa/digest.md"),
    ),
    (
        "playbooks",
        include_str!("../../../engine/packs_dist/playbooks/digest.md"),
    ),
];

/// The two halves of a request, ready to send.
#[derive(Debug, Clone, serde::Serialize)]
pub struct AgentPrompt {
    /// Cacheable. Identical for every call against the same document type.
    pub system: String,
    /// Volatile. Everything that differs call to call.
    pub user: String,
}

fn digests() -> String {
    DIGESTS
        .iter()
        .filter(|(_, text)| !text.trim().is_empty())
        .map(|(name, text)| format!("### {name}\n{}", text.trim()))
        .collect::<Vec<_>>()
        .join("\n\n")
}

fn pretty(v: Option<&Value>) -> String {
    serde_json::to_string_pretty(v.unwrap_or(&Value::Null)).unwrap_or_else(|_| "null".into())
}

fn str_of<'a>(v: &'a Value, key: &str) -> &'a str {
    v.get(key).and_then(Value::as_str).unwrap_or("")
}

/// Take at most `n` characters — never bytes, or a multi-byte character at the
/// boundary would panic.
fn clamp(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

/// Build the prompt for one call.
///
/// `payload` is what the app sent (`task`, `field`, `input`, `attachments`);
/// `clan` is the document's own intelligence layer as
/// [`crate::session::Session::clan_context_for_agent`] assembles it.
/// `grounding` is retrieved craft rules, if any — volatile, so it goes in the
/// user half where it cannot disturb the cached prefix.
pub fn build(payload: &Value, clan: &Value, grounding: &str) -> AgentPrompt {
    let task = payload
        .get("task")
        .and_then(Value::as_str)
        .unwrap_or("draft_brief");
    let field = payload.get("field").and_then(Value::as_str).unwrap_or("");
    let user_input = str_of(payload, "input");
    let data = clan.get("data").cloned().unwrap_or(Value::Null);
    let current = pretty(Some(&data));
    let context = str_of(clan, "context");

    let mut system = String::with_capacity(20_000);
    system.push_str("You are an expert advertising strategist working on a creative brief.\n");
    system.push_str("Fields you may fill (JSON Schema):\n");
    system.push_str(&pretty(clan.get("schema")));
    system.push('\n');
    system.push_str(
        "Rules: `objectives` and `desired_response` are nested objects; \
         `reasons_to_believe`, `tone_and_world`, `mandatories`, `open_questions` are arrays \
         of strings. Output a single JSON object — no markdown fences, no commentary.\n",
    );
    let packs = digests();
    if !packs.is_empty() {
        system.push_str(
            "=====\n\
             KNOWLEDGE DIGESTS — paraphrased patterns from award-effectiveness corpora \
             (IPA, Cannes, D&AD) and planning playbooks. Let these shape the insight, \
             proposition and reasons-to-believe: prefer a named mechanism over a generic \
             claim, obey the craft rules, avoid the traps. Never copy their wording.\n",
        );
        system.push_str(&packs);
        system.push('\n');
    }
    system.push_str("=====\n");

    let user = if task == "regenerate_field" && !field.is_empty() {
        // One field, rewritten. Deliberately lean: this is the app's fastest
        // interaction and does not need the whole history to do its job.
        let mut u = format!(
            "BRIEF SO FAR:\n{current}\n\nGUIDANCE / NOTES:\n{}\n\n\
             TASK: Rewrite ONLY \"{field}\" — sharper and consistent with the brief.",
            clamp(context, 600),
        );
        if !user_input.is_empty() {
            u.push_str(&format!(" Extra instruction from the human: {user_input}"));
        }
        u.push('\n');
        u.push_str(&format!(
            "Output ONLY: {{ \"{field}\": <value>, \"rationale\": \"<= 20 words\" }}"
        ));
        u
    } else {
        let empty = Vec::new();
        let attachments = payload
            .get("attachments")
            .and_then(Value::as_array)
            .or_else(|| data.get("reference_assets").and_then(Value::as_array))
            .unwrap_or(&empty);

        let mut lines = Vec::new();
        for a in attachments {
            let label = a
                .get("label")
                .and_then(Value::as_str)
                .or_else(|| a.get("name").and_then(Value::as_str))
                .unwrap_or("");
            lines.push(format!("  - {label} ({})", str_of(a, "type")));
            let text = str_of(a, "extracted_text").trim();
            if !text.is_empty() {
                lines.push(format!("    ┌─ extracted content of {label} ─"));
                for ln in text.lines() {
                    lines.push(format!("    │ {ln}"));
                }
                lines.push("    └─".to_string());
            }
        }
        let attach_block = if lines.is_empty() {
            "  (none)".to_string()
        } else {
            lines.join("\n")
        };

        let locked = data
            .get("locked_fields")
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(Value::as_str)
                    .collect::<Vec<_>>()
                    .join(", ")
            })
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| "(none)".into());

        // An immersive brief also asks for a brand palette, which the app reads
        // back through the trusted set-theme capability.
        let theme_line = if data.get("brief_style").and_then(Value::as_str) == Some("immersive") {
            "  - \"theme\": { \"bg\": \"#RRGGBB\", \"accent\": \"#RRGGBB\", \"text\": \"#RRGGBB\" } \
             — a brand palette expressing tone_and_world/mood. bg and text MUST be \
             strongly contrasting (one dark, one light) for legibility; accent is a \
             vivid on-brand hue. Optionally add \"font\": \"serif\"|\"sans\"|\"mono\".\n"
        } else {
            ""
        };

        format!(
            "{grounding}PURPOSE / CONTEXT:\n{context}\n\n\
             CURRENT DATA (keep good values):\n{current}\n\n\
             DECISION HISTORY:\n{}\n\n\
             CLIENT INPUT (messy brief):\n{user_input}\n\n\
             ATTACHED REFERENCE FILES:\n{attach_block}\n\n\
             LOCKED FIELDS — do NOT change these, keep their current values: {locked}\n\n\
             TASK: Draft the full creative brief from the client input AND the \
             extracted content of the attached reference files above.\n\
             Output a JSON object with the schema's top-level keys, PLUS:\n\
             \x20 - \"rationale\": <= 2 sentences on the key choices.\n\
             \x20 - \"context\": a SHORT markdown brief for the next agent (client, requirement, \
             audience, key decisions) — <= 120 words.\n\
             {theme_line}\
             Keep values tight. Use \"\" or [] only when genuinely unknowable.",
            serde_json::to_string(clan.get("decision_chain").unwrap_or(&Value::Null))
                .unwrap_or_else(|_| "null".into()),
        )
    };

    AgentPrompt { system, user }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn clan() -> Value {
        json!({
            "schema": { "type": "object", "properties": { "proposition": { "type": "string" } } },
            "data": { "project_name": "Cider" },
            "decision_chain": { "decisions": [] },
            "context": "a heritage cider brand",
        })
    }

    // The whole point of the split: the expensive half must not move between
    // calls, or it is never read from cache.
    #[test]
    fn the_system_half_is_identical_across_calls() {
        let draft = build(
            &json!({ "task": "draft_brief", "input": "one" }),
            &clan(),
            "",
        );
        let other = build(
            &json!({ "task": "draft_brief", "input": "totally different" }),
            &clan(),
            "",
        );
        let regen = build(
            &json!({ "task": "regenerate_field", "field": "proposition" }),
            &clan(),
            "",
        );

        assert_eq!(draft.system, other.system);
        assert_eq!(
            draft.system, regen.system,
            "a regenerate must reuse the draft's prefix"
        );
        assert_ne!(
            draft.user, other.user,
            "the volatile half must carry the difference"
        );
    }

    #[test]
    fn the_digests_ride_the_cached_half() {
        let p = build(&json!({}), &clan(), "");
        assert!(p.system.contains("KNOWLEDGE DIGESTS"));
        assert!(
            p.system.len() > 13_000,
            "all five packs: {}",
            p.system.len()
        );
        assert!(!p.user.contains("KNOWLEDGE DIGESTS"));
    }

    // Retrieved rules change per call, so they belong with the volatile half.
    #[test]
    fn grounding_never_touches_the_cached_half() {
        let with = build(&json!({}), &clan(), "RULES: be specific\n\n");
        let without = build(&json!({}), &clan(), "");
        assert_eq!(with.system, without.system);
        assert!(with.user.starts_with("RULES: be specific"));
    }

    #[test]
    fn a_regenerate_asks_for_one_field_only() {
        let p = build(
            &json!({ "task": "regenerate_field", "field": "proposition", "input": "punchier" }),
            &clan(),
            "",
        );
        assert!(p.user.contains(r#"Rewrite ONLY "proposition""#));
        assert!(p
            .user
            .contains("Extra instruction from the human: punchier"));
        assert!(
            !p.user.contains("DECISION HISTORY"),
            "a regenerate stays lean"
        );
    }

    #[test]
    fn attachment_text_is_carried_into_the_draft() {
        let payload = json!({
            "task": "draft_brief",
            "attachments": [{ "name": "brief.pdf", "type": "pdf",
                              "extracted_text": "line one\nline two" }],
        });
        let p = build(&payload, &clan(), "");
        assert!(p.user.contains("brief.pdf (pdf)"));
        assert!(p.user.contains("│ line one"));
        assert!(p.user.contains("│ line two"));
    }

    #[test]
    fn locked_fields_and_immersive_theme_are_announced() {
        let mut c = clan();
        c["data"] = json!({ "locked_fields": ["proposition"], "brief_style": "immersive" });
        let p = build(&json!({}), &c, "");
        assert!(p.user.contains("keep their current values: proposition"));
        assert!(p.user.contains("\"theme\""));

        let plain = build(&json!({}), &clan(), "");
        assert!(plain.user.contains("keep their current values: (none)"));
        assert!(!plain.user.contains("\"theme\""));
    }

    // Truncation is by character, not byte: the notes field is free text.
    #[test]
    fn multibyte_context_does_not_panic_at_the_cut() {
        let mut c = clan();
        c["context"] = json!("é".repeat(900));
        let p = build(&json!({ "task": "regenerate_field", "field": "x" }), &c, "");
        assert_eq!(p.user.matches('é').count(), 600);
    }
}
