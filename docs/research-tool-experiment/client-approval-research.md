# Client approval of sections: research and recommendation

Date: 2026-09-29. Scope: how an agency's external client approves or asks for
changes to individual sections of a brief, a research report, and later decks,
with as little work as possible for the agency person and for the client. This
is research only. No repository file was changed. Code was read in the
`clan-fields` worktree (`/home/batunii/Documents/Code/napkin-os-wt/clan-fields`,
HEAD `954106e`), and the file:line references below point there.

---

## 0. The short version

- Clients will keep replying by email, on calls and on PDFs whatever we build.
  So the first thing to build is a **one-dialog "Record client feedback"**
  action for agency staff: choose the sections, choose Approved or Asked for
  changes, paste or drop the evidence, save. It needs no new infrastructure,
  and it catches the feedback that arrives today.
- Record it as a **new decision kind, `client_review`**, and never as `approve`
  or `verdict`. `approve` is the file-wide lock, and the host treats any
  unsuperseded `approve` as the lock (`review.rs:195-200`). A `verdict` would
  mix client opinion into the quality dossier, and a good one would silently
  clear the Judge's or a colleague's bad verdict (`decisions.rs:806`,
  `decisions.rs:938-942`).
- Every client review records **what the client saw**: the document version,
  the document hash, and a **hash per section**. The section chip reads "Client
  approved" or "Client asked for changes". It turns **stale** once that
  section's current hash differs, and edits to other sections do not stale it.
- A client change request is **a lock blocker until it is answered**: the
  section is revised, or a staff member replies with a reason. This reuses the
  way unanswered bad verdicts already block the lock (`decisions.rs:793-823`).
  A client approval is **not required to lock** by default. "Client signed off"
  is a separate state shown next to the lock.
- The **no-account review link** comes second. It offers Approve or Ask for a
  change per section, and the page posts the section hashes it showed. The link
  is bound to one revision, one recipient email and a list of sections, and it
  expires. A GET never records anything; recording takes a click, and the
  client confirms their email with a one-time code. This is the Ziflow
  "authentication required" pattern.
- Two gaps must be closed before any client-facing view ships. Export does not
  apply `classify` export marks: `compose_export` calls `export_html` without
  filtering (`crates/napkin-host/src/ops/read.rs:192-222`), and `export.rs` in
  clan-sdk never mentions licences. Brief Maker's own export builds from every
  filled field (`app/templates/brief-maker/index.html:1849-1893`). Separately,
  the lock `approve` records the version but not the content hash that
  Contract 4 §7 promises (`review.rs:1033` against `os-layer.md:322`).

---

## 1. What outside practice does

| Tool | Guest without an account | Unit of approval | Identity | Versioning |
|---|---|---|---|---|
| Ziflow | Yes: invitation email or public link | Whole proof. Decisions: Approved, Approved with changes, Changes required, Not relevant, Pending | Public link: the guest types an email. With "authentication required", Ziflow emails a one-time code | A new version locks the old ones. "Not relevant" lets someone who approved v1 skip v2 when v2 did not touch their part |
| Filestage | Yes, unlimited reviewers | Per file, per version: "Approve version" or "Request changes" | Invited reviewers | Status per version, with history |
| Frame.io | Yes, review links | Per asset: Approved, In progress, Needs review | Self-declared name and email at the first comment, remembered for the session only. Optional passphrase | Per asset |
| Workfront Proof | Share to external clients after internal review | Whole proof. "Approved with changes" means ready once changed, without another look | Invited reviewers | Per proof version |
| Google Drive approvals | **No**: each approver needs a Google account, external approvers need an admin setting, and only some plans have it | Whole file | Google account | Optional "require all approvers to review the same content": any edit resets every approval. Newer "alignment approvals" do not reset on edit |
| PandaDoc | External recipients | Whole document; a rejection means edit and resend | Recipient email | Locked during approval |
| Jira Service Management | Approve and Decline buttons in the email. Decline asks for a comment. Portal login may be prompted | Whole request | Portal account | n/a |
| DocuSign | Yes | Whole envelope, with signature fields | Email by default. Optional access code, SMS or phone code, knowledge-based checks | Certificate of completion: envelope id, signer email, IP address, timestamps |

