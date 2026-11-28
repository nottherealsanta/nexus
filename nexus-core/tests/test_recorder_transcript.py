"""Revisioned transcript primitives and conservative joins (plan 54, C2)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from nexus.recorder.transcript import (
    MAX_ATTEMPTS,
    JoinPolicy,
    Transcript,
    TranscriptCheckpoint,
    TranscriptError,
    TranscriptSegment,
    join_boundary,
)

RATE = 16_000


def result(
    segment_id: int,
    revision: int,
    *,
    start: int,
    end: int,
    text: str,
    status: str = "final",
    attempt: int = 1,
    generation: int = 0,
    window_start: int | None = None,
    retranscription_token: str | None = None,
) -> TranscriptSegment:
    return TranscriptSegment(
        segment_id=segment_id,
        revision=revision,
        start=start,
        end=end,
        status=status,
        raw_text=text,
        attempt=attempt,
        generation=generation,
        window_start=window_start,
        retranscription_token=retranscription_token,
    )


def test_revision_replaces_provisional_and_rejects_stale_or_conflicting_results() -> None:
    transcript = Transcript()
    first = transcript.apply(result(0, 1, start=0, end=800, text="hello wor", status="provisional"))
    assert first.applied
    assert transcript.segment(0).text == "hello wor"

    final = transcript.apply(result(0, 2, start=0, end=800, text="hello world"))
    assert final.applied
    assert transcript.segment(0).status == "final"
    assert transcript.text() == "hello world"

    stale = transcript.apply(result(0, 1, start=0, end=800, text="hello wor", status="provisional"))
    assert not stale.accepted and stale.reason == "stale_revision"
    assert transcript.text() == "hello world"

    duplicate = transcript.apply(result(0, 2, start=0, end=800, text="hello world"))
    assert duplicate.accepted and duplicate.reason == "duplicate"
    assert transcript.text() == "hello world"

    conflict = transcript.apply(result(0, 2, start=0, end=800, text="different"))
    assert not conflict.accepted and conflict.reason == "stale_revision"
    assert transcript.text() == "hello world"


def test_wrong_generation_is_ignored() -> None:
    transcript = Transcript(generation=2)
    outcome = transcript.apply(result(0, 1, start=0, end=100, text="old", generation=1))
    assert not outcome.accepted and outcome.reason == "stale_generation"
    assert transcript.current_segments == ()


def test_out_of_order_segment_gets_joined_when_predecessor_arrives() -> None:
    transcript = Transcript()
    later = transcript.apply(
        result(1, 1, start=RATE, end=2 * RATE, text="on the mat and then", window_start=RATE - RATE // 4)
    )
    assert later.applied
    assert transcript.segment(1).text == "on the mat and then"

    earlier = transcript.apply(result(0, 1, start=0, end=RATE, text="the cat sat on the mat"))
    assert earlier.applied
    assert earlier.changed == (0, 1)
    assert transcript.segment(1).text == "on the mat and then"
    assert transcript.segment(1).uncertain_join
    assert transcript.segment(1).join == "ambiguous_overlap"
    assert transcript.text() == "the cat sat on the mat on the mat and then"


def test_ambiguous_repeated_overlap_is_preserved_and_marked() -> None:
    transcript = Transcript()
    transcript.apply(result(0, 1, start=0, end=RATE, text="hello hello"))
    outcome = transcript.apply(
        result(1, 1, start=RATE, end=2 * RATE, text="hello hello world", window_start=RATE - RATE // 8)
    )

    assert outcome.applied
    segment = transcript.segment(1)
    assert segment.uncertain_join
    assert segment.join == "ambiguous_repetition"
    assert segment.text == "hello hello world"
    assert transcript.text() == "hello hello hello hello world"


def test_repeated_words_without_audio_overlap_are_never_removed() -> None:
    transcript = Transcript()
    transcript.apply(result(0, 1, start=0, end=100, text="yes"))
    transcript.apply(result(1, 1, start=200, end=300, text="yes yes", window_start=200))

    segment = transcript.segment(1)
    assert not segment.uncertain_join
    assert segment.join == "no_overlap"
    assert transcript.text() == "yes yes yes"


def test_single_token_nonperiodic_and_full_overlap_are_all_preserved() -> None:
    single = join_boundary("go now", "now go", overlap=True)
    assert single.uncertain and single.overlap_tokens == 1 and single.text == "now go"

    full = join_boundary("the cat sat", "sat", overlap=True)
    assert full.uncertain and full.text == "sat"

    phrase = join_boundary("the cat sat on the mat", "on the mat and then", overlap=True)
    assert phrase.uncertain and phrase.overlap_tokens == 3 and phrase.text == "on the mat and then"


def test_text_only_nonperiodic_repetition_is_not_deleted() -> None:
    # The same phrase may genuinely have been said twice; text alone cannot prove it is the overlap copy.
    joined = join_boundary("I said go home", "I said go home again", overlap=True)
    assert joined.text == "I said go home again"
    assert joined.overlap_tokens == 4
    assert joined.uncertain and joined.reason == "ambiguous_overlap"

    transcript = Transcript()
    transcript.apply(result(0, 1, start=0, end=RATE, text="I said go home"))
    transcript.apply(
        result(1, 1, start=RATE, end=2 * RATE, text="I said go home again", window_start=RATE - 400)
    )
    assert transcript.text() == "I said go home I said go home again"
    assert transcript.segment(1).uncertain_join


def test_join_is_exact_not_fuzzy() -> None:
    assert join_boundary("cat", "catalog", overlap=True).text == "catalog"
    assert join_boundary("the cat", "a cat", overlap=True).text == "a cat"
    policy = JoinPolicy(max_overlap_tokens=2)
    capped = join_boundary("one two three four", "three four five", overlap=True, policy=policy)
    assert capped.text == "three four five"
    assert capped.overlap_tokens == 2 and capped.uncertain


def test_skipped_and_empty_segments_do_not_add_text() -> None:
    transcript = Transcript()
    transcript.apply(result(0, 1, start=0, end=100, text="hello"))
    transcript.apply(result(1, 1, start=100, end=110, text="ignored", status="skipped"))
    transcript.apply(result(2, 1, start=110, end=200, text="", status="error", attempt=1))
    transcript.apply(result(3, 1, start=200, end=300, text="world"))

    assert transcript.segment(1).text == ""
    assert transcript.text() == "hello world"
    assert [segment.segment_id for segment in transcript.pending_finalizations()] == [2]


def test_retry_limit_and_pending_finalizations() -> None:
    transcript = Transcript()
    transcript.apply(result(0, 1, start=0, end=100, text="draft", status="provisional"))
    transcript.apply(result(1, 1, start=100, end=200, text="", status="error", attempt=MAX_ATTEMPTS))
    transcript.apply(result(2, 1, start=200, end=300, text="done"))

    assert [segment.segment_id for segment in transcript.pending_finalizations()] == [0, 1]
    assert not Transcript.retryable(transcript.segment(1))


def test_documents_round_trip_and_reconstruct_joins() -> None:
    transcript = Transcript(generation=3)
    transcript.apply(result(1, 1, start=RATE, end=2 * RATE, text="on the mat and then", generation=3, window_start=RATE - 400))
    transcript.apply(result(0, 1, start=0, end=RATE, text="the cat sat on the mat", generation=3))

    rebuilt = Transcript.from_documents(transcript.history_documents())
    assert rebuilt.generation == 3
    assert rebuilt.text() == transcript.text() == "the cat sat on the mat on the mat and then"
    assert rebuilt.segment(1).uncertain_join
    assert rebuilt.documents() == transcript.documents()


def test_final_is_immutable_and_boundaries_are_monotonic() -> None:
    transcript = Transcript()
    assert transcript.apply(result(0, 1, start=0, end=800, text="draft", status="provisional")).applied
    assert transcript.apply(result(0, 2, start=0, end=900, text="draft more", status="provisional")).applied

    short = transcript.apply(result(0, 3, start=0, end=850, text="draft more", status="provisional"))
    assert not short.accepted and short.reason == "boundary_regression"
    moved = transcript.apply(result(0, 4, start=10, end=900, text="draft more", status="provisional"))
    assert not moved.accepted and moved.reason == "boundary_regression"
    assert transcript.segment(0).end == 900

    assert transcript.apply(result(0, 5, start=0, end=900, text="final text")).applied
    demote = transcript.apply(result(0, 6, start=0, end=900, text="provisional again", status="provisional"))
    assert not demote.accepted and demote.reason == "final_immutable"
    replace_final = transcript.apply(result(0, 7, start=0, end=900, text="different final"))
    assert not replace_final.accepted and replace_final.reason == "final_immutable"
    shorten_final = transcript.apply(result(0, 8, start=0, end=800, text="short final"))
    assert not shorten_final.accepted and shorten_final.reason == "boundary_regression"
    shorten_and_demote = transcript.apply(
        result(0, 9, start=0, end=800, text="provisional again", status="provisional")
    )
    assert not shorten_and_demote.accepted and shorten_and_demote.reason == "boundary_regression"
    assert transcript.text() == "final text"

    token = transcript.begin_retranscription(0)
    retranscribed = transcript.apply(
        result(0, 10, start=0, end=700, text="new pass", status="provisional", retranscription_token=token)
    )
    assert retranscribed.applied
    assert transcript.segment(0).status == "provisional"
    assert transcript.segment(0).end == 700
    assert transcript.text() == "new pass"


def test_live_revision_history_is_bounded_with_explicit_evictions() -> None:
    persisted = []
    transcript = Transcript(live_revisions_per_segment=4, persist_revisions=persisted.extend)
    evicted = []
    for revision in range(1, 101):
        outcome = transcript.apply(
            result(0, revision, start=0, end=revision * 160, text=f"preview {revision}", status="provisional")
        )
        assert outcome.applied
        evicted.extend(outcome.evicted)

    live = transcript.history_documents()
    assert [document["revision"] for document in live] == [97, 98, 99, 100]
    assert [segment.revision for segment in evicted] == list(range(1, 97))
    assert [segment.revision for segment in persisted] == list(range(1, 97))
    assert transcript.segment(0).revision == 100
    assert transcript.segment(0).text == "preview 100"
    assert transcript.next_revision(0) == 101
    assert transcript.next_revision(0) == 102


def test_next_revision_reservations_are_distinct_atomic_and_out_of_order_safe() -> None:
    transcript = Transcript()
    with ThreadPoolExecutor(max_workers=8) as pool:
        reserved = list(pool.map(lambda _: transcript.next_revision(0), range(100)))
    assert sorted(reserved) == list(range(1, 101))
    assert transcript.next_revision(0) == 101
    assert transcript.next_revision(1) == 1

    assert transcript.apply(result(0, 101, start=0, end=1010, text="latest", status="provisional")).applied
    late = transcript.apply(result(0, 100, start=0, end=1000, text="late", status="provisional"))
    assert not late.accepted and late.reason == "stale_revision"
    assert transcript.segment(0).revision == 101


def test_active_segments_are_hard_bounded_and_paging_is_explicit() -> None:
    persisted = []
    transcript = Transcript(max_active_segments=2, live_revisions_per_segment=4, persist_revisions=persisted.extend)
    assert transcript.apply(result(0, 1, start=0, end=100, text="zero", status="provisional")).applied
    assert transcript.apply(result(1, 1, start=100, end=200, text="one")).applied

    full = transcript.apply(result(2, 1, start=200, end=300, text="two"))
    assert not full.accepted and full.reason == "active_segments_full"
    assert transcript.segment(2) is None
    with pytest.raises(TranscriptError) as reserve_failed:
        transcript.next_revision(2)
    assert reserve_failed.value.code == "active_segments_full"

    assert transcript.apply(result(0, 2, start=0, end=100, text="zero final")).applied
    page = transcript.page_out_through(0)
    assert page.through_segment_id == 0
    assert [segment.segment_id for segment in page.segments] == [0]
    assert page.segments[0].text == "zero final"
    assert [document["revision"] for document in page.revision_documents()] == [1, 2]
    assert [segment.revision for segment in persisted] == [1, 2]
    assert transcript.paged_through == 0
    assert transcript.active_segment_ids == (1,)

    assert transcript.apply(result(2, 1, start=200, end=300, text="two")).applied
    assert transcript.active_segment_ids == (1, 2)
    assert transcript.apply(result(0, 3, start=0, end=100, text="resurrect")).reason == "segment_paged_out"
    with pytest.raises(TranscriptError) as caught:
        transcript.next_revision(0)
    assert caught.value.code == "segment_paged_out"


def test_paging_refuses_nonterminal_and_preserves_join_context() -> None:
    persisted = []
    transcript = Transcript(max_active_segments=2, persist_revisions=persisted.extend)
    assert transcript.apply(result(0, 1, start=0, end=RATE, text="I said go home")).applied
    assert transcript.apply(
        result(1, 1, start=RATE, end=2 * RATE, text="I said go home again", window_start=RATE - 400)
    ).applied
    assert transcript.segment(1).uncertain_join

    page = transcript.page_out_through(0)
    assert [segment.segment_id for segment in page.segments] == [0]
    assert [segment.segment_id for segment in persisted] == [0]
    assert transcript.active_segment_ids == (1,)
    assert transcript.segment(1).uncertain_join  # last paged text retained only as join context

    assert transcript.apply(result(2, 1, start=2 * RATE, end=3 * RATE, text="two", status="provisional")).applied
    with pytest.raises(TranscriptError) as caught:
        transcript.page_out_through(2)
    assert caught.value.code == "segment_not_terminal"
    assert transcript.active_segment_ids == (1, 2)


def test_retranscription_tokens_are_scoped_and_stale_callbacks_are_rejected() -> None:
    transcript = Transcript()
    assert transcript.apply(result(0, 1, start=0, end=1000, text="final")).applied

    token1 = transcript.begin_retranscription(0)
    in_progress = transcript.apply(result(0, 2, start=0, end=1000, text="ordinary final"))
    assert in_progress.reason == "retranscription_in_progress"
    bogus = transcript.apply(
        result(0, 2, start=0, end=900, text="bogus", status="provisional", retranscription_token="not-a-token")
    )
    assert bogus.reason == "retranscription_not_active"

    provisional = transcript.apply(
        result(0, 2, start=0, end=900, text="new pass", status="provisional", retranscription_token=token1)
    )
    assert provisional.applied and transcript.segment(0).end == 900

    token2 = transcript.begin_retranscription(0)
    superseded = transcript.apply(
        result(0, 3, start=0, end=800, text="old pass", status="provisional", retranscription_token=token1)
    )
    assert superseded.reason == "retranscription_not_active"
    final = transcript.apply(
        result(0, 4, start=0, end=700, text="new final", retranscription_token=token2)
    )
    assert final.applied and transcript.segment(0).status == "final"

    late = transcript.apply(
        result(0, 5, start=0, end=650, text="late pass", status="provisional", retranscription_token=token2)
    )
    assert late.reason == "retranscription_not_active"
    normal = transcript.apply(result(0, 6, start=0, end=700, text="other final"))
    assert normal.reason == "final_immutable"

    token3 = transcript.begin_retranscription(0)
    assert transcript.end_retranscription(token3)
    assert not transcript.end_retranscription(token3)
    cancelled = transcript.apply(
        result(0, 7, start=0, end=600, text="cancelled", status="provisional", retranscription_token=token3)
    )
    assert cancelled.reason == "retranscription_not_active"


def test_retranscription_scope_survives_document_replay() -> None:
    transcript = Transcript()
    transcript.apply(result(0, 1, start=0, end=1000, text="old final"))
    token = transcript.begin_retranscription(0)
    transcript.apply(result(0, 2, start=0, end=900, text="new pass", status="provisional", retranscription_token=token))
    transcript.apply(result(0, 3, start=0, end=900, text="new final", retranscription_token=token))

    rebuilt = Transcript.from_documents(transcript.history_documents())
    assert rebuilt.segment(0).status == "final"
    assert rebuilt.segment(0).end == 900
    assert rebuilt.text() == "new final"


def test_replay_refuses_to_silently_drop_segments_at_the_capacity_limit() -> None:
    documents = [
        result(0, 1, start=0, end=100, text="zero").as_document(),
        result(1, 1, start=100, end=200, text="one").as_document(),
    ]
    with pytest.raises(TranscriptError) as caught:
        Transcript.from_documents(documents, max_active_segments=1)
    assert caught.value.code == "replay_incomplete"
    assert caught.value.details["reason"] == "active_segments_full"


def test_pruning_fails_closed_without_sink_or_when_persistence_raises() -> None:
    no_sink = Transcript(live_revisions_per_segment=1)
    assert no_sink.apply(result(0, 1, start=0, end=100, text="one", status="provisional")).applied
    with pytest.raises(TranscriptError) as required:
        no_sink.apply(result(0, 2, start=0, end=200, text="two", status="provisional"))
    assert required.value.code == "persistence_required"
    assert [doc["revision"] for doc in no_sink.history_documents()] == [1]
    assert no_sink.segment(0).text == "one"

    def failing(records):
        raise OSError("disk full")

    broken = Transcript(live_revisions_per_segment=1, persist_revisions=failing)
    assert broken.apply(result(0, 1, start=0, end=100, text="one", status="provisional")).applied
    with pytest.raises(TranscriptError) as failed:
        broken.apply(result(0, 2, start=0, end=200, text="two", status="provisional"))
    assert failed.value.code == "persistence_failed"
    assert [doc["revision"] for doc in broken.history_documents()] == [1]
    assert broken.segment(0).text == "one"


def test_paging_fails_closed_before_removal_on_persistence_failure() -> None:
    without = Transcript(max_active_segments=1)
    assert without.apply(result(0, 1, start=0, end=100, text="done")).applied
    with pytest.raises(TranscriptError) as required:
        without.page_out_through(0)
    assert required.value.code == "persistence_required"
    assert without.active_segment_ids == (0,)
    assert without.paged_through == -1

    def failing(records):
        raise OSError("disk full")

    broken = Transcript(max_active_segments=1, persist_revisions=failing)
    assert broken.apply(result(0, 1, start=0, end=100, text="done")).applied
    with pytest.raises(TranscriptError) as failed:
        broken.page_out_through(0)
    assert failed.value.code == "persistence_failed"
    assert broken.active_segment_ids == (0,)
    assert broken.paged_through == -1


def test_per_text_length_is_bounded() -> None:
    transcript = Transcript(max_text_chars=8)
    assert transcript.apply(result(0, 1, start=0, end=100, text="12345678")).applied
    with pytest.raises(TranscriptError) as too_long:
        transcript.apply(result(1, 1, start=100, end=200, text="123456789"))
    assert too_long.value.code == "text_too_long"
    assert transcript.segment(1) is None
    with pytest.raises(TranscriptError) as replay_too_long:
        Transcript.from_documents([result(1, 1, start=0, end=1, text="123456789").as_document()], max_text_chars=8)
    assert replay_too_long.value.code == "text_too_long"


def test_replay_streams_input_and_stops_at_capacity_without_consuming_rest() -> None:
    consumed = []

    def documents():
        for index, document in enumerate(
            [
                result(0, 1, start=0, end=1, text="zero").as_document(),
                result(1, 1, start=1, end=2, text="one").as_document(),
                result(2, 1, start=2, end=3, text="two").as_document(),
            ]
        ):
            consumed.append(index)
            yield document

    with pytest.raises(TranscriptError) as caught:
        Transcript.from_documents(documents(), max_active_segments=1)
    assert caught.value.code == "replay_incomplete"
    assert caught.value.details["reason"] == "active_segments_full"
    assert consumed == [0, 1]


def test_checkpoint_restores_paged_boundary_and_join_context_for_tail_replay() -> None:
    persisted = []
    transcript = Transcript(max_active_segments=2, persist_revisions=persisted.extend)
    transcript.apply(result(0, 1, start=0, end=RATE, text="I said go home"))
    transcript.apply(
        result(1, 1, start=RATE, end=2 * RATE, text="I said go home again", window_start=RATE - 400)
    )
    transcript.page_out_through(0)
    checkpoint = transcript.checkpoint()
    assert checkpoint.paged_through == 0
    assert checkpoint.last_paged is not None and checkpoint.last_paged.text == "I said go home"

    restored_checkpoint = TranscriptCheckpoint.from_document(checkpoint.as_document())
    tail = [transcript.segment(1).as_document()]
    restored = Transcript.from_documents(tail, checkpoint=restored_checkpoint)
    assert restored.paged_through == 0
    assert restored.active_segment_ids == (1,)
    assert restored.segment(1).uncertain_join
    assert restored.text() == "I said go home again"

    wrong_generation = TranscriptCheckpoint(generation=99, paged_through=0, last_paged=checkpoint.last_paged)
    with pytest.raises(TranscriptError) as mismatch:
        Transcript(generation=0).restore_checkpoint(wrong_generation)
    assert mismatch.value.code == "checkpoint_generation"


def test_terminal_failed_segments_can_be_paged_and_sustain_long_runs() -> None:
    persisted = []
    transcript = Transcript(max_active_segments=2, live_revisions_per_segment=2, persist_revisions=persisted.extend)
    for index in range(60):
        failed = index % 7 == 0
        outcome = transcript.apply(
            result(
                index,
                1,
                start=index * 100,
                end=(index + 1) * 100,
                text="",
                status="error" if failed else "final",
                attempt=MAX_ATTEMPTS if failed else 1,
            )
        )
        assert outcome.applied
        if index:
            transcript.page_out_through(index - 1)

    assert transcript.active_segment_ids == (59,)
    assert [segment.segment_id for segment in persisted] == list(range(59))
    assert any(segment.status == "error" and segment.attempt == MAX_ATTEMPTS for segment in persisted)
    assert Transcript.terminal(transcript.segment(59))

    retryable = Transcript(max_active_segments=1)
    retryable.apply(result(0, 1, start=0, end=100, text="", status="error", attempt=MAX_ATTEMPTS - 1))
    assert not Transcript.terminal(retryable.segment(0))
    with pytest.raises(TranscriptError) as caught:
        retryable.page_out_through(0)
    assert caught.value.code == "segment_not_terminal"


def test_reserve_through_restores_the_revision_floor_and_survives_replay() -> None:
    transcript = Transcript()
    assert transcript.reserve_through(4, 12) == 12
    assert transcript.next_revision(4) == 13
    assert transcript.reserve_through(4, 5) == 13
    assert transcript.reserve_through(4, 20) == 20
    assert transcript.next_revision(4) == 21

    checkpoint = TranscriptCheckpoint.from_document(transcript.checkpoint().as_document())
    assert (4, 21) in checkpoint.revision_floors
    restored = Transcript.from_documents(
        [result(4, 21, start=400, end=500, text="after restart", status="provisional").as_document()],
        checkpoint=checkpoint,
    )
    assert restored.segment(4).revision == 21
    assert restored.next_revision(4) == 22


def test_apply_tolerant_skips_replay_duplicates_but_apply_stays_strict() -> None:
    transcript = Transcript()
    first = result(0, 1, start=0, end=100, text="done")
    assert transcript.apply(first).applied
    duplicate = transcript.apply_tolerant(first)
    assert duplicate.accepted and duplicate.reason == "skipped_duplicate"
    assert transcript.apply(result(0, 2, start=0, end=100, text="done")).applied

    stale = transcript.apply_tolerant(result(0, 1, start=0, end=100, text="done"))
    assert stale.accepted and stale.reason == "skipped_stale_revision"
    strict = transcript.apply(result(0, 1, start=0, end=100, text="done"))
    assert not strict.accepted and strict.reason == "stale_revision"

    immutable = transcript.apply_tolerant(result(0, 3, start=0, end=100, text="different"))
    assert not immutable.accepted and immutable.reason == "final_immutable"


def test_invalid_revision_data_is_rejected_without_changing_state() -> None:
    transcript = Transcript()
    transcript.apply(result(0, 1, start=0, end=100, text="ok"))

    bad_status = TranscriptSegment(segment_id=1, revision=1, start=0, end=1, status="unknown", raw_text="x")
    with pytest.raises(TranscriptError) as caught:
        transcript.apply(bad_status)
    assert caught.value.code == "invalid_status"

    with pytest.raises(TranscriptError):
        transcript.apply(result(1, 1, start=10, end=5, text="backwards"))
    with pytest.raises(TranscriptError):
        transcript.apply(result(1, 0, start=0, end=5, text="revision zero"))
    with pytest.raises(TranscriptError):
        transcript.apply(result(1, 1, start=10, end=15, text="bad window", window_start=11))
    with pytest.raises(TranscriptError):
        transcript.apply(result(1, 1, start=0, end=5, text="attempt", attempt=MAX_ATTEMPTS + 1))
    with pytest.raises(ValueError):
        Transcript(live_revisions_per_segment=0)

    assert transcript.text() == "ok"
    assert transcript.segment(1) is None
