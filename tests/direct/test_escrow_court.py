"""Direct-mode tests for AdjudicatedEscrow (genlayer-test 0.30.1).

Toolchain notes — read before touching the harness (see also
``tests/direct/conftest.py``):

- genlayer-test 0.30.1 targets the GenVM v0.3 SDK layout; the v0.6 std
  moved modules under ``genlayer.py.*``. ``conftest.py`` rebinds the
  loader's helpers and its contract allocator to the v0.6 layout.
- The v0.6 std snapshots ``gl.message`` once, at contract import time,
  and the loader only injects the message then. ``_sync_message()``
  rebuilds that snapshot from the VM before every call, so sender /
  value / warp changes made by a test are actually seen by the contract.
- The loader never captures the ``run_nondet_unsafe`` validator in this
  version (its patch targets ``genlayer.vm``, which does not exist in the
  v0.6 std, and only covers ``run_nondet`` anyway).
  ``_install_nondet_shim()`` aliases ``genlayer.vm`` to
  ``genlayer.gl.vm`` and replaces ``run_nondet_unsafe`` with a direct
  executor that records the validator for ``vm.run_validator()``.
- ``emit_transfer`` is a no-op in direct mode (the mock has no handler
  for the transfer message) and a payable ``value`` is not deducted from
  the sender. These tests therefore assert the contract's own payout
  accounting (``seller_paid`` / ``buyer_paid`` / ``status`` /
  ``outcome``), not balances. Real value movement is only exercised on a
  live network / GenLayer Studio.
- Always run pytest with stdin from ``/dev/null``: the std reads the
  message context from fd 0 at import time, and a bare ``pytest`` run
  inherits a pipe that blocks forever on the first contract import.
"""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

AMOUNT = 10 * 10**18  # 10 GEN, in atto units
T0 = "2026-11-26T00:00:00Z"
T0_EPOCH = int(datetime(2026, 11, 26, tzinfo=timezone.utc).timestamp())
DAY = 86400

SELLER_EVIDENCE = "Tracking RESI-12345: parcel delivered to the buyer, photo attached."
BUYER_CLAIM = "Wrong item arrived: ordered a phone, received an empty box."
BUYER_EVIDENCE = "Photo of the opened empty box next to the shipping label."
EVIDENCE_URL = "https://example.com/delivery-proof"
EVIDENCE_PAGE = "Delivery confirmation: parcel RESI-12345 signed for by the buyer."


# --------------------------------------------------------------------------
# harness helpers
# --------------------------------------------------------------------------


def _sync_message(vm):
    """Rebuild the v0.6 std's ``gl.message`` snapshot from the VM state.

    The std builds ``gl.message`` once at import from the dict decoded
    off fd 0; later ``vm.sender`` / ``vm.value`` / ``vm.warp`` changes
    are invisible to the contract unless this runs first.
    """
    import genlayer.gl as gl_mod
    import genlayer._internal.msg as msg_mod
    from genlayer.py.types import Address, u256

    raw = vm.get_message_raw()

    def _addr(a):
        return a if isinstance(a, Address) else Address(a)

    message_raw = msg_mod.message_raw
    message_raw.clear()
    message_raw.update(
        {
            "contract_address": _addr(raw["contract_address"]),
            "sender_address": _addr(raw["sender_address"]),
            "origin_address": _addr(raw["origin_address"]),
            "stack": raw["stack"],
            "value": u256(raw["value"]),
            "datetime": raw["datetime"],
            "is_init": raw["is_init"],
            "chain_id": u256(raw["chain_id"]),
            "create2": raw["create2"],
            "entry_kind": raw["entry_kind"],
            "entry_data": raw["entry_data"],
            "entry_stage_data": raw["entry_stage_data"],
        }
    )
    gl_mod.message = gl_mod.MessageType(
        contract_address=message_raw["contract_address"],
        sender_address=message_raw["sender_address"],
        origin_address=message_raw["origin_address"],
        value=u256(message_raw["value"]),
        chain_id=u256(message_raw["chain_id"]),
    )


