// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! Export (HTML / PDF).
//!
//! HTML export is a copy of a composed standalone document; PDF is rendered
//! from that HTML by a headless browser (no HTML→PDF Rust dep). The composing
//! is [`crate::session::Session::compose_export`]; this module is the part that
//! puts bytes somewhere.

use std::process::{Command, Stdio};

use crate::error::{HostError, HostResult};

fn has_binary(name: &str) -> bool {
    std::env::var_os("PATH")
        .map(|paths| std::env::split_paths(&paths).any(|dir| dir.join(name).is_file()))
        .unwrap_or(false)
}

/// First available headless-capable browser for HTML→PDF, or None.
/// PATH names cover Linux; macOS GUI apps launch with a minimal PATH and
/// browsers live inside .app bundles, so absolute bundle paths are checked too.
pub fn find_pdf_renderer() -> Option<String> {
    let on_path = [
        "chromium",
        "chromium-browser",
        "google-chrome-stable",
        "google-chrome",
        "chrome",
        "brave",
        "microsoft-edge",
    ]
    .into_iter()
    .find(|b| has_binary(b))
    .map(|s| s.to_string());
    if on_path.is_some() {
        return on_path;
    }
    #[cfg(target_os = "macos")]
    for p in [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ] {
        if std::path::Path::new(p).is_file() {
            return Some(p.to_string());
        }
    }
    None
}

/// The OS's legibility check, run in every export as it renders (in the PDF
/// renderer, and in the browser that opens an HTML export). A theme can leave
/// text the colour of what is behind it — an app's dark tokens on the white of
/// paper, a pale accent on white. Any text under 3:1 against its own background
/// is set to ink or white, whichever reads; nothing else is touched. The count
/// it fixed is left on `<html data-napkin-legible>`.
const LEGIBLE: &str = r#"<script data-napkin-legible>(function(){
function rgb(s){var m=/rgba?\(([^)]+)\)/.exec(s||'');if(!m)return null;var p=m[1].split(',').map(Number);return {r:p[0],g:p[1],b:p[2],a:p.length>3?p[3]:1};}
function lum(c){function f(v){v/=255;return v<=.03928?v/12.92:Math.pow((v+.055)/1.055,2.4);}return .2126*f(c.r)+.7152*f(c.g)+.0722*f(c.b);}
function bg(el){for(;el&&el.nodeType===1;el=el.parentElement){var cs=getComputedStyle(el);if(cs.backgroundImage&&cs.backgroundImage!=='none')return null;var c=rgb(cs.backgroundColor);if(c&&c.a>.5)return c;}return {r:255,g:255,b:255,a:1};}
function run(){var n=0,seen=[],w=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
while(w.nextNode()){var t=w.currentNode,el=t.parentElement;if(!el||!t.textContent.trim()||seen.indexOf(el)>=0)continue;seen.push(el);
var fg=rgb(getComputedStyle(el).color),b=bg(el);if(!fg||!b)continue;var x=lum(fg),y=lum(b);
if((Math.max(x,y)+.05)/(Math.min(x,y)+.05)<3){el.style.setProperty('color',y>.4?'#14161B':'#FFFFFF','important');n++;}}
document.documentElement.setAttribute('data-napkin-legible',String(n));}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',run);else run();
window.addEventListener('beforeprint',run);})();</script>"#;

/// The export with the legibility check spliced in before its last `</body>`
/// (an app's own strings can hold a `</body>` earlier on), or appended.
pub fn with_legibility_check(html: &str) -> String {
    match html.to_ascii_lowercase().rfind("</body>") {
        Some(i) => format!("{}{}{}", &html[..i], LEGIBLE, &html[i..]),
        None => format!("{html}{LEGIBLE}"),
    }
}

/// Write the export HTML to a persistent temp file (removed by [`finish_export`]),
/// with the legibility check in it.
pub fn write_temp_html(html: &str) -> HostResult<String> {
    use std::io::Write;
    let html = with_legibility_check(html);
    let tf = tempfile::Builder::new()
        .prefix("napkin-export-")
        .suffix(".html")
        .tempfile()
        .map_err(|e| HostError::internal(e.to_string()))?;
    let (mut file, path) = tf.keep().map_err(|e| HostError::internal(e.to_string()))?;
    file.write_all(html.as_bytes())
        .map_err(|e| HostError::internal(e.to_string()))?;
    Ok(path.display().to_string())
}

pub fn render_pdf(tmp_html: &str, dest: &str) -> HostResult<()> {
    let bin = find_pdf_renderer().ok_or_else(|| HostError::internal(
        "No PDF renderer found. Install 'chromium' (or Chrome), or export to HTML and print to PDF from your browser.",
    ))?;
    let status = Command::new(&bin)
        .args([
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--no-pdf-header-footer",
            "--run-all-compositor-stages-before-draw",
            "--virtual-time-budget=2500",
        ])
        .arg(format!("--print-to-pdf={dest}"))
        .arg(format!("file://{tmp_html}"))
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map_err(|e| HostError::internal(format!("failed to run {bin}: {e}")))?;
    if !status.success() {
        return Err(HostError::internal(format!(
            "{bin} failed to render the PDF"
        )));
    }
    if !std::path::Path::new(dest).exists() {
        return Err(HostError::internal("the renderer produced no PDF file"));
    }
    Ok(())
}

/// Finish an export the shell has a destination for. `html` → copy the temp
/// file; `pdf` → render it. The temp source is always cleaned up.
pub fn finish_export(kind: &str, tmp_html: &str, dest: &str) -> HostResult<String> {
    let result = match kind {
        "html" => std::fs::copy(tmp_html, dest)
            .map(|_| ())
            .map_err(|e| HostError::internal(e.to_string())),
        "pdf" => render_pdf(tmp_html, dest),
        other => Err(HostError::bad_request(format!(
            "unknown export kind '{other}'"
        ))),
    };
    let _ = std::fs::remove_file(tmp_html);
    result.map(|_| dest.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_legibility_check_goes_before_the_last_body_close() {
        let html = "<html><body><script>var s='</body>';</script><p>x</p></BODY></html>";
        let out = with_legibility_check(html);
        let at = out.find("data-napkin-legible").unwrap();
        assert!(at > out.find("<p>x</p>").unwrap(), "after the content, not at an app's '</body>' string");
        assert!(out.ends_with("</BODY></html>"));
        assert!(with_legibility_check("<p>no body</p>").contains("data-napkin-legible"));
    }
}
