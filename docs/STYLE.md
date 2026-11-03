# Wording style

VirtualDispatch is airline operations software. Its text should read like
an operations system and an operations manual, not like a hobby app. These
rules apply to every new or changed string.

## Language

- British English, as in ICAO and EASA documents: centre, cancelled,
  programme, licence (noun), kilogram.
- No contractions: "do not", "cannot", "is not".
- No exclamation marks, no "please", no "just", "simply", "amazing",
  "awesome", "straight", "a bit", and no jokes.
- Address the pilot in the imperative ("Sign the loadsheet"). Use "you" or
  "your" only where a sentence cannot work without it.

## In the app: operations register

Everything the app shows while it is used (labels, buttons, statuses, empty
states, notes, confirmations and errors) is written like an airline EFB or
ACARS message.

- UPPERCASE.
- Telegraphic: no articles or auxiliary verbs where the meaning survives
  without them. "OFP NOT RECEIVED", not "The OFP has not been received yet".
- Standard abbreviations (ICAO Doc 8400 and those in the app's glossary):
  OFP, NOTAM, MEL, CDL, LMC, DG, NOTOC, PIREP, ATIS, RWY, ARR, DEP, EOBT,
  ETOT, ZFW, TOW, LW, FOB, WX, ACFT, FL, CTOT, SID, STAR, ATC, UTC/Z.
- A message is a state, then the action: "OFP NOT FOUND FOR SIMBRIEF USER.
  GENERATE OFP ON SIMBRIEF, THEN RETRY"
- Full stops separate sentences; the last one never ends with one (checked
  by tools/check_ui_copy.py): "OFP NOT FOUND. GENERATE OFP ON SIMBRIEF".
- Times as 1050Z, local times marked LT; weights with their unit (KG, LB);
  durations as 1H 05M or 25 MIN.
- Names of other services are uppercased with the rest (SIMBRIEF,
  NAVIGRAPH, SAYINTENTIONS); on public pages they keep their own spelling
  (SimBrief, Navigraph, SayIntentions).

Texts that come from data (MEL and CDL wording, delay, LMC and dangerous
goods descriptions, curfew notes, airport names) stay in sentence case in
the data files, where they are also the reference wording; the app shows
everything on its pages uppercase (`#mainContent` in app.css). Values the
pilot types are shown as typed.

Glossary definitions (the DISPATCH TERMS panel) are the one in-app exception:
short sentences in sentence case, because they explain rather than report.

## Public pages

The landing page, sign-in and account pages, onboarding, emails and legal
pages are read before the app is used. They are written in sentence case,
in complete sentences, in the same professional register: descriptive,
brief, no marketing superlatives.

Legal pages change only for spelling and contractions unless the change is
reviewed as a legal change.

## Examples

| Not | But |
| --- | --- |
| Waiting for the SimBrief OFP: the departure and destination NOTAMs come with it | NOTAMS PENDING OFP |
| Could not fetch an OFP from SimBrief for that username. Make sure it's been generated there first | OFP NOT FOUND FOR SIMBRIEF USER. GENERATE OFP ON SIMBRIEF, THEN RETRY |
| The PIREP opens once the loadsheet for this leg is signed | PIREP AVAILABLE AFTER LOADSHEET SIGNATURE |
| SimBrief can't be reached right now | SIMBRIEF NOT AVAILABLE. RETRY IN 1 MIN |
