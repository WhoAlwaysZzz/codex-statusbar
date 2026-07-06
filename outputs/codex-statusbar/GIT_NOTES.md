# Git Notes

## 2026-07-06: statusbar UI commit was too coarse

Mistake:

- I put tray minimize, mini mode, display truncation, session naming, and flicker reduction into one commit.
- That made the regression harder to isolate when mini/full switching clipped the right-side close button.
- This was not good git hygiene. It followed the rhythm of one user request, not the internal shape of the work.

Better rule:

- Commit by coherent change, not by chat turn.
- Split independent UI features into separate commits when they can fail independently.
- Put bug fixes in their own commits with focused tests or focused manual verification.
- When a request contains several separable features, implement and verify in small batches before committing.

Practical reminder:

- A good commit should answer one question: "what behavior changed?"
- If the answer needs "and", the commit is probably too broad.
