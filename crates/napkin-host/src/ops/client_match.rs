// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! Which parts a client's words were about, found by the host without a
//! model (Contract 4 §7.5.2a, owner 2026-09-30). It replaced the middleware's
//! `find_client_parts`: a second model call for a question a word match
//! answers well enough, when a person confirms every suggestion anyway.
//!
//! Deterministic: the same words and parts give the same suggestions, in the
//! same order — the parts' order as the app declared them. Nothing here reads
//! a part's value, so nothing leaves the document and nothing is withheld.
//!
//! 1. **Sentences.** [`sentences`] splits the proof by a fixed rule: a
//!    sentence ends after a run of `.` `!` `?` `…`, with any closing quote or
//!    bracket after it, when whitespace or the end of the text follows; and
//!    at a line break followed by a blank line, by a list bullet (`-`, `*`,
//!    `•`, `–`, `—`, then whitespace) or by a numbered item (one to three
//!    digits, then `.` or `)`, then whitespace). A numbered item's marker
//!    starts its sentence, as a bullet does, and does not end one. Each
//!    sentence is the proof's own text, trimmed of the whitespace around it.
//! 2. **Names.** A part is named by its label, each alias the app declared
//!    (`data-clan-part-aliases="a, b"`), and the last word of its label — in
//!    that order, repeats dropped ([`names`]).
//! 3. **Match.** A sentence matches a part when it holds one of its names as
//!    whole words, ignoring case: lower-cased, every run of whitespace one
//!    space, `’` read as `'` and Unicode NFC, on both sides (so an accent
//!    typed as one character or as two matches); the characters either side of
//!    the name, if any, are not letters or digits. A part takes the first
//!    sentence that matches it; one sentence may match several parts.
//! 4. **Answer.** The document `rejected`: `rejected`. Otherwise
//!    (`accepted_with_changes`): `accepted_with_changes` when the sentence
//!    holds a [`CHANGE_CUES`] word by the same whole-word rule, else
//!    `accepted`. An `accepted` document asks for no suggestions.
//! 5. **Quote.** The matched sentence as it appears in the proof, its first
//!    [`MAX_QUOTE`] characters when longer — still a verbatim substring.

/// The handler a suggestion is recorded under, with `actor: process:host`.
pub const HANDLER: &str = "client_parts_match@1";
/// The process it runs as.
pub const PROCESS: &str = "host";

/// Words that ask for a change (item 4), whole words, ignoring case.
pub const CHANGE_CUES: &[&str] = &[
    "wrong", "flat", "change", "not", "instead", "rather", "too", "should", "don't", "doesn't", "less", "more",
    "prefer", "missing", "remove", "replace",
];

pub const MAX_QUOTE: usize = 500;

/// One part as the matcher sees it: never its value.
#[derive(Debug, Clone)]
pub struct Part<'a> {
    pub label: &'a str,
    pub aliases: &'a [String],
}

/// One suggestion.
#[derive(Debug, Clone, PartialEq)]
pub struct Found {
    /// Index into the parts given.
    pub part: usize,
    pub answer: &'static str,
    /// Verbatim from the proof.
    pub quote: String,
    /// The name that matched, as the part declares it.
    pub name: String,
    /// The change cue that set `accepted_with_changes`, when one did.
    pub cue: Option<&'static str>,
}

/// The byte length of a numbered item's marker (`12.` or `3)`) at the start
/// of `s`, when whitespace follows it.
fn numbered(s: &str) -> Option<usize> {
    let digits = s.bytes().take_while(u8::is_ascii_digit).count();
    if !(1..=3).contains(&digits) {
        return None;
    }
    let mut rest = s[digits..].chars();
    match (rest.next(), rest.next()) {
        (Some('.' | ')'), Some(w)) if w.is_whitespace() => Some(digits + 1),
        _ => None,
    }
}

/// The sentences of `text`, by item 1: slices of `text`, trimmed, in order,
/// none empty.
pub fn sentences(text: &str) -> Vec<&str> {
    const END: &[char] = &['.', '!', '?', '…'];
    const CLOSE: &[char] = &['"', '\'', '”', '’', ')', ']', '»'];
    const BULLET: &[char] = &['-', '*', '•', '–', '—'];
    let mut out = Vec::new();
    let mut start = 0;
    fn push<'t>(text: &'t str, from: usize, to: usize, out: &mut Vec<&'t str>) {
        let s = text[from..to].trim();
        if !s.is_empty() {
            out.push(s);
        }
    }
    // A numbered item's marker is read past, so its `.` ends nothing.
    let lead = text.len() - text.trim_start().len();
    let mut skip_until = numbered(&text[lead..]).map_or(0, |n| lead + n);
    let mut it = text.char_indices().peekable();
    while let Some((i, c)) = it.next() {
        if i < skip_until {
            continue;
        }
        if END.contains(&c) {
            let mut end = i + c.len_utf8();
            while let Some(&(j, n)) = it.peek() {
                if END.contains(&n) || CLOSE.contains(&n) {
                    end = j + n.len_utf8();
                    it.next();
                } else {
                    break;
                }
            }
            if it.peek().map_or(true, |&(_, n)| n.is_whitespace()) {
                push(text, start, end, &mut out);
                start = end;
            }
        } else if c == '\n' {
            let rest = text[i + 1..].trim_start_matches([' ', '\t', '\r']);
            let mut chars = rest.chars();
            let marker = numbered(rest);
            let breaks = match chars.next() {
                Some('\n') => true,
                Some(b) if BULLET.contains(&b) => chars.next().is_some_and(char::is_whitespace),
                _ => marker.is_some(),
            };
            if let Some(n) = marker {
                skip_until = text.len() - rest.len() + n;
            }
            if breaks {
                push(text, start, i, &mut out);
                start = i + 1;
            }
        }
    }
    push(text, start, text.len(), &mut out);
    out
}

