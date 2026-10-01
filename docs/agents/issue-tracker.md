# Issue Tracker: Local Markdown

Issues, specs, and wayfinding maps live as markdown files in `.scratch/`.

## Conventions

- One feature/map per directory: `.scratch/<effort-slug>/`
- The map is `.scratch/<effort-slug>/map.md`
- Tickets are one file per ticket at `.scratch/<effort-slug>/issues/<NN>-<slug>.md`, numbered from `01`.
- A `Type:` line records the ticket type (`research` / `prototype` / `grilling` / `task`).
- A `Status:` line records `open` / `claimed` / `resolved`.
- A `Blocked by: NN, NN` line records dependencies. A ticket is unblocked when every issue it lists is `resolved`.
- Comments and conversation history append under `## Comments`.
- Resolution answers append under `## Answer`.
