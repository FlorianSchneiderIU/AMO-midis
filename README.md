# AMO Midis

This Flask app lets you upload MuseScore `.mscz` files. Each upload is converted to OGG and MusicXML using the MuseScore command-line tool. Users can rate the generated audio and view the score rendered with OpenSheetMusicDisplay.

## Model Arena

The `/arena` route introduces a blind A/B testing flow for comparing model outputs of the same piece:

- Tracks are grouped by their piece metadata and randomly assigned as **Model A** or **Model B** for each match.
- Participants select the version they prefer and optionally provide qualitative feedback about their choice.
- Results are stored in `model_arena_matches.csv`, capturing the winner, feedback, and metadata for later statistical analysis.
- After submitting a verdict, testers can immediately request another comparison for the same song or jump to a brand new piece.

The regular rating page now links to the Model Arena so listeners can seamlessly switch between single-track scoring and head-to-head comparisons.
