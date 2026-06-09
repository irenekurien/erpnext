# Copyright (c) 2019, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from erpnext.loan_management.doctype.loan_repayment.loan_repayment import (
    INTEREST_BEFORE_SHORTFALL,
    SHORTFALL_BEFORE_INTEREST,
    LoanRepayment,
)


class MockDoc:
    """Minimal Frappe Document substitute for testing allocate_amounts."""

    def set(self, key, value):
        setattr(self, key, value)

    def append(self, key, value):
        table = getattr(self, key, [])
        row = SimpleNamespace(**value, idx=len(table) + 1)
        table.append(row)
        setattr(self, key, table)

    def get(self, key, default=None):
        return getattr(self, key, default)


def _allocate(amount_paid, penalty=0, shortfall=0, interest=0, interest_entries=None, allocation_order=None):
    """
    Run allocate_amounts on a MockDoc and return it.

    interest       — shorthand: single accrual entry with this interest amount
    interest_entries — list of (interest_amount, payable_principal_amount) tuples for
                       multi-entry scenarios; takes precedence over `interest`

    is_term_loan=0 and unaccrued_interest=0 keep the demand-loan path free of DB calls.
    """
    doc = MockDoc()
    doc.amount_paid = amount_paid
    doc.penalty_amount = penalty
    doc.shortfall_amount = shortfall
    doc.is_term_loan = 0
    if allocation_order is not None:
        doc.allocation_order = allocation_order

    # Bind LoanRepayment helper methods onto the mock so self.* calls resolve.
    for method in (
        "allocate_interest_amount",
        "allocate_excess_payment_for_demand_loans",
        "allocate_principal_amount_for_term_loans",
    ):
        setattr(doc, method, types.MethodType(getattr(LoanRepayment, method), doc))

    pending = {}
    if interest_entries:
        for i, (i_amount, p_amount) in enumerate(interest_entries):
            pending[f"LIA-{i+1:03d}"] = {
                "interest_amount": float(i_amount),
                "payable_principal_amount": float(p_amount),
            }
    elif interest:
        pending["LIA-001"] = {"interest_amount": float(interest), "payable_principal_amount": 0.0}

    details = {"pending_accrual_entries": pending, "unaccrued_interest": 0}

    module = "erpnext.loan_management.doctype.loan_repayment.loan_repayment"
    with patch(f"{module}.frappe") as mock_frappe:
        mock_frappe.db.get_default.return_value = "2"
        LoanRepayment.allocate_amounts(doc, details)

    return doc


class TestAllocateAmountsDefaultOrder(unittest.TestCase):
    # Default order: penalty → interest → shortfall → principal

    def test_payment_covers_penalty_only(self):
        doc = _allocate(amount_paid=50, penalty=100, interest=200, shortfall=300)
        self.assertEqual(doc.total_penalty_paid, 50)
        self.assertEqual(doc.total_interest_paid, 0)
        self.assertEqual(doc.principal_amount_paid, 0)

    def test_payment_covers_penalty_and_partial_interest(self):
        doc = _allocate(amount_paid=250, penalty=100, interest=200, shortfall=300)
        self.assertEqual(doc.total_penalty_paid, 100)
        self.assertEqual(doc.total_interest_paid, 150)
        self.assertEqual(doc.principal_amount_paid, 0)

    def test_payment_covers_penalty_interest_and_partial_shortfall(self):
        doc = _allocate(amount_paid=450, penalty=100, interest=200, shortfall=300)
        self.assertEqual(doc.total_penalty_paid, 100)
        self.assertEqual(doc.total_interest_paid, 200)
        self.assertEqual(doc.principal_amount_paid, 150)

    def test_payment_covers_all_with_leftover_principal(self):
        # 100 penalty + 200 interest + 300 shortfall + 100 extra = 700
        doc = _allocate(amount_paid=700, penalty=100, interest=200, shortfall=300)
        self.assertEqual(doc.total_penalty_paid, 100)
        self.assertEqual(doc.total_interest_paid, 200)
        self.assertEqual(doc.principal_amount_paid, 400)  # 300 shortfall + 100 leftover

    def test_exact_payment_for_penalty_and_interest(self):
        doc = _allocate(amount_paid=300, penalty=100, interest=200, shortfall=500)
        self.assertEqual(doc.total_penalty_paid, 100)
        self.assertEqual(doc.total_interest_paid, 200)
        self.assertEqual(doc.principal_amount_paid, 0)


class TestAllocateAmountsShortfallFirst(unittest.TestCase):
    # SHORTFALL_BEFORE_INTEREST order: shortfall → penalty → interest → principal

    def test_payment_covers_shortfall_only(self):
        doc = _allocate(
            amount_paid=200, penalty=100, interest=200, shortfall=300,
            allocation_order=SHORTFALL_BEFORE_INTEREST,
        )
        self.assertEqual(doc.principal_amount_paid, 200)
        self.assertEqual(doc.total_penalty_paid, 0)
        self.assertEqual(doc.total_interest_paid, 0)

    def test_payment_covers_shortfall_and_partial_penalty(self):
        doc = _allocate(
            amount_paid=350, penalty=100, interest=200, shortfall=300,
            allocation_order=SHORTFALL_BEFORE_INTEREST,
        )
        self.assertEqual(doc.principal_amount_paid, 300)
        self.assertEqual(doc.total_penalty_paid, 50)
        self.assertEqual(doc.total_interest_paid, 0)

    def test_payment_covers_shortfall_penalty_and_partial_interest(self):
        doc = _allocate(
            amount_paid=500, penalty=100, interest=200, shortfall=300,
            allocation_order=SHORTFALL_BEFORE_INTEREST,
        )
        self.assertEqual(doc.principal_amount_paid, 300)
        self.assertEqual(doc.total_penalty_paid, 100)
        self.assertEqual(doc.total_interest_paid, 100)

    def test_payment_covers_all_with_leftover_principal(self):
        # 300 shortfall + 100 penalty + 200 interest + 100 extra = 700
        doc = _allocate(
            amount_paid=700, penalty=100, interest=200, shortfall=300,
            allocation_order=SHORTFALL_BEFORE_INTEREST,
        )
        self.assertEqual(doc.total_penalty_paid, 100)
        self.assertEqual(doc.total_interest_paid, 200)
        self.assertEqual(doc.principal_amount_paid, 400)  # 300 shortfall + 100 leftover


