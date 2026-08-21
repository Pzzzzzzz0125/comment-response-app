# Permit Precedents — Coworker Demo Guide

Audience: coworkers who may use the app and do not need technical implementation details
Recommended length: 6–7 minutes
App: [http://localhost:8010/](http://localhost:8010/)

## What the audience should remember

Permit Precedents helps us reuse prior review knowledge without searching through folders and opening files one by one. A user can:

- understand a city's historical review patterns;
- ask a practical question in everyday language;
- find prior government comments and company responses;
- follow an issue across multiple review rounds;
- open the original PDF, spreadsheet, or Word source to verify the evidence.

The app is a research and verification tool. It helps users find relevant history faster, but users should still check the cited source before relying on a precedent.

## Before the meeting

1. Open [http://localhost:8010/](http://localhost:8010/) and wait for the page to finish loading.
2. Select **San Jose** in the city selector.
3. Keep this question ready to paste into the chat:

   `How have we handled tree-related comments?`

4. If the AI service is temporarily unavailable, use the Library search for `tree`, `fire separation`, or a project address. The source records remain available even when an AI answer cannot be generated.
5. Do not start with the Import Data flow during a short demo. Mention it at the end as the way future project folders can be added.

## Live demo script

### 0:00–0:40 — Introduce the problem

Say:

> We receive many permit comments and responses across different projects, rounds, and file types. When a similar comment appears again, it can take a long time to remember what happened previously and locate the original evidence. Permit Precedents brings that history into one place.

Show the main page without clicking yet.

### 0:40–1:20 — Start with the city summary

Action:

- Select **San Jose**.
- Point to the city summary and the high-level counts.

Say:

> The app is organized by city, so the history stays relevant to the selected jurisdiction. The summary gives us a quick sense of the available projects, comments, response coverage, and common areas of review.

Do not explain how the counts are calculated unless someone asks.

### 1:20–2:50 — Ask the historical knowledge assistant

Action:

- Open the AI chat.
- Ask: **How have we handled tree-related comments?**
- Let the answer finish.

Say:

> I can ask a normal work question instead of guessing the exact filename or keyword. The answer begins with the historical pattern, explains what actions were recorded, and includes counts so I understand the size of the evidence set.

Point to the numbered citations.

> The citations matter. The answer is not meant to replace the source documents; it is a faster path to the records that support each statement.

If the app suggests follow-up questions, say:

> These suggestions help a user move from a broad question to a more specific project, requirement, or comparison.

### 2:50–3:50 — Verify a source

Action:

- Click one inline citation or supporting source.
- Show the Library-style evidence panel.
- Click **Open original source**.

Say:

> This panel shows the government comment, the recorded company response, and any later review history together. I can then open the original source. The viewer takes me to the cited page or spreadsheet range and highlights the supporting evidence when location information is available.

Close the viewer and return to the same chat.

### 3:50–5:10 — Browse the historical Library

Action:

- Close or collapse the expanded chat.
- In the Library, search for `fire separation` or choose a visible issue with a response.
- Select a record in the left list.

Say:

> The Library is useful when I want to browse directly instead of asking a question. The left side is the set of historical issues, and the right side shows the selected comment and response. The colored response labels make it easy to distinguish records with a confirmed response from records where no response is stored.

Point to filters, but do not open every filter.

> I can narrow the list by project, discipline, review round, response status, or whether the issue has a longer history.

### 5:10–6:10 — Explain recurring issues

Action:

- Open an issue that has **Review history** or choose a Recurring Issue card.
- Show its timeline.

Say:

> A recurring issue is one specific design concern that continued through more than a simple comment-and-response pair. The timeline keeps the original comment, applicant responses, and reviewer follow-ups in date order. If the same event appeared in several files, it is shown once with multiple source links instead of being counted repeatedly.

Then contrast it briefly:

> Common Topics answer a broader question—what areas do reviewers discuss often? Recurring Issues answer a project-level question—what exact problem continued across reviews, and what happened next?

### 6:10–6:50 — Close with the user benefit

Say:

> The main value is faster, more reliable access to institutional history. We can begin with a city overview, ask a question, inspect a prior response, follow the review history, and verify everything in the original files. Future project folders can also be uploaded through the Import Data action so the knowledge base can continue to grow.

Ask:

> Which type of search or historical question would be most useful in your day-to-day work?

## Short 3-minute version

If time is limited:

1. Explain the problem in 20 seconds.
2. Select San Jose and show the city summary in 25 seconds.
3. Ask the tree-related question and explain the answer in 60 seconds.
4. Open one citation and one original source in 50 seconds.
5. Show one recurring-issue timeline in 35 seconds.
6. Close with the benefit and ask for feedback in 30 seconds.

## Good questions to demonstrate

- `How have we handled tree-related comments?`
- `What fire-separation issues have appeared in past projects?`
- `Show the history of this issue across review rounds.`
- `Which historical comments have confirmed responses?`
- `What source supports this response?`

Use the first question for the planned demo. Treat the others as optional follow-ups depending on the records available in the selected city.

## If something goes wrong

### The AI answer is slow

Say:

> The app is checking the selected city's historical evidence. While it finishes, I can still use the Library to search the same records directly.

Then demonstrate Library search.

### The AI service is unavailable

Say:

> The source library is independent of the generated answer, so the underlying comments, responses, timelines, and source links remain available for review.

Do not repeatedly submit the same question.

### A citation has no highlight

Say:

> The app still opens the correct source and location. Highlight precision depends on the location information available from the original file.

### A record has no response

Say:

> The app distinguishes missing response evidence instead of inventing an answer. That makes the gap visible for follow-up or review.

## Presenter checklist

- [ ] App opens at port 8010.
- [ ] San Jose is selected.
- [ ] The demo question is copied and ready.
- [ ] One useful Library record and one recurring issue are identified.
- [ ] Browser zoom is at a comfortable level.
- [ ] Notifications and unrelated tabs are hidden.
- [ ] The PowerPoint is open before screen sharing.
- [ ] A fallback Library search is ready if the AI service is unavailable.