Sources: Ziflow guests and decisions:
https://help.ziflow.com/hc/en-us/articles/40009664245268-Open-proofs-as-a-guest,
https://help.ziflow.com/hc/en-us/articles/30725285311508-Complete-a-review-or-submit-a-decision,
https://help.ziflow.com/hc/en-us/articles/30725020191764-Proof-security-and-sharing-settings,
https://www.ziflow.com/version-management,
https://help.ziflow.com/en/articles/5809937-finishing-a-review-understanding-decisions-in-the-legacy-ziflow-viewer.
Filestage: https://filestage.io/document-approval-software/,
https://help.filestage.io/en/articles/9112521-how-to-share-content-with-reviewers-and-control-their-permissions,
https://filestage.io/blog/review-and-approval/.
Frame.io: https://support.frame.io/en/articles/1161479-review-links-explained-for-clients-legacy,
https://support.frame.io/en/articles/414306-sharing-your-files-and-folders-for-review-legacy.
Workfront: https://experienceleague.adobe.com/en/docs/workfront/using/review-and-approve-work/proofing/review-proofs-in-workfront/make-decision-on-proof/make-decisions-on-proof,
https://experienceleague.adobe.com/en/docs/workfront/using/workfront-proof/get-started-wf-proof/workflow-examples/internal-external-review.
Google: https://support.google.com/drive/answer/9387535?hl=en&co=GENIE.Platform%3DDesktop,
https://knowledge.workspace.google.com/admin/drive/manage-approvals,
https://workspaceupdates.googleblog.com/2026/06/request-lightweight-document-alignment-with%20approvals%20in%20Google%20Drive.html.
PandaDoc: https://support.pandadoc.com/en/articles/9714799-approval-workflow.
Jira: https://support.atlassian.com/jira-service-management-cloud/docs/manage-settings-for-approval-by-email/,
https://community.atlassian.com/forums/Jira-Service-Management-articles/Announcing-approval-by-email-in-Jira-Service-Management/ba-p/1678334.
DocuSign: https://support.docusign.com/s/document-item?language=en_US&bundleId=oeq1643226594604&topicId=gpa1578456339545.html,
https://developers.docusign.com/docs/esign-rest-api/esign101/concepts/recipients/auth/.

What this adds up to:

1. **Per-section approval is rare.** The proofing tools approve a file, proof
   or asset as a whole. Per-part feedback travels as comments, and Ziflow's
   "Not relevant" works around re-approving parts nobody changed. Our
   addressable fields (`<doc-id>#path`, `os-layer.md:92-99`) make per-section
   approval cheap for us. That is an advantage, but a whole-document "sign off"
   is still needed, because that is what clients and contracts understand.
2. **Accountless guest review is table stakes.** Only Google needs an account,
   and that is a known pain point. The accountless tools either accept a typed
   email (Frame.io, Ziflow public link) or add an emailed one-time code
   (Ziflow "authentication required", DocuSign options).