def _install_nondet_shim(vm):
    """Make ``run_nondet_unsafe`` testable in direct mode.

    Aliases ``genlayer.vm`` (missing in the v0.6 std) to
    ``genlayer.gl.vm`` so ``vm.run_validator()`` can import it, and
    replaces ``run_nondet_unsafe`` with a direct executor that runs the
    leader immediately and records ``(result, leader, validator)`` for
    ``vm.run_validator()``.
    """
    import sys

    import genlayer.gl.vm as gl_vm
    from gltest.direct import wasi_mock

    sys.modules.setdefault("genlayer.vm", gl_vm)
    if getattr(gl_vm, "_direct_test_patched", False):
        return

    def _direct_run_nondet_unsafe(leader_fn, validator_fn, /):
        _vm = wasi_mock.get_vm()
        _vm._in_nondet = True
        try:
            result = leader_fn()
        finally:
            _vm._in_nondet = False
        _vm._captured_validators.append((result, leader_fn, validator_fn))
        return result

    gl_vm.run_nondet_unsafe = _direct_run_nondet_unsafe
    gl_vm._direct_test_patched = True


def _hex(addr) -> str:
    """Canonical checksummed-hex string for a test address."""
    from genlayer.py.types import Address

    return Address(addr).as_hex if not isinstance(addr, Address) else addr.as_hex


