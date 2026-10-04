# Healthcare evidence corpus — Ticket #11

The corpus contains **36 distinct documents**: 24 attributed public CDC/MMWR
guidelines and 12 clearly labelled synthetic documentation references. The
versioned authoring files and [manifest](../data/corpus/manifest.json) live under
`data/corpus/`. A clone can build and verify the complete corpus offline.

| Measured artifacts | Documents | Pages |
| --- | ---: | ---: |
| Public Markdown snapshots | 24 | 1,617 virtual pages |
| Synthetic Markdown references | 4 | 4 virtual pages |
| Synthetic text PDFs | 8 | 12 physical pages |
| **Total** | **36** | **1,633** |

The total exceeds both the 30-document and 150-page floor in BRD ASM-03.
Markdown virtual pages are a deterministic size measure, **not publisher page
numbers**. The original Markdown used to generate a PDF is its authoring source,
not an additional corpus document. Generated PDFs are not committed.

## Public evidence and synthetic coverage

Public evidence is the majority of both documents and text. It includes the
CDC/MMWR recommendations for HIV postexposure prophylaxis, doxycycline
postexposure prophylaxis, contraception, sexually transmitted infections,
hepatitis testing and prevention, tuberculosis treatment, opioid prescribing,
botulism, transplant screening and vaccination. The manifest gives every
article's title, DOI, publisher, publication date, source version and URL.

The public snapshots retain actual recommendations, contraindications and drug
interaction discussions. They also include clinical service and documentation
guidance. No clinical claims in these snapshots were authored or paraphrased
by a language model. They are mechanically rendered article text.

The synthetic component is authored for the fictional **Kestrel training
service**, with no patient cases, identifiers, medicines or invented treatment
recommendations. It covers documentation attribution, uncertainty, handoff,
medication-list reconciliation, allergy-status recording, consent records and
amendment history. Every source begins with `SYNTHETIC TRAINING DOCUMENT`;
every generated PDF page and artifact filename also identifies it as synthetic.

| Evidence role | Corpus content |
| --- | --- |
| Positive | Explicit recommendations in public sources; stated synthetic documentation requirements |
| Negative | Public contraindications/restrictions; synthetic prohibited documentation shortcuts |
| Conflicting | Two synthetic memos specify 7-day and 14-day review intervals for the same fictional documentation exercise, with no precedence rule |
| Insufficient | A synthetic display inventory and uncertainty reference deliberately omit treatment, dosage and interaction facts |

`topics`, `evidence_roles` and the shared conflict-pair `conflict_group` describe
the content. They do not contain questions, expected answers, retrieval labels
or evaluation scores. Ticket #12's evaluation set and Ticket #13's poisoned
document are not included.

Publication versions are preserved, including historical guidelines. Corpus
membership does not assert that an older recommendation remains current or
that a synthetic policy has clinical authority. Source title, date, version
and scope remain necessary when assessing retrieved evidence.

## Provenance and usage rights

Each public article was acquired through NCBI PMC's EFetch interface. Its JATS
XML contains the following explicit article-level permission statement:

> All material in the MMWR Series is in the public domain and may be used and
> reprinted without permission; citation as to source, however, is appreciated.

