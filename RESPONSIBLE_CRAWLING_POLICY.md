# WAD Policy for Responsible Handling of Third-Party Website Data

**Version:** 1.1
**Status:** APPROVED BY THE WAD OWNER
**Approval date:** 2026-11-07
**Applies to:** the WAD project, its developers, and its operators

## 1. Our position

WAD helps an operator create an inventory of supported assets on one authorized website and record the provenance of its results. When handling data from third-party websites, the project follows the principle of minimum necessary access and processing.

The fact that material is publicly available online does not, by itself, grant permission to collect, retain, reuse, or publish it. A link to an asset also does not mean that WAD retrieved or inspected the asset itself.

The WAD owner has approved this policy. The project adopts it as a binding rule for development, release preparation, and use of WAD. Project participants must follow it, and WAD users must follow it when conducting inspections.

This commitment does not mean that every safeguard is built into or automatically checked by the software. The operator must provide any safeguard that WAD does not enforce. If that cannot be done, the affected inspection must not proceed.

## 2. Scope and separate requirements

This policy applies to WAD development, documentation, test materials, public-release preparation, and website inspections.

Operators must consider three separate sets of requirements:

- **Target-site requirements:** applicable laws, contracts, website terms, owner instructions, and permissions govern the target website.
- **Platform requirements:** GitHub rules apply when using GitHub Services. They are not a general prohibition on developing crawlers for unrelated websites.
- **WAD safeguards:** this policy sets the approach required of the project and its operators. It must not be presented as GitHub’s own wording or as proof of compliance with GitHub’s rules.

Meeting one set of requirements does not replace the others. The operator is responsible for checking the requirements that apply to the specific website and activity.

## 3. Authorization and target selection

Before an inspection, the operator must have a documented basis for the intended access and use of the results. Under this policy, acceptable bases are:

1. The operator owns or controls the website.
2. The customer owns or controls the website and authorized the inspection.
3. The website owner gave written permission that covers the stated purpose and scope.

Public accessibility alone is not sufficient permission. Before starting, record the target website, authorized hosts and sections, purpose, basis for access, and known restrictions. Review the website terms and the owner’s or customer’s instructions. Do not start if permission or scope is unclear.

Select one target website. Do not use WAD for indiscriminate inspections of multiple websites.

## 4. Data minimization and use

The project and operators collect and retain only information needed for the agreed inventory. WAD results describe discovered assets, where they appear, their sources, and supporting observations.

Do not use WAD for:

- mass extraction of website content;
- collecting personal data, building profiles, or compiling contact databases;
- creating lists for unsolicited advertising, spam, resale, or profiling;
- reusing data for purposes that the website owner or customer did not approve.

WAD records information about assets and their sources; it does not grant rights to use website content. Discovering a resource does not mean that the resource was downloaded, inspected, or verified. Observing an asset does not grant rights to copy, distribute, or publish its content.

If personal information appears incidentally, do not turn it into separate records or use it for other purposes. Avoid retaining it where possible, minimize its presence in reports and logs, and remove unnecessary details before sharing results.

Store working results locally or in storage controlled by the data owner or customer. Do not place customer reports, raw third-party website content, downloaded documents or media, private URLs, credentials, personal data, or confidential evidence in the public WAD repository.

Before sharing or publishing results, the operator must review copyright, database rights, privacy, confidentiality, contractual restrictions, and other third-party rights. Include demonstration materials in a public release only when publication rights are confirmed; synthetic data is preferred.

## 5. Website instructions and inspection boundaries

WAD must identify itself honestly with a stable, descriptive User-Agent. Do not impersonate or rotate identities to avoid restrictions.

