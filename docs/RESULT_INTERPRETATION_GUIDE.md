# How to Read a WAD Result Package

This guide explains how to read a Website Asset Discovery (WAD) output package. Use it to review what WAD observed, how records connect, and which conclusions the data supports. A WAD inventory is a bounded observation of a website, not proof that every asset was found.

## 1. Understanding a WAD Run

A run produces a `Discovery_Report.md` and a `data/` directory with five CSV files. The report summarizes the run, its limits, source outcomes, and unchecked areas. `Asset_Inventory.csv` is the main register. The other CSVs hold uses, sources, observations, and typed relationships.

The report status describes the run as a whole:

- **COMPLETE** means all applicable stages of the intended survey finished. It does not mean that every existing asset was found or that every individual URL succeeded.
- **PARTIAL** means a mandatory part of the survey could not finish because of a limit, access restriction, technical blocker, or unavailable required runtime. WAD preserved a usable partial package and should identify the restriction or unchecked area.
- **FAILED** means WAD could not produce a usable output package. Some intermediate files may still exist; review the report and file list before drawing a conclusion.

A run status is different from an HTTP status or a row's `observation_status`. For example, a run can be PARTIAL even when all requests it actually sent succeeded, because a required page was correctly left uninspected under `robots.txt`.

**Counter note:** current reports list Source record counts and `observation_status` totals separately from HTTP attempts, successful requests, and unsuccessful requests. Older reports generated before this counter correction can mix those populations; do not interpret their document counters as HTTP request-failure counts.

## 2. Understanding the Five CSV Files

The CSVs use UTF-8 with a byte-order mark, comma delimiters, and CRLF line endings. One row represents one record in that file. Each row includes `schema_version` and `run_id`. WAD defines a minimum set of required fields. Additional columns may be absent. Use the header in your package as the definitive list of available fields.

| File | What one row represents | Useful fields |
|---|---|---|
| `Asset_Inventory.csv` | One distinct discovered asset | `asset_id`, `asset_category`, `asset_type`, `primary_url`, `observation_status`, `evidence_ids_json` |
| `Occurrences.csv` | One significant placement or use of an asset | `occurrence_id`, `asset_id`, `placement_page_url`, `page_area`, `component`, `usage_context`, `evidence_ids_json` |
| `Sources.csv` | One information source used or declared during discovery | `source_id`, `source_type`, `requested_url`, `final_url`, `normalized_final_url`, `observation_status` |
| `Evidence.csv` | One specific observation from a source | `evidence_id`, `source_id`, `observed_at`, `evidence_type`, `discovery_method`, `observation_result`, `technical_attributes_json` |
| `Relationships.csv` | One typed connection supported by evidence | `relationship_id`, `source_object_type`, `source_object_id`, `relationship_type`, `target_object_type`, `target_object_id`, `target_url`, `confirmation_status`, `evidence_ids_json` |

To trace an asset, find its `asset_id` in Asset Inventory. Search Occurrences for the same `asset_id` to see recorded placements. Parse the asset's `evidence_ids_json` as a JSON array and find those IDs in Evidence. Follow each Evidence row's `source_id` to Sources. Search Relationships for connections where the asset appears as a source or target. A relationship can identify its target with `target_url` when there is no target object ID.

The model is a set of connected records, not one mandatory chain for every row. An asset can have no Occurrence. A Source can have several Evidence records. An Evidence record can support several assets or relationships. Do not add row counts across files: they count different kinds of records.

## 3. Understanding Asset Types

`asset_type` identifies what kind of asset a row represents. `asset_category` groups related types. These fields describe the asset, not the likely purpose of a page.

The initial approved taxonomy includes:

- **Content:** `WEB_PAGE`, `CONTENT_BLOCK`
- **Media:** `IMAGE`, `VIDEO`, `AUDIO`
- **Navigation:** `NAVIGATION_STRUCTURE`
- **Documents:** `DOCUMENT`
- **Functional components:** `FORM`, `SEARCH`, `FILTER`, `CALCULATOR`, `INTERACTIVE_MAP`

The taxonomy is not a promise that every type will appear in every run or that every possible website object is supported. Technical resources and external dependencies are usually represented as Sources and Relationships, not as owned website assets. `OTHER` means a known type outside the initial list. `UNKNOWN` means the available evidence does not support an asset type.

In Asset Inventory, filter `asset_type` to review each observed type. Do not interpret a missing type as proof that the site has no such assets.

## 4. Understanding Occurrences

An Asset is the distinct object in the register. An Occurrence records a meaningful place where that asset appears, using fields such as `placement_page_url`, `page_area`, and `component`.

One image used on three pages can be one Asset with several Occurrences. Several references to the same file do not automatically mean several distinct assets. Conversely, similar names or URLs do not prove that two assets are identical.

To review placement, filter Occurrences by `asset_id`, then read the page URL and component fields. Occurrences describe recorded placements; they do not prove that a component works or that every placement was discovered.

## 5. Understanding Evidence and Sources

A **Source** record identifies where the information came from. It contains the requested URL and, when available, the final and normalized URLs. An **Evidence** record describes the observation that supports a record. It contains the source, time, method, observation result, and available technical details.

`observation_status` describes the state of inspection. Common values in the current implementation include `OBSERVED`, `PARTIALLY_OBSERVED`, `INACCESSIBLE`, and `NOT_INSPECTED`. Read the status together with the record type and report notes.

