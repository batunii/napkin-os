/*
 * golden-brief.js — render + validate library for the Golden Brief schema.
 * Dependency-free. Works in the browser (window.GoldenBrief) and in Node (module.exports).
 *
 *   GoldenBrief.validate(schema, brief)  -> { fields, dependencies, definition_of_done, open_questions, health }
 *   GoldenBrief.renderForm(schema, mountEl, brief, opts)   (browser only)
 *
 * A "brief" is: { meta:{...}, fields:{ id:{ value, source, confidence, assumptions, filled_by } }, gate:{...} }
 * Auto checks are evaluated here. "llm"/"human" checks return status "review" — wire these to your critic agent.
 */
(function (root, factory) {
  var lib = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = lib;
  else root.GoldenBrief = lib;
})(typeof self !== "undefined" ? self : this, function () {

  function words(s) { return String(s || "").trim().split(/\s+/).filter(Boolean); }
  function wc(s) { return words(s).length; }
  function sentences(s) { return String(s || "").trim().split(/[.!?]+(?:\s|$)/).filter(function (x) { return x.trim().length; }); }
  function listItems(v) { return Array.isArray(v) ? v.filter(Boolean) : String(v || "").split(/[·;\n]|,(?![^()]*\))/).map(function (x) { return x.trim(); }).filter(Boolean); }
  function isFilled(v) {
    if (v == null) return false;
    if (Array.isArray(v)) return v.filter(Boolean).length > 0;
    if (typeof v === "object") return Object.keys(v).some(function (k) { return String(v[k] || "").trim(); });
    return String(v).trim().length > 0;
  }
  function val(brief, id) { return brief && brief.fields && brief.fields[id] ? brief.fields[id].value : undefined; }
  function entry(brief, id) { return (brief && brief.fields && brief.fields[id]) || {}; }

  var PASS = "pass", FAIL = "fail", REVIEW = "review";

  // ---- per-check auto evaluators (only the "auto" ones need a function) ----
  var AUTO = {
    within_limit: function (f, v) {
      var n = wc(v); if (f.max_words && n > f.max_words) return [FAIL, n + "/" + f.max_words + " words"];
      if (f.min_words && n < f.min_words) return [FAIL, "too short (" + n + " words)"];
      return [PASS, n + (f.max_words ? "/" + f.max_words : "") + " words"];
    },
    max_items: function (f, v) { var n = listItems(v).length; return n <= (f.max_items || 99) ? [PASS, n + " items"] : [FAIL, n + " > " + f.max_items]; },
    single_sentence: function (f, v) { var n = sentences(v).length; return n <= 1 ? [PASS, "1 sentence"] : [FAIL, n + " sentences"]; },
    single_minded: function (f, v) {
      var s = String(v || "");
      var listy = /(,| and | & |·|;|\/)/.test(s.replace(/,(?=\d{3}\b)/g, ""));
      return listy ? [REVIEW, "may carry >1 idea"] : [PASS, "one idea"];
    },
    reveals_why: function (f, v) { return /\bbecause\b|\bso the job\b|\bwhich means\b/i.test(String(v || "")) ? [PASS, "states a 'why'"] : [FAIL, "no motivation ('because…')"]; },
    three_levels: function (f, v) { var o = v || {}; var have = f.shape.filter(function (k) { return isFilled(o[k]); }); return have.length === f.shape.length ? [PASS, "3/3 levels"] : [FAIL, have.length + "/3 levels"]; },
    all_three: function (f, v) { var o = v || {}; var have = (f.shape || []).filter(function (k) { return isFilled(o[k]); }); return have.length === f.shape.length ? [PASS, "think/feel/do"] : [FAIL, have.length + "/3"]; },
    has_constraint: function (f, v) { return /\d|budget|media|€|\$|£|prioritise|scope|cities|national/i.test(String(v || "")) && isFilled(v) ? [PASS, "constraint set"] : [FAIL, "no constraint"]; },
    has_deliverables: function (f, v) { return /\d|×|x\d|s\b|OOH|social|TV|print|radio|deliver|live|cutdown/i.test(String(v || "")) ? [PASS, "deliverables listed"] : [REVIEW, "check deliverables"]; },
    names_rivals: function (f, v) { return isFilled(v) && wc(v) > 4 ? [PASS, "category read present"] : [FAIL, "too thin"]; }
  };

  function runFieldChecks(schema, field, brief) {
    var v = val(brief, field.id);
    var filled = isFilled(v);
    var checks = (field.rubric || []).map(function (c) {
      if (!filled) return { id: c.id, test: c.test, method: c.method, status: REVIEW, note: "empty" };
      if (c.method === "auto" && AUTO[c.id]) { var r = AUTO[c.id](field, v); return { id: c.id, test: c.test, method: c.method, status: r[0], note: r[1] }; }
      // "ownable" depends on competitor_context being present — partial auto guard
      if (c.id === "ownable") { if (!isFilled(val(brief, "competitor_context"))) return { id: c.id, test: c.test, method: c.method, status: FAIL, note: "needs competitor_context" }; return { id: c.id, test: c.test, method: c.method, status: REVIEW, note: "agent to judge" }; }
      return { id: c.id, test: c.test, method: c.method, status: REVIEW, note: c.method === "llm" ? "agent to judge" : "human to confirm" };
    });
    return { id: field.id, label: field.label, filled: filled, hero: !!field.hero, checks: checks };
  }

  function fieldRequired(field, brief) {
    if (field.required) return true;
    if (field.required_unless_brief_type) { var bt = brief && brief.meta && brief.meta.brief_type; return field.required_unless_brief_type.indexOf(bt) === -1; }
    return false;
  }

  function deriveOpenQuestions(schema, brief) {
    var floor = schema.confidence_floor || 0.6, out = [];
    schema.fields.forEach(function (f) {
      var e = entry(brief, f.id);
      if (fieldRequired(f, brief) && !isFilled(e.value)) out.push({ question: "Missing: " + f.label, blocks_field: f.id, severity: "high" });
      else if (e.source === "missing") out.push({ question: "Confirm with client: " + f.label, blocks_field: f.id, severity: "high" });
      else if (typeof e.confidence === "number" && e.confidence < floor && isFilled(e.value)) out.push({ question: "Low confidence on " + f.label + " — confirm.", blocks_field: f.id, severity: "medium" });
    });
    // include any author-supplied questions
    (brief && brief.open_questions || []).forEach(function (q) { out.push(q); });
    return out;
  }

  function runDependencies(schema, brief) {
    return schema.dependencies.map(function (d) {
      var allFilled = d.fields.every(function (id) { return isFilled(val(brief, id)); });
      var status = REVIEW, note = d.method === "auto" ? "" : "agent to judge";
      if (d.id === "rtb_supports_smp") { status = allFilled ? REVIEW : FAIL; note = allFilled ? "RTBs present — agent to confirm support" : "missing SMP or RTBs"; }
      else if (d.id === "response_ladders") { status = allFilled ? REVIEW : FAIL; note = allFilled ? "agent to confirm ladder" : "missing response/objectives"; }
      else if (d.id === "ownable_needs_competitors") { status = isFilled(val(brief, "competitor_context")) ? PASS : FAIL; note = status === PASS ? "competitor context present" : "fill competitor_context"; }
      return { id: d.id, rule: d.rule, method: d.method, status: status, note: note };
    });
  }

  function runDoD(schema, brief, fieldResults, openQuestions) {
    var byId = {}; fieldResults.forEach(function (fr) { byId[fr.id] = fr; });
    function chk(fid, cid) { var fr = byId[fid]; if (!fr) return null; var c = fr.checks.filter(function (x) { return x.id === cid; })[0]; return c ? c.status : null; }
    var requiredFilled = schema.fields.filter(function (f) { return fieldRequired(f, brief); }).every(function (f) { return isFilled(val(brief, f.id)); });
    var withinLimits = fieldResults.every(function (fr) { return fr.checks.filter(function (c) { return c.id === "within_limit" || c.id === "max_items"; }).every(function (c) { return c.status !== FAIL; }); });
    var highOpen = openQuestions.some(function (q) { return q.severity === "high"; });
    var evalPresent = !!(brief && brief.gate && String(brief.gate.evaluation_criteria || "").trim());
    var map = {
      all_required_filled: requiredFilled ? PASS : FAIL,
      objectives_linked: chk("objectives", "three_levels") || REVIEW,
      smp_single: (chk("smp", "single_sentence") === PASS && chk("smp", "within_limit") === PASS) ? PASS : (chk("smp", "single_sentence") === FAIL ? FAIL : REVIEW),
      rtb_supports_smp: isFilled(val(brief, "reasons_to_believe")) ? REVIEW : FAIL,
      evaluation_present: evalPresent ? PASS : FAIL,
      open_questions_clear: highOpen ? FAIL : PASS,
      within_limits: withinLimits ? PASS : FAIL
    };
    return schema.gate.definition_of_done.map(function (d) {
      var status = map[d.id] != null ? map[d.id] : REVIEW;
      return { id: d.id, rule: d.rule, method: d.method, status: status };
    });
  }

  function health(fieldResults, dod, openQuestions) {
    var total = 0, score = 0;
    fieldResults.forEach(function (fr) {
      fr.checks.forEach(function (c) {
        var w = fr.hero ? 2 : 1; total += w;
        score += (c.status === PASS ? w : c.status === REVIEW ? w * 0.5 : 0);
      });
    });
    var base = total ? score / total : 0;
    var dodFails = dod.filter(function (d) { return d.status === FAIL; }).length;
    var highOpen = openQuestions.filter(function (q) { return q.severity === "high"; }).length;
    var penalty = dodFails * 0.04 + highOpen * 0.03;
    return Math.max(0, Math.min(100, Math.round((base - penalty) * 100)));
  }

  function validate(schema, brief) {
    var fieldResults = schema.fields.map(function (f) { return runFieldChecks(schema, f, brief); });
    var openQuestions = deriveOpenQuestions(schema, brief);
    var dependencies = runDependencies(schema, brief);
    var dod = runDoD(schema, brief, fieldResults, openQuestions);
    return { fields: fieldResults, dependencies: dependencies, definition_of_done: dod, open_questions: openQuestions, health: health(fieldResults, dod, openQuestions) };
  }

  /* ----------------------------- RENDER (browser) ----------------------------- */
  function el(tag, css, html) { var e = document.createElement(tag); if (css) e.setAttribute("style", css); if (html != null) e.innerHTML = html; return e; }
  var PROV = { client_stated: ["#EAF3DE", "#27500A", "client-stated"], inferred: ["#E6F1FB", "#0C447C", "inferred"], missing: ["#FAEEDA", "#633806", "needs client"] };

  function renderForm(schema, mount, brief, opts) {
    if (typeof document === "undefined") throw new Error("renderForm requires a browser DOM");
    opts = opts || {}; brief = brief || { meta: {}, fields: {}, gate: {} }; brief.fields = brief.fields || {};
    mount.innerHTML = "";
    var state = brief;

    function provChip(src) { var c = PROV[src] || PROV.inferred; return '<span style="font-size:11px;background:' + c[0] + ';color:' + c[1] + ';padding:2px 7px;border-radius:6px;">' + c[2] + '</span>'; }

    function fieldRow(f) {
      var e = state.fields[f.id] || (state.fields[f.id] = { value: f.type === "list" ? [] : (f.type === "objectives" || f.type === "tfd") ? {} : "", source: "inferred", confidence: 0.7 });
      var hero = !!f.hero;
      var wrap = el("div", hero
        ? "margin:11px 0;background:#EEEDFE;border:0.5px solid #AFA9EC;border-radius:12px;padding:12px 14px;"
        : "padding:11px 0;border-bottom:0.5px solid var(--bd, rgba(0,0,0,.12));");
      var head = el("div", "display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:5px;");
      head.innerHTML = '<span style="font-size:13.5px;font-weight:500;' + (hero ? "color:#3C3489;" : "") + '">' + f.label + '</span>';
      // provenance select
      var sel = el("select", "font-size:11px;padding:2px 4px;");
      ["client_stated", "inferred", "missing"].forEach(function (p) { var o = el("option"); o.value = p; o.text = PROV[p][2]; if (e.source === p) o.selected = true; sel.appendChild(o); });
      sel.onchange = function () { e.source = sel.value; recompute(); };
      head.appendChild(sel);
      // confidence
      var conf = el("input"); conf.type = "range"; conf.min = 0; conf.max = 1; conf.step = 0.01; conf.value = e.confidence != null ? e.confidence : 0.7; conf.setAttribute("style", "width:80px;");
      var confOut = el("span", "font-size:11px;color:#777;min-width:30px;", Number(conf.value).toFixed(2));
      conf.oninput = function () { e.confidence = parseFloat(conf.value); confOut.textContent = e.confidence.toFixed(2); recompute(); };
      head.appendChild(conf); head.appendChild(confOut);
      wrap.appendChild(head);
      // prompt
      wrap.appendChild(el("div", "font-size:11.5px;color:#888;font-style:italic;margin:0 0 5px;", f.prompt));
      // input
      var input;
      if (f.type === "list") { input = el("textarea"); input.value = Array.isArray(e.value) ? e.value.join(" · ") : e.value; input.placeholder = "one per line or separated by ·"; }
      else if (f.type === "objectives" || f.type === "tfd") {
        input = el("div");
        (f.shape).forEach(function (k) {
          var sub = el("input"); sub.type = "text"; sub.placeholder = k; sub.value = (e.value && e.value[k]) || ""; sub.setAttribute("style", "width:100%;font-size:13px;margin:2px 0;padding:5px 7px;");
          sub.oninput = function () { e.value = e.value || {}; e.value[k] = sub.value; recompute(); };
          input.appendChild(sub);
        });
      } else { input = el("textarea"); input.value = e.value || ""; }
      if (input.tagName === "TEXTAREA") {
        input.setAttribute("style", "width:100%;font-size:" + (f.id === "smp" ? "15px" : "13.5px") + ";min-height:" + (hero ? "44px" : "38px") + ";padding:7px 9px;border:0.5px solid rgba(0,0,0,.18);border-radius:8px;resize:vertical;font-family:inherit;");
        input.oninput = function () { e.value = f.type === "list" ? input.value.split(/[·\n]/).map(function (x) { return x.trim(); }).filter(Boolean) : input.value; recompute(); };
      }
      wrap.appendChild(input);
      // example
      wrap.appendChild(el("div", "font-size:11.5px;color:#999;margin-top:4px;", "<b style='color:#b51d1d;font-weight:500;'>e.g.</b> <i>" + f.good_example + "</i>"));
      // rubric badges
      var badges = el("div", "margin-top:7px;display:flex;flex-wrap:wrap;gap:6px;"); badges.setAttribute("data-badges", f.id);
      wrap.appendChild(badges);
      return wrap;
    }

    // header
    var header = el("div", "display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;");
    header.innerHTML = '<div style="font-size:16px;font-weight:500;"><i class="ti ti-notes"></i> Napkin Brief</div>';
    var healthBox = el("div", "text-align:right;"); healthBox.setAttribute("data-health", "1"); header.appendChild(healthBox);
    mount.appendChild(header);

    var fieldsWrap = el("div"); schema.fields.forEach(function (f) { fieldsWrap.appendChild(fieldRow(f)); }); mount.appendChild(fieldsWrap);

    var oq = el("div", "margin-top:12px;background:#FAEEDA;border:0.5px solid #EF9F27;border-radius:12px;padding:10px 12px;"); oq.setAttribute("data-oq", "1"); mount.appendChild(oq);
    var gate = el("div", "margin-top:12px;background:rgba(0,0,0,.04);border-radius:12px;padding:10px 12px;"); gate.setAttribute("data-gate", "1"); mount.appendChild(gate);

    function badge(c) {
      var col = c.status === "pass" ? ["#EAF3DE", "#27500A", "check"] : c.status === "fail" ? ["#FCEBEB", "#A32D2D", "x"] : ["#F1EFE8", "#5F5E5A", "dots"];
      return '<span title="' + c.test + (c.note ? " — " + c.note : "") + '" style="font-size:11px;background:' + col[0] + ';color:' + col[1] + ';padding:2px 7px;border-radius:6px;"><i class="ti ti-' + col[2] + '"></i> ' + c.id.replace(/_/g, " ") + '</span>';
    }

    function recompute() {
      var res = validate(schema, state);
      res.fields.forEach(function (fr) { var b = mount.querySelector('[data-badges="' + fr.id + '"]'); if (b) b.innerHTML = fr.checks.map(badge).join(""); });
      var hb = mount.querySelector('[data-health="1"]');
      var color = res.health >= 80 ? "#0F6E56" : res.health >= 60 ? "#854F0B" : "#A32D2D";
      hb.innerHTML = '<div style="font-size:11px;color:#888;">Brief health</div><div style="font-size:20px;font-weight:500;color:' + color + ';">' + res.health + '<span style="font-size:11px;color:#aaa;">/100</span></div>';
      var oqEl = mount.querySelector('[data-oq="1"]');
      oqEl.innerHTML = '<div style="font-size:13px;font-weight:500;color:#633806;margin-bottom:6px;"><i class="ti ti-help-circle"></i> Open questions (' + res.open_questions.length + ')</div>' +
        (res.open_questions.length ? res.open_questions.map(function (q) { return '<div style="font-size:12.5px;color:#633806;margin:3px 0;"><span style="font-size:10px;background:#EF9F27;color:#412402;padding:1px 6px;border-radius:5px;">' + q.severity + '</span> ' + q.question + '</div>'; }).join("") : '<div style="font-size:12.5px;color:#633806;">None — ready to brief.</div>');
      var gEl = mount.querySelector('[data-gate="1"]');
      gEl.innerHTML = '<div style="font-size:13px;font-weight:500;margin-bottom:6px;"><i class="ti ti-flag"></i> The gate — definition of done</div>' +
        '<div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 14px;">' + res.definition_of_done.map(function (d) {
          var ic = d.status === "pass" ? ["circle-check", "#0F6E56"] : d.status === "fail" ? ["alert-circle", "#A32D2D"] : ["clock", "#5F5E5A"];
          return '<div title="' + d.rule + '" style="font-size:12px;color:#444;"><i class="ti ti-' + ic[0] + '" style="color:' + ic[1] + '"></i> ' + d.id.replace(/_/g, " ") + '</div>';
        }).join("") + '</div>';
      if (opts.onChange) opts.onChange(res, state);
    }
    recompute();
    return { recompute: recompute, getBrief: function () { return state; }, validate: function () { return validate(schema, state); } };
  }

  return { validate: validate, renderForm: renderForm, _util: { wc: wc, sentences: sentences, listItems: listItems, isFilled: isFilled } };
});
