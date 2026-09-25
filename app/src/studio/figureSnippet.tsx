// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// The agent figures as a plain-HTML snippet, for the template apps.
//
// Brief Maker and the Research Tool are plain HTML inside a .clan: no React,
// no bundler. They draw the same approved figures as the shell by inlining
// this snippet, which is AgentFigure rendered to static SVG once per agent,
// plus AgentFigure.css and a small `agentFigure(key, opts)` helper. The
// generated file is app/templates/shared/agent-figures.html (written by
// scripts/agent-figures.mjs; a test holds it to this function), and each
// template's packer (crates/clan-sdk/examples/make_*.rs) splices it in at the
// `<!-- @napkin:agent-figures -->` marker in the template's <head>.
//
// It also carries the shared crew, `NapkinAgents`: the one place an app asks
// who does its work. An app names what a step is doing (read, research on a
// lens, synthesise, draft, judge) and gets that agent, and asks who made a
// decision with the same rule the shell uses (model.ts agentOfDecision).

import { renderToStaticMarkup } from 'react-dom/server'
import { AgentFigure } from './AgentFigure'
import { AGENTS, AGENT_FOR_WORK, AGENT_KEYS, AGENT_OF_LENS, LENS_IDS, WORK_OF_STEP } from './model'

export const FIGURES_MARKER = '<!-- @napkin:agent-figures -->'

/**
 * The helper the templates call. Plain ES5, no dependencies: it takes the
 * idle markup and sets the state, the stride, the label and the badge.
 *   agentFigure('drafter', {state:'working', label:'Drafter · Insight'})
 *   agentFigure('judge', {state:'needs-you'})
 *   agentFigure('extract', {decorative:true})
 */
const HELPER = `function agentFigure(key,o){
  var F=AGENT_FIGURES, s=F.svg[key]; if(!s) return ''; o=o||{};
  var st=o.state==='working'||o.state==='needs-you'?o.state:'idle';
  s=s.replace('data-state="idle"','data-state="'+st+'"'+(o.walking?' data-walking="true"':''));
  if(o.cls) s=s.replace('class="agent-figure"','class="agent-figure '+o.cls+'"');
  if(st==='needs-you') s=s.replace(/<\\/svg>$/,F.badge+'<\\/svg>');
  if(o.decorative) s=s.replace(/ role="img" aria-label="[^"]*"/,' aria-hidden="true" focusable="false"');
  else {
    var l=o.label||F.name[key]+', '+F.role[key];
    l+=st==='needs-you'?' \\u2014 needs you':st==='working'?' \\u2014 working':'';
    l=String(l).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});
    s=s.replace(/aria-label="[^"]*"/,'aria-label="'+l+'"');
  }
  return s;
}`

/**
 * The shared crew, plain ES5. Mirrors model.ts agentForWork and
 * agentOfDecision over the same tables; tests/agentFigures.test.ts runs both
 * on the same cases.
 *   NapkinAgents.forWork('draft')                      'drafter'
 *   NapkinAgents.forWork('research', 'media_spend')    'media'
 *   NapkinAgents.ofDecision({agent:'napkin/brief/judge'})  'judge'
 *   NapkinAgents.figure('draft', {state:'working'})    the Drafter's figure
 */
const CREW = `var NapkinAgents=(function(T){
  function last(s){ return String(s).split('/').pop().replace(/@.*$/,'').toLowerCase(); }
  function forWork(w,lens){
    if(w==='research') return T.lens[lens]||'market_structure';
    return T.work[w]||'';
  }
  function ofDecision(d){
    d=d||{}; var who=String(d.agent||''), act=String(d.action||'');
    if(!who||who==='human'||/^human:/.test(who)) return null;
    if(d.kind==='verdict'||d.polarity) return 'judge';
    if(d.kind==='finding') return 'synthesis';
    var w=T.step[last(who)]||T.step[act.toLowerCase()]
      ||(/judge|critic/i.test(who)?'judge':/extract|capture/i.test(who+' '+act)?'read':/draft/i.test(who)?'draft':'');
    if(!w) return null;
    if(w!=='research') return T.work[w];
    var hay=[d.lens||'',act].concat(d.targets||[]);
    for(var i=0;i<T.lenses.length;i++) for(var j=0;j<hay.length;j++)
      if(String(hay[j]).indexOf(T.lenses[i])!==-1) return T.lens[T.lenses[i]];
    return null;
  }
  return {
    keys:T.keys, lenses:T.lenses, name:AGENT_FIGURES.name, role:AGENT_FIGURES.role,
    forWork:forWork, ofDecision:ofDecision,
    // A figure by the work it stands for: figure('judge'), figure('research', o, 'media_spend').
    figure:function(w,o,lens){ return agentFigure(forWork(w,lens),o); }
  };
})(${'${TABLES}'});`

/** Escape for a JS string literal that sits inside an HTML <script>. */
function js(s: string): string {
  // JSON quoting, then keep the HTML parser from seeing a closing tag.
  return JSON.stringify(s).replace(/<\//g, '<\\/')
}

/** JSON as a JS literal inside an HTML <script>. */
function js2(json: string): string {
  return json.replace(/<\//g, '<\\/')
}

/** The whole snippet: the figure CSS, then the figures and the helper. */
export function figureSnippet(css: string): string {
  const svg: string[] = []
  const name: string[] = []
  const role: string[] = []
  let badge = ''
  for (const key of AGENT_KEYS) {
    svg.push(`${key}:${js(renderToStaticMarkup(<AgentFigure agent={key} />))}`)
    name.push(`${key}:${js(AGENTS[key].name)}`)
    role.push(`${key}:${js(AGENTS[key].role)}`)
    if (!badge) {
      const needs = renderToStaticMarkup(<AgentFigure agent={key} state="needs-you" />)
      badge = needs.slice(needs.indexOf('<g class="af-badge">'), needs.lastIndexOf('</svg>'))
    }
  }
  return [
    '<!-- Generated by app/scripts/agent-figures.mjs from app/src/studio/AgentFigure.tsx',
    '     and AgentFigure.css. Do not edit here: change the figures there and run',
    '     `npm run figures` in app/. -->',
    `<style id="agent-figures-css">\n${css.trim()}\n</style>`,
    '<script>',
    `var AGENT_FIGURES={svg:{${svg.join(',\n')}},\nname:{${name.join(',')}},\nrole:{${role.join(',')}},\nbadge:${js(badge)}};`,
    HELPER,
    CREW.replace('${TABLES}', js2(JSON.stringify({
      keys: AGENT_KEYS, work: AGENT_FOR_WORK, lens: AGENT_OF_LENS, lenses: LENS_IDS, step: WORK_OF_STEP,
    }))),
    '</script>',
    '',
  ].join('\n')
}
