# Research Tool experiment

The Research Tool rebuilt on the OS's clan-* fields, with the decision chain a
person can read and edit. The branch holds the code (on top of
`feature/studio-start`) and these working documents, each a standalone page:

- `research-tool-flow.html`: how a research job runs, stage by stage.
- `research-agents.html`: a review of the research agents, with the open bugs.
- `clan-fields-report.html`: the clan-* field components and the agent-laid-out report.
- `decisions-panel.html`: the mock of the decision panel the shell now implements.

What the code does:

- The OS's fields (`clan-field`, `clan-chart`, `clan-quote`, `clan-gap`,
  `clan-sources`, `clan-cite`) show recorded data and carry its review.
- The report is laid out by an agent over the record, checked by
  `server/napkin/rules/layout.py`; the tool has a working mode and a done mode.
- A document carries its evidence: each source's record and each pin's quote.
- Edit mode for every app: a person rewrites wording or corrects a fact; each
  edit asks why and records what it was and what it is now.
- The decision panel says who did what and why, shows the change, can ask an
  agent to redo a step, and can show the edit in the document.
