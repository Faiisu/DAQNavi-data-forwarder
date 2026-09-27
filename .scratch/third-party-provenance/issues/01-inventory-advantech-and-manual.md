# 01: Inventory Advantech SDK and hardware manual provenance

Status: resolved
Blocked by: None

## Outcome

Create a reviewable inventory of bundled Advantech and other third-party material, its source, and its stated license or redistribution terms. Avoid asserting that the project owner holds copyright in this material.

## Work

1. Identify Advantech SDK/example/library/manual copies stored in the repository and distinguish them from project-authored notes and runtime package declarations.
2. Remove duplicate vendor materials from the repository.
3. Point maintainers to official Advantech pages for DAQNavi drivers, SDK libraries, and hardware manuals.
4. Update repository docs and setup guidance while retaining project-authored hardware notes that link to official sources.

## Deliverables and acceptance

- [x] Project documentation points to official Advantech DAQNavi driver/SDK and manual pages.
- [x] The SDK example bundle, duplicate polling example, example-only helper, and local manual PDF are removed.
- [x] Project-authored PCI-1716 engineering notes remain and refer to the official manual.
- [x] README and setup wizard point to the official DAQNavi support page.

## Comments

- 2026-09-27: At the user's direction, removed the 1,667-file `references/advantech_sdk/` bundle, including its bundled examples and helper components; removed the copied PCI-1716 manual PDF and the Advantech `PolliingStream.py` example plus its example-only `CommonUtils.py` helper.
- Kept `docs/hardware/PCI-1716.md` as project-authored paraphrased notes, with links to Advantech's official manual. Updated the README and provenance document to refer to official DAQNavi Linux driver/SDK, SDK product, and manual pages.
- Verified no remaining repository path references to the removed bundle/manual and no Advantech-copyrighted duplicate source files outside the deleted copies. Documentation path checks and `git diff --check` passed; no runtime tests were needed.
- Ticket resolved; vendor libraries and documentation should be obtained from Advantech's official support pages.
