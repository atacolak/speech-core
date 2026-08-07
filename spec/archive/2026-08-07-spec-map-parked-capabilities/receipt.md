schema: ata.spec-convergence/v1
change_id: spec-map-parked-capabilities
intent_digest: sha256:4579621adeb675b938db58849f023e60f1b08257f9d2772e82f75f85e92d6db9
intent_revision: 2
implementation_commit: 84acb81dbf4892db6f67b0c48ef7d843ca8acc5b
verdict: converged
recorded_at: 2026-08-07T10:30:00Z
recorded_by: speech-core/steward
evidence:
  summary: >
    Implement commit 84acb81 is README-only and matches every Acceptance bullet
    in the frozen proposal (parked/direction TOC, speech-out row, direction-only
    evolution row, honesty paragraph, placement between accepted and archive
    TOCs). Independent review sc-po4 PASS in a distinct session. Flow pointer
    in spec/flows/speech-in.md now resolves. No chapter/ADR/runtime/charter
    edits. Charter sweep clean. Dual digest-procedure mismatch (city freeze vs
    local G6) noted as tooling finding; bound intent_digest remains the city
    freeze used by mint/sc-9c9; proposal/tasks bytes match freeze-time writes.
  compared:
    intent_ref: spec/archive/2026-08-07-spec-map-parked-capabilities/proposal.md
    implementation_ref: 84acb81dbf4892db6f67b0c48ef7d843ca8acc5b
  checks:
    - "git show 84acb81 --stat  # only spec/README.md +11"
    - "git diff f4a0ea4..84acb81 --name-only  # spec/README.md"
    - "git rev-parse 84acb81^  # == f4a0ea4"
    - "content match proposal Acceptance rows vs README parked table"
    - "rg speech-out|parked|ADR-001 spec/flows/speech-in.md"
    - "gc bd show sc-po4  # PASS distinct verifier"
    - "gc bd show sc-9c9  # implementation_status closed-steward-pending"
    - "session transcript freeze digest 4579621a… reconfirmed"
  artifacts:
    - spec/archive/2026-08-07-spec-map-parked-capabilities/outcome.md
    - spec/archive/2026-08-07-spec-map-parked-capabilities/execution-request.json
    - spec/README.md
  operator_word: "alr lgtm!!"
notes: >
  Control sc-scv-38d9a38358582b220483. Product root sc-9c9 left OPEN for
  manager close after archive evidence. Steward did not implement and does
  not close the product root.
