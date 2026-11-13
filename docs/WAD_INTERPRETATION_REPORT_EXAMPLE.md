# WAD Interpretation Report — Illustrative Example

> **Synthetic example only:** The domain, identifiers, observations, and recommendations below are invented for illustration. This is not the output of an actual WAD run. The reserved `example.invalid` domain is not a real inspected website.

## 1. Executive summary

This example describes a bounded inspection of one HTML page on `https://example.invalid/`. The illustrative run status is `COMPLETE`: the intended static inspection finished, but that status does not establish that every website asset was found.

WAD recorded the page and observed a link in its footer to a PDF hosted on another domain. The link was found in the inspected page. WAD recorded the external PDF source as `NOT_INSPECTED`; the file's availability and contents were not verified.

The brochure link may be relevant to the site's visitor information. The available data does not show whether visitors use the brochure, whether the file is current, or whether the owner wants to keep it during modernization.

## 2. Evidence and records

| Record | Illustrative observation | What it supports |
|---|---|---|
| Asset `AST-000001` | `WEB_PAGE` at `https://example.invalid/` | The page was recorded as an asset. |
| Occurrence `OCC-000001` | Link in the page footer; `component` is `a`; usage context is “Brochure” | The brochure link was observed at this placement. |
| Evidence `EVD-000001` | Link markup observed in the page's HTML | Supports the recorded link, not the brochure's contents. |
| Source `SRC-000002` | External PDF URL; `observation_status` is `NOT_INSPECTED` | WAD did not fetch or inspect the external PDF. |
| Relationship `REL-000001` | `LINKS_TO` from the page to the external PDF URL | The inspected page contains a link to that URL. |

These identifiers and records are fictional. In a real report, use the IDs and fields present in the run's CSV files. Follow each record's Evidence and Source references before relying on a conclusion.

## 3. Modernization consideration

**VERIFY:** Ask the owner whether the brochure is current and should remain available. Confirm publication rights and identify a destination URL only after the new site's structure is defined. If the old URL changes, the migration team can then assess whether a redirect is needed.

WAD did not establish that the brochure is popular, indexed by a search engine, or valuable to the business. It also did not inspect the external file.

## 4. Five points for the owner

1. `Asset` identifies a distinct website object; an `Occurrence` records a place where it appears.
2. Evidence confirms the observation made in the inspected HTML; it does not prove that the linked file works or is useful.
3. `NOT_INSPECTED` means WAD did not inspect the source. It is not an availability result.
4. `COMPLETE` describes completion of the bounded run, not exhaustive coverage of the website.
5. Treat recommendations as questions for the owner and engineering team. WAD does not decide which assets to migrate.