/// Lower-cased, every run of whitespace one space, `’` read as `'`, trimmed,
/// in Unicode NFC.
fn fold(s: &str) -> String {
    use unicode_normalization::UnicodeNormalization;
    s.split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .to_lowercase()
        .replace('’', "'")
        .nfc()
        .collect()
}

/// `needle` (folded) in `hay` (folded) as whole words.
fn holds(hay: &str, needle: &str) -> bool {
    if needle.is_empty() {
        return false;
    }
    let mut from = 0;
    while let Some(at) = hay[from..].find(needle) {
        let at = from + at;
        let end = at + needle.len();
        let before = hay[..at].chars().next_back();
        let after = hay[end..].chars().next();
        if !before.is_some_and(char::is_alphanumeric) && !after.is_some_and(char::is_alphanumeric) {
            return true;
        }
        from = at + hay[at..].chars().next().map_or(1, char::len_utf8);
    }
    false
}

/// A part's names (item 2): its label, its aliases, the last word of its
/// label — trimmed, the last word stripped of anything but letters and
/// digits at its ends, repeats (by the folded form) dropped.
pub fn names(part: &Part) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    let last = part
        .label
        .split_whitespace()
        .next_back()
        .map(|w| w.trim_matches(|c: char| !c.is_alphanumeric()).to_string());
    let all = std::iter::once(part.label.trim().to_string())
        .chain(part.aliases.iter().map(|a| a.trim().to_string()))
        .chain(last);
    for n in all {
        if !n.is_empty() && !out.iter().any(|o| fold(o) == fold(&n)) {
            out.push(n);
        }
    }
    out
}

