// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// How the serverless host frames an app — the part of it that needs no
// WebAssembly.
//
// Kept apart from wasm.ts so the shell can compose a frame, and the one bundle
// can name this host, without pulling a 5 MB module onto the critical path of
// a signed-in page that may never open a file on the device.

/**
 * What an app frame may reach when the document came from this device.
 *
 * The frame is sandboxed without `allow-same-origin`, so it already cannot
 * touch the shell's storage, its offline copies or its service worker. This
 * closes the other door: the app's own scripts sending the document somewhere.
 * Every host call is a postMessage to the parent, so the frame needs no
 * network at all — only its inline code, the assets the shim turns into blobs,
 * and the typeface the published templates ask Google for.
 *
 * It does not stop a script navigating its own frame to an address it builds;
 * no browser ships a CSP directive for that yet.
 */
export const FRAME_CSP = [
  "default-src 'none'",
  "script-src 'unsafe-inline'",
  "style-src 'unsafe-inline' https://fonts.googleapis.com",
  'font-src data: https://fonts.gstatic.com',
  'img-src data: blob:',
  'media-src data: blob:',
  // An app that prints builds its layout in a child frame.
  'frame-src blob: data:',
  "connect-src 'none'",
  "form-action 'none'",
  "base-uri 'none'",
].join('; ')

/** The policy goes first of all — ahead of the shim, and of anything the app
 * put before its `<head>` — because a meta policy only governs what is parsed
 * after it. After the doctype, so the page is not thrown into quirks mode. */
function withPolicy(html: string): string {
  const meta = `<meta http-equiv="Content-Security-Policy" content="${FRAME_CSP}">`
  const doctype = /^\s*<!doctype[^>]*>/i.exec(html)
  if (doctype) return doctype[0] + meta + html.slice(doctype[0].length)
  return meta + html
}

/** Put a shim ahead of every script the app has, not after them.
 *
 * Apps fetch on parse, not on DOMContentLoaded — the launcher asks for its app
 * list from an inline script in the body. A shim spliced at `</body>` installs
 * itself after that has already run and quietly does nothing. */
function injectFirst(html: string, script: string): string {
  const head = /<head\b[^>]*>/i.exec(html)
  if (head) return html.slice(0, head.index + head[0].length) + script + html.slice(head.index + head[0].length)
  const tag = /<html\b[^>]*>/i.exec(html)
  if (tag) return html.slice(0, tag.index + tag[0].length) + script + html.slice(tag.index + tag[0].length)
  return script + html
}

const SHIM = `<script>(function(){
  var f=window.fetch, seq=0, pending={};
  window.addEventListener('message',function(e){
    if(e.source!==window.parent) return;
    var m=e.data;
    if(m&&m.type==='clan:rpc-reply'&&pending[m.id]){pending[m.id](m);delete pending[m.id];}
  });
  function rpc(path,query,body){
    return new Promise(function(resolve){
      var id=++seq; pending[id]=resolve;
      window.parent.postMessage({type:'clan:rpc',id:id,path:path,query:query,body:body},'*');
    }).then(function(m){
      // 204 and 304 may not carry a body; nothing here returns them, but a
      // Response constructed with one would throw.
      var body=(m.status===204||m.status===304)?null:m.body;
      return new Response(body,{status:m.status,headers:m.headers||{}});
    });
  }
  // There is no server: every clan:// call is a function call in the parent.
  function split(u){
    var m=/^(?:clan:\\/\\/localhost|http:\\/\\/clan\\.localhost)(\\/[^?]*)(?:\\?(.*))?$/.exec(u);
    return m?{path:m[1],query:m[2]||''}:null;
  }
  window.fetch=function(input,init){
    if(typeof input==='string'){
      var t=split(input);
      if(t) return rpc(t.path,t.query,(init&&init.body)||'');
    }
    return f.call(this,input,init);
  };

  // An <img src="clan://…/assets/x.png"> never reaches fetch, so asset URLs in
  // the DOM are swapped for blobs as they appear. Mood boards depend on it.
  var blobs={};
  function resolveAsset(el){
    var src=el.getAttribute('src')||'';
    var t=split(src);
    if(!t||t.path.indexOf('/assets/')!==0) return;
    if(blobs[src]){el.src=blobs[src];return;}
    rpc(t.path,t.query,'').then(function(r){return r.ok?r.blob():null;}).then(function(b){
      if(b){blobs[src]=URL.createObjectURL(b);el.src=blobs[src];}
    });
  }
  function sweep(root){
    if(root.querySelectorAll) root.querySelectorAll('img[src]').forEach(resolveAsset);
    if(root.tagName==='IMG') resolveAsset(root);
  }
  new MutationObserver(function(muts){
    muts.forEach(function(m){
      m.addedNodes.forEach(function(n){ if(n.nodeType===1) sweep(n); });
      if(m.type==='attributes'&&m.target.tagName==='IMG') resolveAsset(m.target);
    });
  }).observe(document.documentElement,{childList:true,subtree:true,attributes:true,attributeFilter:['src']});
  document.addEventListener('DOMContentLoaded',function(){sweep(document);});
})();</script>`

/** Last pass over a composed app page before the frame loads it. */
export function prepareFrameHtml(html: string): string {
  return withPolicy(injectFirst(html, SHIM))
}