The [source tool](../scripts/corpus_sources.py) requires that exact notice before
rendering an article. This is an MMWR permission statement, not an assumption
that everything hosted by NLM/PMC may be redistributed. See also
[NLM copyright guidance](https://www.nlm.nih.gov/copyright.html) and
[NLM web policies](https://www.nlm.nih.gov/web_policies.html).

Attribution and the permission notice are retained in every public source.
Only text snapshots with this verified permission are committed; no public PDF
or third-party figure image is redistributed. Rendering retains headings,
paragraphs, lists, tables, captions and references. Table spans are expanded
into labelled row records so their column context survives the existing
CommonMark extractor. Superscripts, subscripts and reference markers remain
distinguishable. Figure images and image-only algorithms are omitted, with
explicit notices and a link to the original article. An image-only table causes
source rendering to fail rather than invent its contents.

Synthetic sources identify the Domain Copilot project as their author/owner
and use a repository path for provenance. Their dates are authorship dates.
They claim no external publisher, third-party license or public-source
endorsement. No real patient records or individually identifiable health
information were collected or authored for the corpus. Bibliographic author
names and institutional attribution in public guidelines are retained.

## Manifest and corpus identity

The manifest is JSON, with `schema_version: 1` and corpus version:

```text
healthcare-evidence-v1-bb14a5384031a161
```

Every document records:

| Field group | Meaning |
| --- | --- |
| `id`, `title`, `publisher`, `source_url` | Stable corpus ID, actual source title, owner and public URL or repository provenance |
| `publication_date`, `publication_version`, `retrieval_date` | ISO dates and the precise source version; synthetic documents use the project authorship date |
| `classification`, `data_scope` | Public/synthetic classification and the nature of the content |
| `license_name`, `license_url`, `license_notice`, `usage_notes` | Verified public permission and representation limitations; synthetic records explicitly claim no external license grant |
| `source_path`, `source_sha256` | Versioned authoring file and its normalized UTF-8 SHA-256 |
| `format`, `artifact_path`, `renderer`, `sha256` | PDF/Markdown ingestion artifact, renderer and exact artifact-byte SHA-256 |
| `byte_count`, `word_count`, `page_count` | Measurements of the actual artifact |
| `ingestion_version` | Document version passed to the existing Ticket #8 API |
| `topics`, `evidence_roles`, `conflict_group` | Content coverage and explicit association of the synthetic conflict pair |
| Public-only `doi`, `acquisition` | DOI, PMC accession, acquisition endpoint, raw response SHA-256 and source-renderer version |

Source checksums normalize CRLF and CR to LF before hashing UTF-8 text, so Git's
Windows line-ending conversion does not change the corpus. Byte-order marks
and binary NULs are rejected. Artifact checksums cover the exact generated
bytes. PDF output is deterministic under the repository's existing pinned
`pypdf` dependency, with no timestamps or random document identifiers.

The root `sha256` hashes UTF-8 canonical JSON: sorted object keys, compact
separators, Unicode preserved, excluding the root `sha256` and `corpus_version`
fields. `corpus_version` is `healthcare-evidence-v1-` plus the first 16 hex
characters of that hash. Any source, provenance, measurement, renderer or
coverage change therefore requires a new version. The full root digest is
also available in the manifest.

`corpus_version` identifies the collection. It is separate from document
`ingestion_version`, the embedding pipeline version and database migration
revisions. Later evaluation reports can record it without changing ingestion.

## How pages are counted

For PDFs, `pypdf.PdfReader` counts actual page objects, and every page must have
extractable text. Blank or image-only PDFs cannot satisfy the floor. The
synthetic renderer uses US Letter pages, Courier text, fixed wrapping and page
breaks; it adds a synthetic banner and page number to every page.

For Markdown, decode the actual artifact as UTF-8 and count Unicode word
matches using Python's regular expression:

```text
\b\w+(?:['’\-]\w+)*\b
```

Its virtual page count is `max(1, ceil(word_count / 500))`, including headings,
references, table text and attribution. Punctuation alone is not a word.
No browser layout, printing preference or publisher pagination is assumed.

Ticket #8 keeps Markdown citation `page` as `null` and retains section
headings. These virtual pages are only corpus inventory measurements; they
are never manufactured as citation page numbers. PDF citations retain actual
one-based extracted page numbers.

## Offline build and verification

From a checkout with the existing project dependencies installed:

```bash
python scripts/corpus.py build
python scripts/corpus.py verify
```

Both commands print the corpus version and measured totals. The default output
directory is the ignored `.tasks/corpus-build/`; choose another local directory
with `--output-directory`. Build output cannot overwrite versioned corpus
sources.

Verification reads the actual files. It checks source and artifact inventories,
checksums, required provenance, unique IDs and content, real measurements,
synthetic labels, retained public permission notices, both supported formats
and both size floors. Missing, extra, edited or mismatched files fail with a
nonzero exit status. Metadata totals alone are insufficient.

Normal build and verification make no network calls. Optional reacquisition
can independently check the pinned public source responses:

```bash
python scripts/corpus_sources.py --output-directory .tasks/public-source-recheck
```

This uses only the manifest's validated PMC accessions and the fixed NCBI
endpoint. It respects the unauthenticated NCBI request rate and checks both
the acquisition-response hash and rendered-source hash. A publisher/PMC
update, metadata change or unavailable endpoint fails the replay; it does not
silently replace a pinned file. The committed snapshots still reproduce the
corpus offline. Review upstream changes, provenance, permissions and measured
artifacts together before publishing a new manifest version.

## Ingest through Ticket #8

Start the existing stack and migrate using [INGESTION.md](./INGESTION.md).
The corpus command runs **from the checkout on the host** against that API;
the corpus is not baked into the application image. Dockerfile, Compose,
the original two-document smoke seed and ingestion behavior are unchanged.

Provide an admin `INGEST_ACCESS_TOKEN` in the environment, or set
`AUTH__DEMO_PASSWORD` to the same development demo password as the API. The
host CLI does not load `.env` automatically. Then run:

```bash
python scripts/corpus.py ingest --api-url http://127.0.0.1:8000
```

Alternatively, use the existing Compose seed image without installing Python
on the host. From a Bash shell in the checkout, after starting the stack:

```bash
docker compose run --rm --build \
  -v "${PWD}/scripts:/app/scripts:ro" \
  -v "${PWD}/data/corpus:/corpus:ro" \
  seed python scripts/corpus.py ingest \
  --manifest /corpus/manifest.json --output-directory /tmp/corpus-build \
  --api-url http://api:8000
```

The mounts are read-only; generated files live in the temporary container.
Compose supplies its configured demo password. With demo accounts disabled,
forward an existing admin token by adding `-e INGEST_ACCESS_TOKEN` before
`seed`. This uses the same uploader and requires no Compose or image changes.

This builds and verifies the artifacts, then calls the existing
`scripts/ingest_documents.py` with the manifest's document versions and
`--wait`. All uploads go through the existing admin-only HTTP API and
`document.ingest` Celery handler. No direct database insertion, alternative
extractor, queue or embedding implementation is used. The first run needs the
existing local MiniLM weights; CPU indexing of the public guidelines takes
longer than the two-document smoke seed. `--timeout` defaults to 1,800 seconds
per document; a timeout leaves a queryable job, not a cancellation.

The CLI prints each filename, document/job IDs, reuse flag, final status and
chunk count, without credentials or source text. Keep that output with the
corpus version when recording a run. To map a database document back to its
manifest entry, use the artifact filename, document version and exact artifact
SHA-256 (`content_hash` in Ticket #8).

Run the same command again with the same ingestion configuration. Ticket #8
should return `reused: true` with the original document/job IDs and unchanged
chunks/embeddings. An intentional content, document-version or ingestion-profile
change has the existing Ticket #8 semantics described in its documentation.

## Tests

```bash
python -m pytest tests/unit/corpus tests/integration/test_corpus_ingestion.py
```

The tests rebuild actual committed files, reject changed/unlisted sources and
artifacts, exercise provenance and page-count boundaries, check table rendering
and deterministic PDFs, and pass all 36 artifacts through Ticket #8's real
extractor with its default upload/page/text limits. No external network or
model download is needed for these tests.

For end-to-end verification, use the ingestion command above, poll all jobs to
completion, check chunks and embeddings in PostgreSQL, exercise the existing
Ticket #9 retrieval store, and repeat the command to verify idempotency.

## Recorded verification — 2026-10-04

The version above built byte-identical artifacts on Windows and in Linux Docker.
The current Ticket #8 API and real Celery worker ran from the worktree against
fresh Docker PostgreSQL/pgvector and Redis services, using the default 512/64
chunk settings and local `all-MiniLM-L6-v2` embeddings (384 dimensions, version 1).

- All 36 documents/jobs completed all five stages, producing **4,386 chunks and
  4,386 embeddings**. PDF and Markdown uploads returned HTTP 202.
- Ticket #9 dense and PostgreSQL FTS searches returned stored chunks; document,
  version, section, page and chunk metadata resolved against the database.
- Re-ingestion from the Linux Docker CLI returned `reused: true` for all 36
  documents. Document, job, chunk and embedding ID sets were unchanged.
- The full suite passed: **988 passed, 2 existing skips** (the opt-in BGE model
  smoke test and a baseline permission case). Ruff lint/format, mypy,
  import-linter and Git whitespace checks passed. The corpus secret scan found
  no leaks.
- Alembic reported one head, `95c7e8a12d40`; two upgrades were successful and
  the post-ingestion schema check found no new upgrade operations.
