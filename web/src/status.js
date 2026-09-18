// Where a novel is in its life. Mirrors NOVEL_STATUSES in server/projects.py, which is
// the authority — the server refuses anything not in its own list, so a value added here
// and not there is a 400 rather than a silent miswrite.
//
// Purely a label: nothing in the translation or posting pipeline reads it. It exists
// because a shelf of fifty-odd novels gives no way to tell the three being actively
// worked on from the forty that are finished.
export const STATUSES = ['ongoing', 'completed', 'hiatus']

export const STATUS_LABEL = {
  ongoing: 'Ongoing',
  completed: 'Completed',
  hiatus: 'Hiatus',
}

// Reuses the existing chapter-badge palette rather than inventing colours: green already
// means "done" everywhere else in this app, and grey already means "set aside".
export const STATUS_PILL = {
  ongoing: 'pill-queued',
  completed: 'pill-translated',
  hiatus: 'pill-muted',
}