class TestAllocateAmountsNoShortfall(unittest.TestCase):
    # When shortfall=0, both orders produce the same result.

    def test_default_order_no_shortfall(self):
        doc = _allocate(amount_paid=300, penalty=100, interest=200)
        self.assertEqual(doc.total_penalty_paid, 100)
        self.assertEqual(doc.total_interest_paid, 200)
        self.assertEqual(doc.principal_amount_paid, 0)

    def test_shortfall_first_order_no_shortfall(self):
        doc = _allocate(
            amount_paid=300, penalty=100, interest=200,
            allocation_order=SHORTFALL_BEFORE_INTEREST,
        )
        self.assertEqual(doc.total_penalty_paid, 100)
        self.assertEqual(doc.total_interest_paid, 200)
        self.assertEqual(doc.principal_amount_paid, 0)

    def test_interest_before_shortfall_constant_is_default(self):
        doc_default = _allocate(amount_paid=300, penalty=100, interest=200, shortfall=500)
        doc_explicit = _allocate(
            amount_paid=300, penalty=100, interest=200, shortfall=500,
            allocation_order=INTEREST_BEFORE_SHORTFALL,
        )
        self.assertEqual(doc_default.total_penalty_paid, doc_explicit.total_penalty_paid)
        self.assertEqual(doc_default.total_interest_paid, doc_explicit.total_interest_paid)
        self.assertEqual(doc_default.principal_amount_paid, doc_explicit.principal_amount_paid)


class TestAllocateAmountsMultipleAccrualEntries(unittest.TestCase):
    # Verifies allocate_interest_amount iterates over entries in order.

    def test_payment_covers_first_entry_fully_second_partially(self):
        # 2 entries: LIA-001 (100), LIA-002 (200). After penalty=50, remaining=200.
        doc = _allocate(
            amount_paid=250, penalty=50,
            interest_entries=[(100, 0), (200, 0)],
        )
        self.assertEqual(doc.total_penalty_paid, 50)
        self.assertEqual(doc.total_interest_paid, 200)  # 100 from LIA-001 + 100 from LIA-002
        self.assertEqual(doc.principal_amount_paid, 0)

        rows = doc.repayment_details
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].paid_interest_amount, 100)
        self.assertEqual(rows[1].paid_interest_amount, 100)

    def test_payment_covers_both_entries_fully_with_leftover(self):
        # 2 entries: 100 + 200 = 300 total interest. amount=400, penalty=50.
        doc = _allocate(
            amount_paid=400, penalty=50,
            interest_entries=[(100, 0), (200, 0)],
        )
        self.assertEqual(doc.total_penalty_paid, 50)
        self.assertEqual(doc.total_interest_paid, 300)
        self.assertEqual(doc.principal_amount_paid, 50)  # leftover after penalty+interest


class TestAllocateAmountsZeroPenalty(unittest.TestCase):
    # penalty=0 means the penalty block is skipped entirely.

    def test_zero_penalty_default_order(self):
        # Full amount should go straight to interest then shortfall.
        doc = _allocate(amount_paid=500, penalty=0, interest=200, shortfall=300)
        self.assertEqual(doc.total_penalty_paid, 0)
        self.assertEqual(doc.total_interest_paid, 200)
        self.assertEqual(doc.principal_amount_paid, 300)

    def test_zero_penalty_shortfall_first(self):
        doc = _allocate(
            amount_paid=500, penalty=0, interest=200, shortfall=300,
            allocation_order=SHORTFALL_BEFORE_INTEREST,
        )
        self.assertEqual(doc.total_penalty_paid, 0)
        self.assertEqual(doc.total_interest_paid, 200)
        self.assertEqual(doc.principal_amount_paid, 300)


class TestAllocateAmountsNoAccrualEntries(unittest.TestCase):
    # No pending accrual entries: interest allocation is a no-op and full
    # remaining (after penalty) flows directly to shortfall / principal.

    def test_default_order_no_entries_goes_to_shortfall(self):
        doc = _allocate(amount_paid=400, penalty=100, shortfall=300)
        self.assertEqual(doc.total_penalty_paid, 100)
        self.assertEqual(doc.total_interest_paid, 0)
        self.assertEqual(doc.principal_amount_paid, 300)

    def test_default_order_no_entries_no_shortfall_goes_to_principal(self):
        doc = _allocate(amount_paid=500, penalty=100)
        self.assertEqual(doc.total_penalty_paid, 100)
        self.assertEqual(doc.total_interest_paid, 0)
        self.assertEqual(doc.principal_amount_paid, 400)

    def test_shortfall_first_no_entries_goes_to_shortfall_then_principal(self):
        doc = _allocate(
            amount_paid=500, penalty=100, shortfall=300,
            allocation_order=SHORTFALL_BEFORE_INTEREST,
        )
        self.assertEqual(doc.total_penalty_paid, 100)
        self.assertEqual(doc.total_interest_paid, 0)
        self.assertEqual(doc.principal_amount_paid, 400)  # 300 shortfall + 100 leftover