3. **A decision is bound to a version.** Tools either reset approvals on any
   edit (Google's "same content" option) or keep them on the old version and
   ask again (Ziflow, Filestage). Nobody carries an approval onto changed
   content.
4. **"Approved with changes" is a real third state** (Ziflow, Workfront): go
   ahead once these edits are made, no further look needed.
5. **Legal weight.** A click or an email "approved" is a simple electronic
   signature under eIDAS: it is admissible, but its weight depends on the
   evidence of identity, intent and integrity
   (https://ec.europa.eu/digital-building-blocks/sites/spaces/DIGITAL/pages/880312429/eSignature+FAQ,
   https://blog.eevidence.com/en/what-is-a-simple-electronic-signature-and-when-is-it-legally-valid/).
   E-signature services add a certificate of completion with the email, IP
   address and timestamps. Agency practice asks for the same record: file,
   version, reviewer identity, timestamp, outcome. It also says to confirm
   verbal changes back in writing
   (https://fileapproved.com/blog/creative-proof-approval-checklist,
   https://www.creativeagencybook.com/blog/scope-creep-how-to-prevent-it).
6. **What gets clients to respond.** Few clicks from the email, due dates with
   automatic reminders (Filestage's pitch:
   https://help.filestage.io/en/articles/3161157-stay-on-top-of-your-due-dates-as-a-reviewer),
   and pages that work on a phone: most email is now opened on mobile
   (https://www.litmus.com/email-client-market-share). An account or login
   step is the biggest drop-off, which is why Google's account requirement
   stands out.
7. **Email identity is weak unless checked.** The From header is trivially
   spoofed. DKIM signs the message and DMARC aligns the From domain, so a
   forwarded `.eml` with a passing, aligned DKIM signature is real evidence,
   while pasted text is not
   (https://www.valimail.com/blog/dmarc-dkim-spf-explained/,
   https://www.trustedsec.com/blog/real-or-fake-spoof-proofing-email-with-spf-dkim-and-dmarc).
8. **Magic links have known failure modes.** Mail security gateways prefetch
   links and can consume single-use tokens, and links get forwarded or leak
   through the Referer header. The fix is that a GET only shows the page and a
   POST records the action
   (https://mojoauth.com/blog/are-magic-links-secure-technical-deep-dive,
   https://obie.medium.com/prefetching-breaks-magic-link-password-less-login-systems-unless-you-take-precautions-a4c011a3e165).

---

## 2. What exists in the OS today

- **Decisions.** One primitive with `kind`, `targets[]`, `cites[]`,
  `superseded_by`, the actor from `Ctx`, `polarity` and `rationale`
  (`os-layer.md:127-145`). Unknown fields survive a read-modify-write
  (`crates/clan-sdk/src/decision.rs:127-130`, `extra`). A new kind with new
  fields is therefore additive.
- **`rationale` is not safe for verbatim quotes.** It is the field compression
  rewrites (`decision.rs:108-112`), so a client's exact words must live in
  their own field.
- **Actors.** `Actor::parse` accepts only `human:`, `process:` and
  `<producer>/<version>` (`crates/napkin-host/src/ctx.rs:41-65`). A
  `client:<email>` actor is refused today. `who()` knows only `person` and
  `agent` (`decisions.rs:76-85`, `decisions.rs:944-970`).
- **Person-only review operations.** `person()` refuses anyone who is not a
  human (`review.rs:175-183`). `not_locked()` refuses every review operation
  after lock (`review.rs:202-212`).
- **Lock.** `approve` is refused while the lock list is non-empty. It records
  one `approve` decision with the version (`review.rs:998-1034`), but no
  content hash, although Contract 4 says "exact version and content hash"
  (`os-layer.md:321-323`). Any unsuperseded `approve` counts as the lock
  (`review.rs:195-200`).
- **Lock list.** Open contests, unmerged branches, unverified findings, fields
  citing rejected findings, and bad verdicts nobody has answered. A bad
  verdict is answered by a later edit or a reasoned good verdict on the same
  address (`decisions.rs:616`, `decisions.rs:793-823`, using `later()` at
  `decisions.rs:365-371`). This is exactly the "answered" rule a client change
  request needs.
- **Staleness.** Documents have a version (`document.rs:137`) and the manifest
  carries `sha256` (`app/src/host/types.ts:27-35`). The offline copy stores
  `revision` and `sha256` (`app/src/offline/actions.ts:21-38`). "Present
  offline" is a read-only snapshot (`os-layer.md:66-70`,
  `app/src/components/Toolbar.tsx:187-190`).
- **Export.** Path A is declarative, Path B is the app's own HTML
  (`app/templates/EXPORT-CONTRACT.md:35-88`). Brief Maker uses Path B and
  groups fields by `sec` (`index.html:684-702`, `1857-1893`). It stamps the
  date only: no revision, no hash, no section ids. Contract 4 says `classify`
  is enforced in `compose_export` (`os-layer.md:210-212`), but the code does
  not do it (`read.rs:192-222`).
- **Evidence storage.** `/upload-asset` stores a binary inside the document
  (`crates/napkin-host/src/routes.rs:289-292`, `session.rs:438`). The host
  already extracts text from uploaded PDFs (memory: attachment text
  extraction).
- **Edit mode** already requires a reason on every person's edit
  (`review.rs:694-704`, `clan-fields.md:77-101`). That is the natural place
  for "Client asked for this" when staff revise a section in response.
- **Spin-off carries the whole chain**, approvals included (`os-layer.md:261-268`).
  A client's approval of the brief's proposition is therefore visible in the
  deck made from it.
- **App-level lock state.** Brief Maker has its own `locked` and
  `locked_fields` in its schema (`app/templates/brief-maker/schema.json`), and
  a `review` block written only by the middleware (the Judge). Neither may
  reach a client view.

---

## 3. Ways a client approval can reach the system

Ranked from least to most total effort. "Proves" is what the record can stand
behind later.

| # | Channel | Agency effort | Client effort | Proves |
|---|---|---|---|---|
| 1 | **Staff records a phone call or meeting**: "Record client feedback", sections, Approved or Changes, a note | 1 dialog, about 20 s | None | Only that a named staff member says the client said it, at a time. Weakest. Mark it "recorded by Aoife, not confirmed by the client" |
| 2 | **Staff pastes the client's email text** into the same dialog | 1 dialog plus a paste | None (they already replied) | The words, as a claim. Anyone can type them, so identity is not proven |
| 3 | **Staff drops the forwarded `.eml`** (or "Forward as attachment") | 1 dialog plus a drag | None | With a DKIM signature that passes and aligns with the From domain: the client's domain sent these exact words, at this time. Strong for what it costs. Without DKIM, the same as #2 |
| 4 | **Staff uploads the client's marked-up or signed PDF** | 1 dialog plus a drag | Whatever they already do with PDFs | That this PDF came back. It is tied to a revision **only if our export stamps the revision and hash** on every page, which it does not today. Identity is still the staff member's claim unless it came as #3 |
| 5 | **No-account review link** (Napkin emails the link, or staff copy it into their own email) | 1 click "Send for client review", choose sections and the recipient | Open the link, tap per section, type a reason for changes, enter an emailed code once | Control of the invited inbox at that moment (with the code), the exact section hashes shown, per-section intent, a verbatim reason, a timestamp and the IP address. The strongest practical record |
| 6 | **Approve by reply**: Napkin sends an email; the client replies "approve" or writes changes | Nothing after sending | Reply, the easiest possible | Needs inbound mail, DKIM checking and parsing of free text. Per-section is unreliable ("all fine except the audience"), so an agent would propose and staff confirm |
| 7 | **E-signature** (DocuSign and the like) on the final export | Send an envelope | Sign | Legally strongest, but whole-document only, costly and heavy. Only for a contractual sign-off, and possible without us |

Recommendation: build #1 to #4 as **one dialog** (the channel is a field, and
the evidence is optional but encouraged). Build #5 next. Treat #6 as a later
convenience on top of #5's email. Leave #7 outside: link an envelope id as
evidence if an agency uses one.

---

## 4. Recommended design

### 4.1 The decision

A new kind, `client_review`. Do not reuse `approve`, which is the lock
(`review.rs:195`), or `verdict`: see §0 and the reasons below.

```yaml
- id: d_01K…                       # new_decision_id()
  kind: client_review
  action: client_approved          # | client_changes_requested | client_signed_off | client_withdrawn
  polarity: good                   # good = approved, bad = changes requested
  targets:                         # section addresses; for sign-off, the doc id
  - 3f2a…#single_minded_proposition
  - 3f2a…#reasons_to_believe
  actor: human:aoife               # who recorded it (channels 1-4), from Ctx
                                   # or client:jane@acme.ie via the link (§4.5)
  client:
    email: jane@acme.ie            # lower-cased; the identity the approval is attributed to
    name: Jane Murphy              # optional, as given
  channel: email                   # phone | meeting | email | pdf | link | reply
  said: "Love the proposition. RTB 3 needs the 2025 figure, not 2023."   # verbatim, never compressed
  said_at: 2026-09-29T10:14:00Z    # when the client said it; timestamp is when it was recorded
  seen:                            # what the client was looking at
    version: "7"
    doc_sha256: 9c1e…
    sections:                      # sha256 of the canonical JSON of each target's value
      single_minded_proposition: 41ab…
      reasons_to_believe: 77d0…
    export: exp_…                  # the stamped export the client had, when known
  evidence:
  - { asset: sha256:ab12…, kind: eml, dkim: pass, dkim_domain: acme.ie }
  - { asset: sha256:cd34…, kind: pdf }
  rationale: "Client approved the proposition; asked for a change to reasons to believe."   # the one-line summary older readers show
  timestamp: 2026-09-29T10:20:03Z
  scope: { org: …, brand: … }      # from Ctx, as ever
```

Rules:

- `said` is required when `polarity: bad`, just as a bad verdict needs its
  reason. When the channel is phone or meeting, staff type the gist and the
  dialog labels it "in Aoife's words".
- One decision may carry mixed outcomes ("approved A, changes on B"). The
  dialog writes **one decision per outcome**, both citing the same evidence
  asset, so every decision has one polarity.
- A later `client_review` by the same client on the same address
  **supersedes** the earlier one (`superseded_by`), and the history stays.
- `client_withdrawn`: the client takes back an approval. It is recorded like
  the others and supersedes the earlier one.
- Why not `verdict`: verdicts feed the quality dossier, where good means an
  exemplar and bad means a constraint (`os-layer.md:217-221`). A reasoned good
  verdict also answers a bad one (`decisions.rs:806`, `938-942`), so a client
  "approved" would silently clear the Judge's objection. Client opinion is a
  different signal and should stay separate. It is good RAG context of its
  own.

### 4.2 How it shows on a section

The host derives per-section client state in `/decisions` (a new
`client: {state, by, at, said, stale}` per target) so every app shows it the
same way. `<clan-tray>` and the drawer show it, and Brief Maker shows a chip
on each section heading.

| State | Chip | When |
|---|---|---|
| none | nothing, or "Not sent to client" in client-review mode | no `client_review` on the address |
| sent | "With client · sent 2 days ago" | an open review link or send record covers it |
| approved | **Client approved** · Jane, 29 Sept | the latest unsuperseded review is good **and** the current section hash equals `seen.sections[path]` |
| changes | **Client asked for changes** · the reason inline | latest is bad and not yet answered |
| answered | "Changes made · awaiting client" | latest is bad and the section has since been edited (hash changed) or staff replied with a reason |
| stale | **Client approved an earlier version** (struck-through tick) | latest is good but the section hash has changed |

Staleness works on the **section hash, not the document version**: editing the
audience does not stale the client's approval of the proposition. That is the
"Not relevant" problem Ziflow solves by hand, solved automatically. The
document-level `seen.version` and `doc_sha256` stay on the record for
evidence.

For a research report laid out by an agent, the text keys
(`report:<layout hash>:b<n>`, `clan-fields.md:90-95`) change whenever the
layout changes, so they are not stable sections. Client review of a report
targets data paths, pins or findings. A block-level review falls back to a
whole-document review of that revision.

### 4.3 Interaction with the agency's lock

- **A client change request blocks the lock** until it is answered. Add item 6
  to `lock_blockers` (`decisions.rs:616`): an unsuperseded bad `client_review`
  whose target has no later edit and no staff reply with a reason. It is the
  same predicate as item 5, applied to a different kind. The staff reply ("we
  kept 2023; the 2025 figure is not published") is itself a decision and goes
  back to the client in the next round.
- **Client approval is not required to lock** by default. The lock means the
  agency accepts its own work (D7), and many briefs are locked before the
  client sees the final PDF. An app or tenant may opt into "lock requires
  client sign-off" (see Q1).
- **"Client signed off" is a separate, document-level state**: a
  `client_signed_off` decision whose `seen.doc_sha256` equals the locked
  hash. Shown next to the lock as "Locked by Aoife · Signed off by Jane (acme.ie),
  by link". Because it changes no content, it must be **allowed after lock**.
  This is an exception to `not_locked()` (`review.rs:202`) for this one
  append-only kind, which is consistent with "decision appends in general" not
  needing the lease (`os-layer.md:302`).
- **A client asks for changes after lock**: the change request is recorded (an
  append) and shows as "Client asked for changes on a locked version". Acting
  on it needs a new version, and what unsealing means is still open
  (`os-layer.md:335`, W5-Z1). This design does not settle it.
- The lock `approve` should also record `doc_sha256`, as §7 promises, so
  sign-off can be matched to the lock (`review.rs:1033`).

### 4.4 Recording phone or email feedback in one or two taps

Entry points: a **"Client said…"** button in the shell toolbar next to Present
offline and Export, and on each section chip.

1. Tap "Client said…" on a section chip. The section is pre-selected; more can
   be ticked. The client field is remembered per document.
2. Choose **Approved**, **Asked for changes**, or Approved with changes (a
   good polarity plus `said`).
3. Paste or drop the evidence. A pasted email body goes to `said`. A dropped
   `.eml` is stored with `/upload-asset`, its DKIM is checked on the server,
   and the body is pre-filled into `said`. A dropped PDF is stored and its text
   extracted; if it carries our revision stamp (§4.6), `seen` fills itself.
4. "Which version did they see?" defaults to the last version sent or exported
   to that client, and the current one otherwise. If the section changed since
   then, the dialog says so: "They saw version 5; the proposition has changed
   since. Record against version 5?"
5. Save. For an approval with the email pasted, that is two taps plus a paste.

When the client asked for changes, the dialog offers **"Make the change
now"**. It opens edit mode on that section, with the edit reason pre-filled
"Client asked: <said>" and the edit citing the `client_review` id. The lock
item then clears itself.

### 4.5 The optional no-account review link

- **Send.** "Send for client review" picks the sections (defaulting to every
  section the app marks client-facing), the recipient email, and a due date
  (optional; default 5 working days). The server freezes a **client view** of
  the current version (§5, confidentiality) and creates a link record:
  `{link_id, doc, version, doc_sha256, sections: {path: hash}, recipient,
  expires_at, created_by}`. The token is 128 random bits, stored hashed, and
  sits in the URL path. The recipient gets an email from Napkin "on behalf of
  <agency>", or staff copy the link into their own email.
- **The page.** Mobile-first. The agency's identity is shown, not the Napkin
  mark: Napkin's logo is OS-only (memory). Each section appears with two
  buttons: **Approve** and **Ask for a change** (a text box, required). One
  button at the top, "Approve all remaining", records a `client_signed_off`
  when every section is approved.
- **Recording.** A GET only renders the page. Every decision is a POST that
  carries the section hash the page rendered, and the server refuses a
  mismatch. The **first POST in a session** asks for a 6-digit code emailed to
  the invited address (Ziflow's "authentication required", DocuSign's access
  code), which stops prefetch scanners and forwarded links. The decision's
  actor is `client:<email>`, which needs a fourth actor shape in `Actor::parse`
  (`ctx.rs:41-65`) and a `client` kind in `Who` (`decisions.rs:76-85`). The
  recorded evidence is the link id, the code-verified email, the IP address,
  the user agent and the time.
- **A newer version.** If the document moves on while the link is open, the
  link still shows what was sent. The agency can **"Update the link"** to the
  new version. Sections whose hash did not change keep the client's decisions,
  and changed ones come back as "changed since you looked". The server never
  moves a decision onto content the client did not see.
- **Expiry and scope.** Default 14 days, revocable, one recipient per link
  (send two links for two people), the listed sections only, read-only
  otherwise. No other document, no chain, no evidence drawer, no agent
  reasoning.
- **Reminders.** One automatic nudge 2 working days before the due date and
  one on it, both off by default per tenant, plus a "Nudge" button for staff.

### 4.6 Export ties paper to a revision

Stamp every export page (the Path A OS footer, and a helper for Path B apps)
with `Revision 7 · 9c1e…` (a short hash) and give each section a visible label.
A PDF that comes back annotated, or an email reply saying "approved rev 7", can
then be matched to a version, and §4.4 step 4 can fill itself. Section hashes
of that export go in a small `exports` record (`exp_…`), so `seen` can point
at it.

### 4.7 In clan-extract (RAG)

One line per client review, with the verbatim words and the evidence strength
spelled out:

> **Client review** of *Reasons to believe* (revision 7): **asked for a change**.
> Jane Murphy (jane@acme.ie) said by email, DKIM-verified for acme.ie, recorded
> by Aoife: "RTB 3 needs the 2025 figure, not 2023." Answered in revision 8:
> Aoife revised it ("client asked: …").

The strength wording ("said on a call, per Aoife", "email, not verified",
"email, DKIM-verified", "by review link, email code-verified") keeps retrieval
from treating a claim as proof. It is the same discipline as "findings need
human verification". Client reviews travel with spin-off unchanged
(`os-layer.md:265`).

---

## 5. Minimum first version, and what to defer

**Build first (v1):**

1. The `client_review` kind: the fields in §4.1 and a host route
   `/client-review {targets, polarity, client, channel, said, said_at,
   seen_version?, evidence[]}`, recorded as the staff member in `Ctx`. The
   host computes `seen.sections` from the named version. It is allowed after
   lock for sign-off only.
2. The per-section state (§4.2) in `/decisions`, the chip in the tray and
   drawer, and in Brief Maker's section headings.
3. Lock item 6 (§4.3): an unanswered client change request blocks the lock.
4. The "Client said…" dialog (§4.4) with paste or file drop through
   `/upload-asset`. A `.eml` is stored; checking DKIM can come in v1.1.
5. The revision and hash stamp on exports (§4.6), plus record the hash on the
   lock `approve`.
6. **The export confidentiality fix** (§6.3): a prerequisite, because clients
   already get exports today.
7. The clan-extract rendering (§4.7), once that contract lands.

**Defer:**

- The review link with the email code (§4.5): v2, the first thing after v1.
- DKIM checking of dropped `.eml` files: v1.1, small.
- Reminders and due dates.
- Approve by reply, which needs inbound mail and an agent that proposes and a
  person who confirms.
- Several client approvers and rules like "legal and marketing must both
  approve".
- E-signature integration: link an envelope id as evidence only.
- Lock requiring client sign-off (a tenant or app option, Q1).
- What unsealing means for a change requested after lock (W5-Z1).

---

## 6. Risks

1. **Spoofing (channels 2 to 4).** Pasted text and a PDF prove only what the
   staff member claims. Mitigation: the record always states the channel and
   the strength, and never says "Client approved" without "recorded by X".
   Check DKIM on a `.eml`. The review link with its email code is the strong
   path. Staff cannot record a `client:` actor; only the link route can.
2. **Approving a stale revision.** A client approves the PDF from last week
   after the section changed. Mitigation: `seen` is required, and the dialog
   defaults to the version last sent, not the current one. The chip goes stale
   by section hash. The link POST carries the rendered hash and a mismatch is
   refused. An approval is never moved onto changed content.
3. **Confidential fields reaching a client.** Today `compose_export` does not
   filter by `classify` export marks (`read.rs:192-222`), and Brief Maker's
   Path B export prints every filled field (`index.html:1857-1866`). Internal
   material (`review` Judge verdicts, `materials`, `passages`, agent reasoning,
   sources marked client-confidential or internal) must never be on the client
   page. Mitigation: a client view built by the OS from an explicit
   **client-facing section list** the app declares (allow-list, not
   block-list), with `export: false` marks removed, and a "Preview as client"
   before anything is sent.
4. **A forwarded link.** Anyone with the link could act as the client.
   Mitigation: the emailed code on the first POST, one recipient per link,
   expiry, revocation, and every decision stamped with the verified email, IP
   address and user agent. Viewing without the code is still possible, so the
   sections shown must already be safe for anyone at the client (risk 3).
5. **Mail scanners prefetching links**: a GET never changes anything. The
   Referer leak: `Referrer-Policy: no-referrer` on the page, and no outbound
   links.
6. **Section drift.** Section addresses must be stable data paths. Agent
   report layouts are not (§4.2). If an app renames a field between majors,
   old reviews point at nothing; show them as "on a field this version no
   longer has".
7. **Legal over-reach.** A section tick is a simple electronic signature with
   modest weight. Do not call it a "signature" in the UI. Call it
   "sign-off", and point to e-signature for contracts.
8. **Tenancy.** The link must resolve to exactly one tenant and one document,
   and the `client:` actor carries the scope of the link's creator. A link
   token must never grant any other route (`os-layer.md:44-46`: scope is never
   from the body).

---

## 7. Questions only the owner can answer

1. **Must the client sign off before the agency can lock?** Recommended
   default: no. Client change requests block the lock, but approval is not
   required, and "Signed off" is shown beside the lock. A tenant can opt in.
2. **Does the review link require the emailed one-time code before any
   decision?** Recommended default: yes, once per device per link. It costs
   one email and closes both the forwarding and the scanner problems. Without
   it, a link approval is only as strong as a pasted email.
3. **Who counts as "the client" for a document: one named approver, or anyone
   at the client's email domain?** Recommended default: named recipients only,
   one per link, with the domain shown. "Anyone @acme.ie" is a later option.
4. **Is "Approved with changes" (go ahead once edited, no second look) a state
   we offer clients?** Recommended default: yes, as good polarity plus a
   required `said`. The section shows "Approved, with changes to make", and
   the lock is not blocked. It matches how agencies already work (Ziflow,
   Workfront).