@pytest.fixture
def env(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/escrow_court.py")
    _install_nondet_shim(direct_vm)
    direct_vm.warp(T0)
    _sync_message(direct_vm)
    return SimpleNamespace(
        vm=direct_vm, contract=contract, buyer=direct_alice, seller=direct_bob
    )


def _as(env, addr):
    """Act as ``addr`` for the next call(s)."""
    env.vm.sender = addr
    _sync_message(env.vm)


def _warp(env, iso):
    env.vm.warp(iso)
    _sync_message(env.vm)


def _create(env, amount=AMOUNT, delivery_days=7, dispute_days=7, seller=None):
    env.vm.deal(env.buyer, 100 * AMOUNT)
    _as(env, env.buyer)
    env.vm.value = amount
    _sync_message(env.vm)
    escrow_id = env.contract.create_escrow(
        seller if seller is not None else env.seller,
        "Test item",
        "A test item in good condition.",
        delivery_days,
        dispute_days,
    )
    env.vm.value = 0
    _sync_message(env.vm)
    return escrow_id


def _record(env, escrow_id):
    return json.loads(env.contract.get_escrow(escrow_id))


def _deliver(env, escrow_id, evidence=SELLER_EVIDENCE, url=""):
    _as(env, env.seller)
    env.contract.mark_delivered(escrow_id, evidence, url)


def _dispute(env, escrow_id, by="buyer", claim=BUYER_CLAIM, evidence=BUYER_EVIDENCE):
    _as(env, env.buyer if by == "buyer" else env.seller)
    env.contract.open_dispute(escrow_id, claim, evidence)


def _disputed(env, with_url=False):
    escrow_id = _create(env)
    _deliver(env, escrow_id, url=EVIDENCE_URL if with_url else "")
    _dispute(env, escrow_id)
    return escrow_id


def _mock_verdict(vm, verdict, seller_bps=0, confidence=80, reasoning="Judged."):
    vm.mock_llm(
        "adjudicator",
        json.dumps(
            {
                "verdict": verdict,
                "seller_bps": seller_bps,
                "confidence": confidence,
                "reasoning": reasoning,
            }
        ),
    )


def _mock_page(vm, body=EVIDENCE_PAGE):
    vm.mock_web("example.com", {"body": body})


def _adjudicate(env, escrow_id):
    _as(env, env.buyer)
    env.contract.adjudicate(escrow_id)


# --------------------------------------------------------------------------
# creation
# --------------------------------------------------------------------------


def test_create_and_read_fields(env):
    escrow_id = _create(env)
    assert escrow_id == 0
    rec = _record(env, 0)
    assert rec["buyer"] == _hex(env.buyer)
    assert rec["seller"] == _hex(env.seller)
    assert rec["amount"] == AMOUNT
    assert rec["title"] == "Test item"
    assert rec["status"] == "FUNDED"
    assert rec["created_at"] == T0_EPOCH
    assert rec["delivery_deadline"] == T0_EPOCH + 7 * DAY
    assert rec["dispute_deadline"] == T0_EPOCH + 14 * DAY
    assert rec["seller_paid"] == 0
    assert rec["buyer_paid"] == 0


def test_total_escrows_counts(env):
    _create(env)
    _create(env)
    assert env.contract.total_escrows() == 2


def test_create_rejects_zero_value(env):
    with env.vm.expect_revert("Escrow amount must be positive"):
        _create(env, amount=0)


def test_create_rejects_same_buyer_and_seller(env):
    with env.vm.expect_revert("Buyer and seller must differ"):
        _create(env, seller=env.buyer)


def test_create_rejects_zero_address_seller(env):
    zero = bytes(20)
    with env.vm.expect_revert("Seller address must be set"):
        _create(env, seller=zero)


@pytest.mark.parametrize("title", ["", "x" * 201])
def test_create_rejects_bad_title(env, title):
    env.vm.deal(env.buyer, 100 * AMOUNT)
    _as(env, env.buyer)
    env.vm.value = AMOUNT
    _sync_message(env.vm)
    with env.vm.expect_revert("Title must be"):
        env.contract.create_escrow(env.seller, title, "desc", 7, 7)


def test_create_rejects_empty_description(env):
    env.vm.deal(env.buyer, 100 * AMOUNT)
    _as(env, env.buyer)
    env.vm.value = AMOUNT
    _sync_message(env.vm)
    with env.vm.expect_revert("Description must be"):
        env.contract.create_escrow(env.seller, "title", "", 7, 7)


@pytest.mark.parametrize("delivery_days,dispute_days", [(0, 7), (181, 7), (7, 0), (7, 31)])
def test_create_rejects_bad_windows(env, delivery_days, dispute_days):
    with env.vm.expect_revert("window must be"):
        _create(env, delivery_days=delivery_days, dispute_days=dispute_days)


def test_unknown_escrow_id_rejected(env):
    with env.vm.expect_revert("Unknown escrow id"):
        env.contract.get_escrow(99)


# --------------------------------------------------------------------------
# happy path + role / state guards
# --------------------------------------------------------------------------


def test_happy_path_confirm_releases_to_seller(env):
    escrow_id = _create(env)
    _deliver(env, escrow_id)
    _as(env, env.buyer)
    env.contract.confirm_receipt(escrow_id)
    rec = _record(env, escrow_id)
    assert rec["status"] == "CONFIRMED"
    assert rec["outcome"] == "release"
    assert rec["seller_bps"] == 10000
    assert rec["seller_paid"] == AMOUNT
    assert rec["buyer_paid"] == 0


def test_only_seller_can_mark_delivered(env):
    escrow_id = _create(env)
    _as(env, env.buyer)
    with env.vm.expect_revert("Only the seller can do this"):
        env.contract.mark_delivered(escrow_id, SELLER_EVIDENCE, "")


def test_only_buyer_can_confirm(env):
    escrow_id = _create(env)
    _deliver(env, escrow_id)
    _as(env, env.seller)
    with env.vm.expect_revert("Only the buyer can do this"):
        env.contract.confirm_receipt(escrow_id)


def test_confirm_requires_delivered_state(env):
    escrow_id = _create(env)
    _as(env, env.buyer)
    with env.vm.expect_revert("Only a delivered escrow can be confirmed"):
        env.contract.confirm_receipt(escrow_id)


def test_mark_delivered_twice_rejected(env):
    escrow_id = _create(env)
    _deliver(env, escrow_id)
    _as(env, env.seller)
    with env.vm.expect_revert("Only a funded escrow can be marked delivered"):
        env.contract.mark_delivered(escrow_id, SELLER_EVIDENCE, "")


def test_mark_delivered_rejects_empty_evidence_and_bad_url(env):
    escrow_id = _create(env)
    _as(env, env.seller)
    with env.vm.expect_revert("Delivery evidence must be"):
        env.contract.mark_delivered(escrow_id, "", "")
    with env.vm.expect_revert("Evidence URL must be an http(s) URL"):
        env.contract.mark_delivered(escrow_id, SELLER_EVIDENCE, "ftp://example.com/x")


def test_dispute_requires_delivered_state(env):
    escrow_id = _create(env)
    _as(env, env.buyer)
    with env.vm.expect_revert("Only a delivered escrow can be disputed"):
        env.contract.open_dispute(escrow_id, BUYER_CLAIM, BUYER_EVIDENCE)


# --------------------------------------------------------------------------
# timeouts
# --------------------------------------------------------------------------


def test_timeout_refund_after_delivery_deadline(env):
    escrow_id = _create(env)
    _warp(env, "2026-12-04T00:00:01Z")  # past 2026-12-03 delivery deadline
    _as(env, env.buyer)
    env.contract.claim_timeout_refund(escrow_id)
    rec = _record(env, escrow_id)
    assert rec["status"] == "REFUNDED"
    assert rec["outcome"] == "refund"
    assert rec["buyer_paid"] == AMOUNT
    assert rec["seller_paid"] == 0


def test_timeout_refund_before_deadline_rejected(env):
    escrow_id = _create(env)
    _as(env, env.buyer)
    with env.vm.expect_revert("Delivery deadline has not passed yet"):
        env.contract.claim_timeout_refund(escrow_id)


def test_refunded_escrow_is_final(env):
    escrow_id = _create(env)
    _warp(env, "2026-12-04T00:00:01Z")
    _as(env, env.buyer)
    env.contract.claim_timeout_refund(escrow_id)
    with env.vm.expect_revert("Only a funded, undelivered escrow can be refunded"):
        env.contract.claim_timeout_refund(escrow_id)
    _as(env, env.seller)
    with env.vm.expect_revert("Only a funded escrow can be marked delivered"):
        env.contract.mark_delivered(escrow_id, SELLER_EVIDENCE, "")


def test_silent_buyer_release_after_dispute_window(env):
    escrow_id = _create(env)
    _deliver(env, escrow_id)
    _warp(env, "2026-12-11T00:00:01Z")  # past 2026-12-10 dispute deadline
    _as(env, env.seller)
    env.contract.claim_release_after_window(escrow_id)
    rec = _record(env, escrow_id)
    assert rec["status"] == "CONFIRMED"
    assert rec["outcome"] == "release"
    assert rec["seller_paid"] == AMOUNT


def test_release_before_window_and_confirm_after_window_rejected(env):
    escrow_id = _create(env)
    _deliver(env, escrow_id)
    _as(env, env.seller)
    with env.vm.expect_revert("Dispute window has not closed yet"):
        env.contract.claim_release_after_window(escrow_id)
    _warp(env, "2026-12-11T00:00:01Z")
    _as(env, env.buyer)
    with env.vm.expect_revert("Dispute window has closed"):
        env.contract.confirm_receipt(escrow_id)


# --------------------------------------------------------------------------
# disputes
# --------------------------------------------------------------------------


def test_dispute_flow_and_second_dispute_rejected(env):
    escrow_id = _disputed(env)
    rec = _record(env, escrow_id)
    assert rec["status"] == "DISPUTED"
    assert rec["disputer"] == "buyer"
    assert rec["dispute_claim"] == BUYER_CLAIM
    _as(env, env.seller)
    with env.vm.expect_revert("Only a delivered escrow can be disputed"):
        env.contract.open_dispute(escrow_id, "again", "again")


def test_only_a_party_can_dispute(env, direct_charlie):
    escrow_id = _create(env)
    _deliver(env, escrow_id)
    _as(env, direct_charlie)
    with env.vm.expect_revert("Only a party can dispute"):
        env.contract.open_dispute(escrow_id, BUYER_CLAIM, BUYER_EVIDENCE)


def test_dispute_rejects_empty_claim_or_evidence(env):
    escrow_id = _create(env)
    _deliver(env, escrow_id)
    _as(env, env.buyer)
    with env.vm.expect_revert("Dispute claim must be"):
        env.contract.open_dispute(escrow_id, "", BUYER_EVIDENCE)
    with env.vm.expect_revert("Dispute evidence must be"):
        env.contract.open_dispute(escrow_id, BUYER_CLAIM, "")


# --------------------------------------------------------------------------
# adjudication
# --------------------------------------------------------------------------


def test_adjudicate_release_and_validator_agrees(env):
    escrow_id = _disputed(env)
    _mock_verdict(env.vm, "release", seller_bps=10000, confidence=90)
    _adjudicate(env, escrow_id)
    rec = _record(env, escrow_id)
    assert rec["status"] == "ADJUDICATED"
    assert rec["outcome"] == "release"
    assert rec["seller_bps"] == 10000
    assert rec["seller_paid"] == AMOUNT
    assert rec["buyer_paid"] == 0
    assert rec["confidence"] == 90
    assert env.vm.run_validator() is True


def test_adjudicate_refund(env):
    escrow_id = _disputed(env)
    _mock_verdict(env.vm, "refund", seller_bps=0, confidence=85)
    _adjudicate(env, escrow_id)
    rec = _record(env, escrow_id)
    assert rec["status"] == "ADJUDICATED"
    assert rec["outcome"] == "refund"
    assert rec["seller_paid"] == 0
    assert rec["buyer_paid"] == AMOUNT
    assert env.vm.run_validator() is True


def test_adjudicate_split(env):
    escrow_id = _disputed(env)
    _mock_verdict(env.vm, "split", seller_bps=6000, confidence=70)
    _adjudicate(env, escrow_id)
    rec = _record(env, escrow_id)
    assert rec["status"] == "ADJUDICATED"
    assert rec["outcome"] == "split"
    assert rec["seller_bps"] == 6000
    assert rec["seller_paid"] == 6 * 10**18
    assert rec["buyer_paid"] == 4 * 10**18
    assert env.vm.run_validator() is True


def test_adjudicate_with_evidence_url_uses_fetched_page(env):
    escrow_id = _disputed(env, with_url=True)
    _mock_page(env.vm)
    _mock_verdict(env.vm, "release", seller_bps=10000, confidence=95)
    _adjudicate(env, escrow_id)
    rec = _record(env, escrow_id)
    assert rec["outcome"] == "release"
    assert rec["seller_paid"] == AMOUNT


def test_adjudicate_tolerates_dead_evidence_url(env):
    # No web mock registered: the fetch fails, the text evidence alone
    # is still judged and adjudication must not block.
    escrow_id = _disputed(env, with_url=True)
    _mock_verdict(env.vm, "release", seller_bps=10000, confidence=60)
    _adjudicate(env, escrow_id)
    rec = _record(env, escrow_id)
    assert rec["status"] == "ADJUDICATED"
    assert rec["outcome"] == "release"


def test_adjudicate_rejects_unparsable_llm(env):
    escrow_id = _disputed(env)
    env.vm.mock_llm("adjudicator", "not json at all")
    with env.vm.expect_revert("[LLM_ERROR]"):
        _adjudicate(env, escrow_id)


def test_adjudicate_requires_disputed_state(env):
    escrow_id = _create(env)
    _mock_verdict(env.vm, "release", seller_bps=10000)
    with env.vm.expect_revert("Only a disputed escrow can be adjudicated"):
        _adjudicate(env, escrow_id)
    _deliver(env, escrow_id)
    _dispute(env, escrow_id)
    _adjudicate(env, escrow_id)
    with env.vm.expect_revert("Only a disputed escrow can be adjudicated"):
        _adjudicate(env, escrow_id)


def test_validator_disagrees_when_llm_changes(env):
    escrow_id = _disputed(env)
    _mock_verdict(env.vm, "release", seller_bps=10000, confidence=90)
    _adjudicate(env, escrow_id)
    # The validator re-runs the adjudication against the CURRENT mocks:
    # with a different verdict now, it must disagree.
    env.vm.clear_mocks()
    _mock_verdict(env.vm, "refund", seller_bps=0, confidence=90)
    assert env.vm.run_validator() is False


def test_validator_rejects_leader_error(env):
    import genlayer.gl.vm as gl_vm

    escrow_id = _disputed(env)
    _mock_verdict(env.vm, "release", seller_bps=10000, confidence=90)
    _adjudicate(env, escrow_id)
    # Leader errored, validator re-running the same work succeeds:
    # canonical handling says disagree (a new leader must be picked).
    assert (
        env.vm.run_validator(
            leader_error=gl_vm.UserError("[EXPECTED] simulated leader failure")
        )
        is False
    )