Honor applicable `robots.txt` rules that can be parsed correctly under [RFC 9309](https://www.rfc-editor.org/rfc/rfc9309). If access to the rules is denied or their meaning cannot be determined safely, stop the affected part of the inspection. If `robots.txt` is unreachable because of a network failure, follow RFC 9309’s disallow behavior unless the standard permits use of a valid cached copy.

`robots.txt` governs crawler behavior; it does not grant permission to access a website. Its absence or permissive rules do not replace owner permission, legal requirements, or website terms. A sitemap is a discovery hint, not permission to inspect every listed URL.

Stay within the authorized host scope and WAD’s redirect limits. Do not start a separate inspection of an external website. Record an external resource only as an uninspected dependency when WAD’s data model supports it.

## 6. Request limits and no access-control bypass

Use WAD’s existing request, time, byte, page, depth, redirect, retry, and process limits. Do not increase limits to overcome a website’s refusal or instability. Do not use uncontrolled concurrency, distributed crawling, proxy or IP rotation, identity rotation, or unbounded retries.

Do not submit forms, trigger website actions, or use state-changing HTTP methods. Do not execute page JavaScript to perform actions or bypass restrictions.

Do not bypass authentication, paywalls, CAPTCHAs, web application firewalls, bot checks, IP blocks, or other access controls. Do not inspect hidden or restricted areas outside the authorized scope. An access denial is a reason to stop, not a challenge to work around.

Stop the inspection if permission is missing, withdrawn, or unclear; website terms prohibit the inspection; the website denies or rate-limits requests; a redirect leaves the authorized scope; a security challenge appears; a budget is exhausted; the owner asks to stop; or a privacy or confidentiality concern arises. Preserve confirmed results and record the stop reason where WAD supports it. Do not continue through another account, proxy, or environment.

## 7. WAD’s use of GitHub

The WAD repository may contain source code, documentation, tests, synthetic test fixtures, and reviewed release materials. Do not use repositories, Issues, Pull Requests, Pages, Packages, Actions artifacts, or other GitHub Services as a crawler runtime, job queue, proxy, relay, or store for inspection results.

As an additional WAD rule, inspections of production and customer websites must run outside GitHub Actions runners, whether GitHub-hosted or self-hosted. GitHub Actions may be used for WAD development and checks against local synthetic data when permitted by current GitHub terms.

GitHub Services and content hosted on GitHub are out of scope for WAD’s generic HTML crawler. Do not collect information about GitHub users for unsolicited contact, profiling, resale, or spam.

Any future GitHub integration requires a separate review. It must use an access method permitted by applicable terms and comply with relevant API terms and rate limits. Before a release or integration, review the current [GitHub Acceptable Use Policies](https://docs.github.com/en/site-policy/acceptable-use-policies/github-acceptable-use-policies), [GitHub Terms of Service](https://docs.github.com/en/site-policy/github-terms/github-terms-of-service), and [GitHub Terms for Additional Products and Features](https://docs.github.com/en/site-policy/github-terms/github-terms-for-additional-products-and-features).

## 8. Complaints and result corrections

Operators must provide a way to receive and review complaints and requests to correct or remove inspection results.

If a website owner or customer asks, stop the affected activity and review the request. If a credible concern arises about privacy, confidentiality, content rights, or access, do not continue the inspection until the concern is resolved.

## 9. WAD limitations and truthful reporting

This policy is a binding conduct standard, not a technical guarantee. WAD’s static inspection may not discover material available only through JavaScript, browser actions, authentication, or other unsupported mechanisms. A `COMPLETE` status does not prove that the inventory is exhaustive.

The project must document which safeguards the software enforces, which the operator must perform, and which are unsupported. Do not describe a safeguard as implemented or verified without evidence. If the software does not provide an important safeguard, the operator must provide it manually. If that is not possible, do not use the affected capability.

This policy is not legal advice, GitHub approval or endorsement, proof of compliance with GitHub requirements, or a guarantee against account or repository restrictions. The project and its operators commit to following these rules while remaining responsible for applicable laws, contracts, target-site terms, permissions, and third-party rights.

## 10. Policy review

Review this policy before the first public release, before enabling browser-based inspections or new execution environments, before adding scheduled or multi-site inspections, and after a substantiated complaint or material change to applicable requirements.

Changes to mandatory safeguards require approval from the WAD owner.

## References

- [GitHub Acceptable Use Policies](https://docs.github.com/en/site-policy/acceptable-use-policies/github-acceptable-use-policies)
- [GitHub Terms of Service](https://docs.github.com/en/site-policy/github-terms/github-terms-of-service)
- [GitHub Terms for Additional Products and Features](https://docs.github.com/en/site-policy/github-terms/github-terms-for-additional-products-and-features)
- [GitHub REST API rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)
- [IETF RFC 9309 — Robots Exclusion Protocol](https://www.rfc-editor.org/rfc/rfc9309)

**Note:** Recheck the links and requirements when relevant rules change materially.
