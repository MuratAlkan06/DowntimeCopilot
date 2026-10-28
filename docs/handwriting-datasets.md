# Handwriting datasets: licensing, provenance, privacy (#67)

The handwriting eval fixtures (`tools/hw_fixtures.py`) are the three synthetic form templates with values
composited from real people's handwritten characters. This page records where those characters come from,
under which terms, which datasets were considered and left alone, and why the generated images stay out of
git. Every quote below is verbatim from the linked page as retrieved on 2026-10-27 (whitespace collapsed).

## What we use: NIST Special Database 19, 2nd edition

- Landing page: <https://www.nist.gov/srd/nist-special-database-19>, DOI
  [10.18434/T4H01C](https://doi.org/10.18434/T4H01C).
- Files: `by_write.zip` and `by_class.zip` with their MD5 logs, from `https://s3.amazonaws.com/nist-srd/SD19/`,
  pinned in [`tools/handwriting_sources.lock.json`](../tools/handwriting_sources.lock.json). `hsf_page.zip`
  (the full-page forms) is never downloaded.
- Users' guide (2nd edition, Grother and Hanaoka, August 2016):
  <https://s3.amazonaws.com/nist-srd/SD19/sd19_users_guide_edition_2.pdf>.

What NIST says it is (landing page):

> Special Database 19 contains NIST's entire corpus of training materials for handprinted document and
> character recognition. It publishes Handprinted Sample Forms from 3600 writers, 810,000 character images
> isolated from their forms, ground truth classifications for those images, reference forms for further data
> collection, and software utilities for image management and handling.

> 2nd Edition – September 2016
>
> Download – by_class.zip – MD5 hash file
>
> Download – by_write.zip – MD5 hash file

How it is distributed: NIST's Special Databases catalog
(<https://www.nist.gov/srd/related-data-products-and-links/special-databases-and-special-software>) lists

> Special Database 19 NIST Handprinted Forms and Characters ... Free download

### Terms

NIST's licensing page, <https://www.nist.gov/open/license>, "Fair Use of Other NIST Data/Works":

> Data/works created by NIST employees that are not covered by the Standard Reference Data Act are subject to
> 17 U.S.C. §105 and generally are not subject to copyright protection within the United States. NIST data or
> other works may be subject to copyright protection in foreign countries.

> This data/work was created by employees of the National Institute of Standards and Technology (NIST), an
> agency of the Federal Government. Pursuant to title 17 United States Code Section 105, works of NIST
> employees are not subject to copyright protection in the United States. This data/work may be subject to
> foreign copyright.

> You may improve, modify, and create derivative works of the data or any portion of the data, and you may
> copy and distribute such modifications or works. Modified works should carry a notice stating that you
> changed the data and should note the date and nature of any such change. Please explicitly acknowledge the
> National Institute of Standards and Technology as the source of the data:

> Permission to use this data is contingent upon your acceptance of the terms of this agreement and upon your
> providing appropriate acknowledgments of NIST’s creation of the data/work.

The same page has a separate paragraph for Standard Reference Data:

> The Standard Reference Data Act, 15 U.S.C. § 290e, empowers the Secretary of Commerce to secure copyright on
> behalf of the United States in Standard Reference Data (SRD) prepared by NIST.

**Our reading.** SD19 is a Special Database that NIST lists as a free download. We found no SRD copyright
statement on its landing page or in its 2nd-edition users' guide, and the data.gov record for it (below)
names <https://www.nist.gov/open/license> as its license. We therefore follow the "Fair Use of Other NIST
Data/Works" terms: NIST is acknowledged as the source, and our derivative carries the date and nature of the
change. Whichever paragraph applies, we redistribute no SD19 bytes: the archives stay in the git-ignored
cache, the generated images stay git-ignored, and the repository holds only our own values, an anonymous
writer code and an image hash per fixture.

### The stale "available for a fee" metadata

The data.gov record, <https://catalog.data.gov/dataset/nist-handprinted-forms-and-characters-nist-special-database-19>,
still says:

> rights: Access to NIST Special Database 19 is available for a fee. For more information please visit
> https://www.nist.gov/srd/nist-special-database-19.

with `accessLevel` "restricted public", `modified` "2016-08-18 00:00:00" and `license`
"https://www.nist.gov/open/license".

The live reality on 2026-10-27: the landing page that this very record points to links the archives as direct
downloads; NIST's catalog lists SD19 as "Free download"; the archives answer an anonymous HTTPS request with no
registration, login or payment step. **Resolution:** we treat the fee line as stale metadata and follow the
landing page it defers to ("For more information please visit ..."). If NIST ever puts SD19 behind a fee or a
registration, the fetch stops and this page is revisited before anything else is generated.

### Acknowledgment (used wherever the fixtures or their results are shown)

> Handwriting samples: NIST Special Database 19, Handprinted Forms and Characters, 2nd edition, National
> Institute of Standards and Technology, https://doi.org/10.18434/T4H01C. NIST is the source of the data.
> Changed by DowntimeCopilot in 2026: individual character images were cropped, scaled, recolored and
> composited onto synthetic form templates, then distorted to resemble phone photos (`tools/hw_fixtures.py`).
> NIST does not endorse this project.

Every truth JSON says the same in short: `"source": "composited from public handwriting samples (NIST Special
Database 19)"`.

### Labeling language (approved)

- Say: **forms built from public handwriting samples**, or "composited from public handwriting samples (NIST
  Special Database 19)".
- Never: "handwritten forms", "real handwritten forms", "scanned patient forms", "photographed handwritten
  forms". Each mark is a real person's ink, but the arrangement, the values and the form are synthetic, and the
  "photo" is a simulated distortion.

### Privacy

The users' guide describes the writers:

> The partition of hsf_4 were completed by the Bethesda high school students, and all those of partitions
> hsf_{0,1,2,3,6,7,8} were obtained from Census Bureau employees in Suitland, Maryland.

The guide's example form is dated 8-3-89, and NIST removed names before release:

> Notice that the first field on this form, the name field, has been intentionally occluded, on some others it
> remains blank.

Writer ids are anonymous codes (`f0000_14`). We use only the segmented character images, never the full-page
forms (which also show date, city, state and ZIP). Still, one writer per fixture means each generated image
reproduces one real person's letterforms. That is part of why the images stay out of git
(`evals/fixtures/*.jpg` is ignored) and are regenerated from the seed instead, and why the committed truth
names only the anonymous writer code.

## Considered and not used

None of these was registered for, downloaded or fetched. The gate is quoted from each source.

| Dataset | Gate, verbatim | Why not |
|---|---|---|
| IAM Handwriting Database, <https://fki.tic.heia-fr.ch/databases/iam-handwriting-database> | "Before you can download the IAM Handwriting DB 3.0 we ask you to register so we are aware of who is using our data." "This database may be used for non-commercial research purpose only." | Registration plus non-commercial terms. (On 2026-10-27 the site's TLS certificate had also expired.) |
| CEDAR CDROM-1, <https://cedar.buffalo.edu/Databases/CDROM1/> | "To obtain a copy of CEDAR CDROM 1, send a check payable to "THE RESEARCH FOUNDATION OF SUNY" for the amount of $950 (U.S. funds)" and "The images from the Data set may not be re-distributed to a third-party, except for publishing examples in scientific publications or lectures." | Fee, mailed check. |
| Kaggle-hosted handwriting sets (Kaggle CLI docs, <https://github.com/Kaggle/kaggle-cli/blob/main/docs/README.md>) | "First, you will need a Kaggle account." | Account gate; licenses vary per upload, and many uploads are re-packagings of other datasets. |
| UNIPEN train_r01_v07, <https://zenodo.org/records/1195803> | "PERMISSION IS HEREBY GRANTED TO USE THE DATA FOR RESEARCH PURPOSES. IT IS NOT ALLOWED TO DISTRIBUTE THIS DATA FOR COMMERCIAL PURPOSES." | Research-only terms inside the data, contradicting the record's `cc-by-4.0` license field; on-line pen trajectories, not images; <https://www.unipen.org/> answered HTTP 522 on 2026-10-27. |
| GNHK (GoodNotes Handwriting Kollection), <https://www.goodnotes.com/gnhk> | None retrievable: the page redirected (HTTP 301) to the GoodNotes home page on 2026-10-27. | Terms unverifiable. |
| IMGUR5K, <https://github.com/facebookresearch/IMGUR5K-Handwriting-Dataset> | "IMGUR5K is Creative Commons Attribution-NonCommercial 4.0 International Public licensed, as found in the LICENSE file." "The dataset is provided using public links to each image, and the availability of these images is controlled by IMGUR or the original user (who uploaded it)." | Non-commercial; link rot (the images are third-party Imgur links; the repository is archived); unknown writers photographed in the wild. |

## EMNIST: a stop-and-redesign checkpoint, not a fallback

NIST's EMNIST page, <https://www.nist.gov/itl/products-and-services/emnist-dataset>:

> The EMNIST dataset is a set of handwritten character digits derived from the NIST Special Database 19 and
> converted to a 28x28 pixel image format and dataset structure that directly matches the MNIST dataset.

EMNIST is SD19 downsampled to 28x28 rasters, too coarse to composite legible writing at the generator's
200 DPI, and switching to it would change what the fixtures are. If the SD19 hash join (`by_class` x
`by_write`, checked against the real archives in S3) yields too few labeled glyphs per writer, the work stops
and a redesign goes to the owner. Nothing falls back to EMNIST automatically.

## Handling

- Only `python -m tools.hw_glyphs fetch` touches the network, and only when run by hand: never on import, in
  tests or in CI. It verifies each archive against NIST's per-file MD5 log, and `--write-lock` records the
  archive's sha256 and size in the lockfile at the first verified fetch. Until then the glyph source refuses to
  load.
- Archives, MD5 logs and the glyph index live in `.cache/handwriting/` (covered by the `.cache/` ignore rule).
- Tests use a stub glyph pack drawn at test time in SD19's raster format (128x128, white background, black
  ink). No dataset bytes are in the repository.
