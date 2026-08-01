"""The dashboard must not overstate what happened.

Two display bugs, both of the same kind — a number that means one thing being
labelled as another:

* **Safe mode reason: unknown.** When no breaker has tripped there *is* no
  reason, and "unknown" reads as "something tripped and we lost the detail".
  The honest word is "None".
* **Demo orders blocked.** This counted every strategy signal that did not
  become a real order, including the overwhelming majority that were never
  order candidates at all. The bot looked like it was being constantly refused
  when it was mostly doing shadow research exactly as designed.

The page is plain JavaScript with no build step, so these tests run the real
functions in node rather than asserting on the source text. If node is absent
the behavioural tests skip and the structural ones still run.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import textwrap

import pytest

from btcbot.dashboard.server import _PAGE

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def page_script() -> str:
    # `create_app` substitutes the refresh interval before serving; do the
    # same, or the page's own bootstrap line is a syntax-level landmine.
    page = _PAGE.replace("__REFRESH__", "5000")
    match = re.search(r"<script>\n(.*?)\n</script>", page, re.S)
    assert match, "the dashboard page has no script block"
    return match.group(1)


def call_in_node(expression: str) -> str:
    """Evaluate an expression against the page's real functions."""
    script = page_script()
    # The page's own bootstrap calls setInterval and fetch on load; stub the
    # browser surface it touches so the functions can be exercised directly.
    harness = textwrap.dedent(
        """
        globalThis.__dom = {};
        globalThis.document = {
          getElementById: (id) => ({ set innerHTML(v){ globalThis.__dom[id] = v; } }),
        };
        globalThis.fetch = async () => ({ json: async () => ({}) });
        globalThis.setInterval = () => 0;
        globalThis.addEventListener = () => {};
        """
    )
    source = f"{harness}\n{script}\nconsole.log(String({expression}));"
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", source],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


class TestSafeModeDisplay:
    @needs_node
    def test_an_inactive_safe_mode_shows_CLEAR_and_None(self):
        state = {"safety": {"safe_mode": {"active": False, "reason": "", "recent": []}}}
        html = call_in_node(f"safeModeRows({json.dumps(state)})")

        assert "CLEAR" in html
        assert "Safe mode reason" in html
        assert ">None<" in html
        assert "unknown" not in html.lower()

    @needs_node
    def test_a_missing_safety_block_still_reads_CLEAR_not_unknown(self):
        """A state snapshot taken before the breakers exist must not lie."""
        html = call_in_node("safeModeRows({})")

        assert "CLEAR" in html
        assert ">None<" in html
        assert "unknown" not in html.lower()

    @needs_node
    def test_an_active_safe_mode_shows_the_exact_breaker_and_reason(self):
        state = {
            "safety": {
                "safe_mode": {
                    "active": True,
                    "reason": "unprotected_position: BTC-USDT-SWAP had no verified stop",
                    "entered_at": "2026-08-01T10:00:00+00:00",
                    "recent": [
                        {"breaker": "unprotected_position",
                         "reason": "BTC-USDT-SWAP had no verified stop",
                         "at": "2026-08-01T10:00:00+00:00"},
                    ],
                }
            }
        }
        html = call_in_node(f"safeModeRows({json.dumps(state)})")

        assert "ACTIVE" in html
        assert "unprotected_position" in html
        assert "had no verified stop" in html
        assert "2026-08-01T10:00:00" in html

    @needs_node
    def test_an_active_breaker_without_a_reason_says_so_rather_than_unknown(self):
        state = {"safety": {"safe_mode": {"active": True, "reason": "", "recent": []}}}
        html = call_in_node(f"safeModeRows({json.dumps(state)})")

        assert "ACTIVE" in html
        assert "check the logs" in html
        assert "unknown" not in html.lower()

    def test_the_page_never_falls_back_to_the_word_unknown_for_safe_mode(self):
        script = page_script()
        safe_mode_fn = script[script.index("function safeModeRows") :]
        safe_mode_fn = safe_mode_fn[: safe_mode_fn.index("\n}")]
        assert "unknown" not in safe_mode_fn.lower()


class TestTheSignalFunnelIsSeparatedFromBlockedOrders:
    def test_the_page_no_longer_labels_signals_as_blocked_demo_orders(self):
        assert "Demo orders blocked" not in _PAGE, (
            "the ambiguous counter is back — it counted signals, not orders"
        )

    @pytest.mark.parametrize(
        "label",
        [
            "Signals evaluated",
            "Shadow-only signals",
            "Preliminary eligibility rejections",
            "Final execution blocks",
            "Actual Demo orders sent",
        ],
    )
    def test_each_required_stage_has_its_own_row(self, label):
        assert label in _PAGE, label

    @needs_node
    def test_the_stages_render_as_distinct_numbers(self):
        """The failure mode is one number shown under several labels."""
        state = {
            "system": {}, "market": {}, "regime": {}, "research": {},
            "pipeline": {
                "signals_evaluated": 237,
                "decision_rejections": 40,
                "eligibility_assessed": 197,
                "shadow_only_signals": 190,
                "eligibility_rejections": 188,
                "final_execution_blocks": 3,
                "actual_orders_sent": 4,
                "eligibility_reasons": [["stop distance 0.0400% is below", 120]],
                "cost_model": {
                    "taker_fee_pct": 0.25, "round_trip_pct": 0.56,
                    "spread_bps": 2.0, "slippage_bps": 2.0, "source": "exchange",
                },
            },
        }
        call_in_node(f"render({json.dumps(state)})")
        html = call_in_node(f"(render({json.dumps(state)}), globalThis.__dom.grid)")

        for stage, value in (
            ("Signals evaluated", 237),
            ("Shadow-only signals", 190),
            ("Preliminary eligibility rejections", 188),
            ("Actual Demo orders sent", 4),
        ):
            assert stage in html, stage
            assert f">{value}<" in html, f"{stage} did not render as {value}"
        # The one that mattered: only three orders were actually blocked.
        assert "Final execution blocks (layers 8-10)" in html
        assert ">3<" in html

    def test_the_cost_model_is_shown_so_the_thresholds_can_be_checked(self):
        for label in ("Taker fee (per side)", "Round trip", "Fee source"):
            assert label in _PAGE, label


class TestStateShape:
    def test_the_orchestrator_pipeline_block_carries_every_rendered_field(self):
        """The page reads these keys; the state provider must produce them."""
        import inspect

        from btcbot.app import orchestrator

        source = inspect.getsource(orchestrator.Orchestrator._pipeline_counters)
        for key in (
            "signals_evaluated", "decision_rejections", "eligibility_assessed",
            "shadow_only_signals", "eligibility_rejections",
            "final_execution_blocks", "actual_orders_sent", "cost_model",
        ):
            assert f'"{key}"' in source, key

    def test_the_page_is_still_self_contained(self):
        """No build step, no external assets — it must not become one."""
        assert "<script src=" not in _PAGE
        assert "<link" not in _PAGE
        assert "https://" not in _PAGE