`NOT_INSPECTED` means WAD did not inspect that source or resource. For example, a static page may declare an external embed or technical file that WAD records without fetching it. The declaration can be observed in the containing page while the declared resource remains uninspected. `NOT_INSPECTED` is not an availability result and does not, by itself, mean WAD failed.

Evidence supports specific claims. Follow `evidence_ids_json` from an Asset, Occurrence, or Relationship to Evidence, then follow `source_id` to Sources. A recorded URL without a successful response is not the same as an inspected resource. An HTTP response confirms that a response was received; it does not confirm the resource's business meaning, completeness, or quality.

## 6. Understanding Relationships

Relationships record typed links such as `LINKS_TO`, `USES`, `CONTAINS`, `EMBEDS`, `REDIRECTS_TO`, and `CANONICAL_OF`. Each row identifies a source object, the relationship type, a target object or URL, supporting evidence, and a `confirmation_status`.

To inspect a connection, identify the source and target fields, then follow `evidence_ids_json` to its supporting observation. A relationship marked `CONFIRMED` means that the relationship was recorded with supporting evidence under the output model. It does not prove that a linked function works or that the overall website inventory is complete.

The number of Relationship records counts recorded connections. A large count does not establish broad page coverage, correct classification, or complete discovery.

## 7. Understanding COMPLETE, PARTIAL and FAILED

Use the report status together with its counters, source outcomes, stop reason, and budget information.

- **COMPLETE:** Review the applicable stages and any individually unsuccessful or uninspected resources. A complete run can contain inaccessible resources. WAD does not claim that it found every asset.
- **PARTIAL:** Find the recorded reason, such as a robots restriction, request or byte limit, deadline, or technical blocker. Review which output files and observations were preserved. Do not treat PARTIAL as equivalent to a failed run when the package contains usable results.
- **FAILED:** Check whether a usable package exists and which files were written. Do not infer a successful survey from a directory or CSV headers alone.

An HTTP error is a result for one request. It is not automatically the run status. Likewise, the absence of budget exhaustion does not rule out PARTIAL: access restrictions or other mandatory work can remain incomplete.

## 8. Understanding Coverage and Limitations

Keep these quantities separate:

- **Discovered URLs** are unique candidates recorded from the allowed sources.
- **HTTP attempts** count requests actually made, including redirects and retries under the request-accounting rules.
- **Processed HTML pages** are pages whose content WAD received and processed as eligible HTML.
- **Assets** are distinct inventory objects.
- **Occurrences** are recorded uses or placements.
- **Sources** and **Evidence** describe provenance and observations.

These counts have different meanings and cannot be used as substitutes for one another. Asset totals do not measure completeness. A URL can be discovered but unrequested because of `robots.txt`, an external-host boundary, a budget, or another rule.

Page-purpose classification is separate from `asset_type`. The approved preliminary page types include `HOME`, `LISTING`, `DETAIL`, `INFORMATIONAL`, `FUNCTIONAL`, and `UNKNOWN`. WAD uses `UNKNOWN` when the available signals do not support a more specific page type. It is a conservative classification, not an HTTP failure. Extraction of other supported observations can still proceed.

In the current implementation, H1 headings are used as page-classification heading cues. Other heading levels can appear in the page Evidence but do not currently serve as classifier heading cues. A descriptive H2 heading can therefore coexist with `UNKNOWN`. The classifier also uses a limited set of English and Russian purpose words and structural signals. Treat UNKNOWN as a real uncertainty in the output, not as proof that a page lacks a purpose.

`page_type` and `page_type_confidence` are stored in the page details inside the linked Evidence row's `technical_attributes_json`; they are not columns in `Asset_Inventory.csv`. To review page classification, check the Discovery Report and the page's linked Evidence. `UNKNOWN` does not mean the page has no content. It means the run did not assign a more specific page purpose. Do not infer page purpose from a URL alone.

Static-only WAD inspects received HTML and other resources through its existing HTTP controls. It does not execute website JavaScript or test business workflows. Browser-dependent content, client-side state, form submission, search behavior, and other interactive outcomes remain unverified unless a separately approved and verified browser workflow is available.

## 9. Practical Result Review

1. Check the run status, stop reason, budgets, and report limitations.
2. Compare discovered URLs, HTTP attempts, and processed HTML pages as separate counts.
3. Filter Asset Inventory by `asset_category` and `asset_type`; open the asset records that matter to your work.
4. Search Occurrences by `asset_id` to find recorded placements.
5. Follow `evidence_ids_json` to Evidence and `source_id` to Sources. Check whether each source was inspected.
6. Review related Relationship records and their evidence before interpreting links.
7. Record robots restrictions, `NOT_INSPECTED` sources, `UNKNOWN` classifications, and browser-dependent areas.
8. Decide whether the confirmed observations are sufficient for the next planning task. If coverage matters, identify what remains unchecked before relying on the inventory.

## 10. Glossary

- **Asset:** A distinct website object with significance to the inventory.
- **Occurrence:** A recorded placement or use of an asset.
- **Source:** A URL or other information source from which WAD obtains or declares information.
- **Evidence:** A specific observation that supports an asset, occurrence, claim, or relationship.
- **Relationship:** A typed, evidence-supported connection between objects or a target URL.
- **Observation status:** The recorded state of inspection for a record.
- **Page type:** A tentative classification of a page's primary purpose, separate from its `WEB_PAGE` asset type.
- **Run status:** The overall result of the bounded survey: `COMPLETE`, `PARTIAL`, or `FAILED`.
