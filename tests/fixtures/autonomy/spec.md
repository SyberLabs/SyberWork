# Order markers (fixture spec for the autonomy thin slice)

Sectioned Markdown with ids (owner decision 11, 2026-10-28). Each `##` section is one requirement of #154's spec
object; the bullets are its scenarios. The three features of `map.json` trace to these sections.

## R1: The rules module records that its decision thresholds were reviewed.

- given fxapp/rules.py, when the feature is delivered, then the module ends with the delivery marker for F1

## R2: The ingest module records that its normalization was reviewed after the rules.

- given fxapp/ingest.py, when the feature is delivered after F1, then the module ends with the delivery marker for F2

## R3: The variants module records that its entrypoints were reviewed.

- given fxapp/variants.py, when the feature is delivered, then the module ends with the delivery marker for F3