/// The suggestions for a document answer `answer` (`rejected` or
/// `accepted_with_changes`; anything else gives none) over `proof`, one per
/// part at most, in the parts' order.
pub fn suggest(answer: &str, proof: &str, parts: &[Part]) -> Vec<Found> {
    if !matches!(answer, "rejected" | "accepted_with_changes") {
        return Vec::new();
    }
    let said: Vec<(&str, String)> = sentences(proof).into_iter().map(|s| (s, fold(s))).collect();
    let mut out = Vec::new();
    for (i, part) in parts.iter().enumerate() {
        let names = names(part);
        let hit = said.iter().find_map(|(s, folded)| {
            names.iter().find(|n| holds(folded, &fold(n))).map(|n| (*s, folded, n.clone()))
        });
        let Some((sentence, folded, name)) = hit else { continue };
        let cue = CHANGE_CUES.iter().copied().find(|c| holds(folded, c));
        let answer = match (answer, cue) {
            ("rejected", _) => "rejected",
            (_, Some(_)) => "accepted_with_changes",
            _ => "accepted",
        };
        out.push(Found {
            part: i,
            answer,
            quote: sentence.chars().take(MAX_QUOTE).collect(),
            name,
            cue: if answer == "rejected" { None } else { cue },
        });
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn part<'a>(label: &'a str, aliases: &'a [String]) -> Part<'a> {
        Part { label, aliases }
    }

    #[test]
    fn sentences_end_at_terminal_punctuation_blank_lines_and_bullets() {
        let text = "Honestly?  This isn't the brief.\nIt wraps\nhere. “Too loud!” she said…\n\nNew para\n- the tone\n* the audience.\n3.5 stays whole, e.g. too";
        assert_eq!(
            sentences(text),
            vec![
                "Honestly?",
                "This isn't the brief.",
                "It wraps\nhere.",
                "“Too loud!”",
                "she said…",
                "New para",
                "- the tone",
                "* the audience.",
                "3.5 stays whole, e.g.",
                "too",
            ]
        );
        assert!(sentences("  \n\n ").is_empty());
        for s in sentences(text) {
            assert!(text.contains(s), "a sentence is the text's own: {s:?}");
        }
    }

    #[test]
    fn a_numbered_list_is_one_sentence_per_item_its_marker_leading() {
        let text = "Points:\n1. The audience is wrong\n2) Budget is fine\n 10. Tone. Too loud\n3.5 stays";
        assert_eq!(
            sentences(text),
            vec!["Points:", "1. The audience is wrong", "2) Budget is fine", "10. Tone.", "Too loud\n3.5 stays"]
        );
        assert_eq!(sentences("1. First\n2. Second"), vec!["1. First", "2. Second"], "a list at the start");
        assert_eq!(sentences("1234. Not a marker"), vec!["1234.", "Not a marker"]);
        let parts = [part("Audience", &[]), part("Budget", &[])];
        let r = suggest("accepted_with_changes", text, &parts);
        assert_eq!(r[0].quote, "1. The audience is wrong", "no next item's marker in the quote");
        assert_eq!((r[1].quote.as_str(), r[1].answer), ("2) Budget is fine", "accepted"));
    }

    #[test]
    fn an_accent_typed_as_one_character_or_two_matches_alike() {
        let aliases = vec!["café".to_string()];
        let parts = [part("Venue", &aliases)];
        for said in ["The caf\u{e9} is wrong.", "The cafe\u{301} is wrong."] {
            let r = suggest("accepted_with_changes", said, &parts);
            assert_eq!(r.len(), 1, "{said:?}");
            assert_eq!(r[0].quote, said, "the quote is the proof's own text, not normalised");
        }
        let decomposed = vec!["cafe\u{301}".to_string()];
        assert_eq!(suggest("rejected", "The café.", &[part("Venue", &decomposed)]).len(), 1);
    }

    #[test]
    fn a_part_is_named_by_its_label_its_aliases_and_the_last_word_of_its_label() {
        let aliases = vec!["SMP".to_string(), " the line ".to_string(), "".to_string()];
        assert_eq!(names(&part("Single-minded proposition", &aliases)), vec![
            "Single-minded proposition",
            "SMP",
            "the line",
            "proposition"
        ]);
        assert_eq!(names(&part("Audience", &[])), vec!["Audience"], "the last word is the label");
        assert_eq!(names(&part("Why now?", &[])), vec!["Why now?", "now"]);
    }

    #[test]
    fn whole_words_ignoring_case_and_runs_of_whitespace() {
        assert!(holds(&fold("The AUDIENCE's age"), &fold("audience")));
        assert!(!holds(&fold("audiences are"), &fold("audience")));
        assert!(!holds(&fold("a stone"), &fold("tone")));
        assert!(holds(&fold("the single-minded\n  proposition"), &fold("Single-minded proposition")));
        assert!(holds(&fold("It doesn’t land"), "doesn't"));
        assert!(!holds(&fold("cannot"), "not"));
        assert!(!holds("anything", ""));
    }

    #[test]
    fn the_answer_follows_the_document_and_the_change_cues() {
        let parts = [part("Single-minded proposition", &[]), part("Audience", &[]), part("Tone", &[])];
        let said = "Love the proposition. The audience is wrong.\nTone: warm, good.";
        let r = suggest("accepted_with_changes", said, &parts);
        let got: Vec<_> = r.iter().map(|f| (f.part, f.answer, f.quote.as_str(), f.cue)).collect();
        assert_eq!(got, vec![
            (0, "accepted", "Love the proposition.", None),
            (1, "accepted_with_changes", "The audience is wrong.", Some("wrong")),
            (2, "accepted", "Tone: warm, good.", None),
        ]);
        assert_eq!(r[0].name, "proposition", "found by the last word of its label");

        let r = suggest("rejected", said, &parts);
        assert!(r.iter().all(|f| f.answer == "rejected" && f.cue.is_none()));
        assert!(suggest("accepted", said, &parts).is_empty(), "an acceptance asks for none");
        assert!(suggest("rejected", "Not us at all.", &parts).is_empty(), "no part named: none");
    }

    #[test]
    fn one_sentence_may_name_several_parts_and_each_part_takes_its_first() {
        let aliases = vec!["voice".to_string()];
        let parts = [part("Audience", &[]), part("Tone", &aliases)];
        let said = "The voice and the audience are off. Also the audience is too young.";
        let r = suggest("accepted_with_changes", said, &parts);
        assert_eq!(r.len(), 2);
        assert_eq!(r[0].quote, "The voice and the audience are off.");
        assert_eq!(r[1].quote, "The voice and the audience are off.");
        assert_eq!(r[1].name, "voice");
        assert_eq!(r[0].answer, "accepted", "no cue in that sentence");
    }

    #[test]
    fn it_is_deterministic_and_every_quote_is_verbatim() {
        let parts = [part("Audience", &[]), part("Tone", &[])];
        let said = "  The tone is  too\nloud!  And the AUDIENCE? ".repeat(3);
        let a = suggest("accepted_with_changes", &said, &parts);
        assert_eq!(a, suggest("accepted_with_changes", &said, &parts));
        assert_eq!(a.iter().map(|f| f.part).collect::<Vec<_>>(), vec![0, 1], "in the parts' order");
        for f in &a {
            assert!(said.contains(&f.quote), "{:?}", f.quote);
        }
        let long = format!("The audience {}.", "x".repeat(600));
        let q = &suggest("rejected", &long, &parts)[0].quote;
        assert_eq!(q.chars().count(), MAX_QUOTE);
        assert!(long.contains(q.as_str()));
    }
}
