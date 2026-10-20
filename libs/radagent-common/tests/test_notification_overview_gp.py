"""Guard: the critical-result notification is NOT on the patient overview widget (#136).

#123 registered the notification concept on `dashboard.overview.showConcepts`, because nothing
else surfaced a bare Observation on the legacy dashboard. That widget turned out to be the wrong
surface. It wraps the whole value in a link to the observation EDIT form, so the ack link inside
was never clickable and a referring physician who clicked the entry looped on the login page. It
renders one entry per patient. And it offers a "New" control on the AI concept. Found driving arc
2 in a browser on the demo host, 2026-10-08.

The bootstrap now takes the concept back off the widget, and the radiology module's own box at the
top of the Overview tab lists the notifications instead (o3 `o3-048ef7d6`, lh-radiology!100). The
removal is only safe on an o3 image that has that box, which is what the pin test at the bottom
holds together.

Lives in `ris-poller-tests` alongside test_presign_concept_drift.py for the same reason: a guard
CI never runs is not a guard.
"""
from __future__ import annotations

import importlib.util
import re
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
BOOTSTRAP_PATH = REPO_ROOT / "docker" / "openmrs" / "bootstrap_presign_concept.py"
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"

# o3 tags built before the module's Overview box existed. On any of these the overview widget is
# the ONLY place the notification renders, so removing it there makes a critical result invisible
# on the chart again (#123).
O3_TAGS_WITHOUT_THE_PANEL = {"o3-92019348", "o3-074c9b8d", "o3-31e7000a"}


def _load_bootstrap_module():
    """Import the bootstrap by path, stubbing pymysql (not installed in this test env; the
    script installs it at container start)."""
    if "pymysql" not in sys.modules:
        stub = types.ModuleType("pymysql")
        stub.Error = Exception
        stub.connections = types.SimpleNamespace(Connection=object)
        sys.modules["pymysql"] = stub
    spec = importlib.util.spec_from_file_location("bootstrap_presign_concept", BOOTSTRAP_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeCursor:
    """Answers the two SELECTs the function makes, and records the UPDATE."""

    def __init__(self, concept_id=42, gp_value="", gp_registered=True, concept_found=True):
        self.concept_id, self.gp_value = concept_id, gp_value
        self.gp_registered, self.concept_found = gp_registered, concept_found
        self.updates: list[tuple] = []
        self._next = None

    def execute(self, sql, params=()):
        if sql.startswith("SELECT concept_id"):
            self._next = (self.concept_id,) if self.concept_found else None
        elif sql.startswith("SELECT property_value"):
            self._next = (self.gp_value,) if self.gp_registered else None
        elif sql.startswith("UPDATE global_property"):
            self.updates.append(params)
            self._next = None

    def fetchone(self):
        return self._next


def _run(cursor):
    mod = _load_bootstrap_module()
    assert mod._take_notification_off_the_patient_overview(cursor) is True
    return cursor


def test_a_stack_that_ran_the_old_bootstrap_loses_the_entry():
    """The state every existing deployment is in: #123 left the concept as the only entry. The
    GP lives in the database, so it has to be removed, not merely no longer added."""
    cur = _run(FakeCursor(concept_id=42, gp_value="42"))
    assert cur.updates == [("", "dashboard.overview.showConcepts")]


def test_an_operators_other_concepts_are_preserved_in_order():
    """This GP is a LIST. Only our entry goes; clearing it would take an operator's own overview
    concepts with it."""
    cur = _run(FakeCursor(concept_id=42, gp_value="7, 42 ,9"))
    assert cur.updates == [("7,9", "dashboard.overview.showConcepts")]


def test_only_the_exact_concept_id_is_removed():
    """Ids are compared as whole entries, never as substrings: concept 42 must not take 142 or
    420 with it."""
    cur = _run(FakeCursor(concept_id=42, gp_value="142,42,420"))
    assert cur.updates == [("142,420", "dashboard.overview.showConcepts")]


def test_it_is_idempotent():
    """Runs on every stack start; once the entry is gone there is nothing left to write."""
    cur = _run(FakeCursor(concept_id=42, gp_value="7,9"))
    assert cur.updates == []


def test_an_unset_gp_is_left_alone():
    """The shipped state on a fresh stack: nothing listed, nothing to do."""
    cur = _run(FakeCursor(concept_id=42, gp_value=""))
    assert cur.updates == []


def test_a_missing_gp_is_not_a_startup_failure():
    cur = _run(FakeCursor(gp_registered=False))
    assert cur.updates == []


def test_a_missing_concept_is_a_warning_not_a_startup_failure():
    cur = _run(FakeCursor(concept_found=False))
    assert cur.updates == []


class FakeConnection:
    """Just enough of a pymysql connection to drive `bootstrap()` end to end."""

    def __init__(self, cursor):
        self._cursor, self.committed, self.closed = cursor, False, False

    def cursor(self):
        conn = self

        class _Ctx:
            def __enter__(self):
                return conn._cursor

            def __exit__(self, *exc):
                return False

        return _Ctx()

    def commit(self):
        self.committed = True

    def rollback(self):
        pass

    def close(self):
        self.closed = True


def test_a_real_bootstrap_run_removes_the_entry_and_commits():
    """The wiring, not just the function: `bootstrap()` has to call the removal and commit it.
    Driven through `bootstrap()` itself, because a test that only greps for the call's name is
    satisfied by the function's own `def` line."""
    mod = _load_bootstrap_module()
    assert not hasattr(mod, "_show_notification_on_the_patient_overview")

    cursor = FakeCursor(concept_id=42, gp_value="7,42")
    conn = FakeConnection(cursor)
    mod._connect_with_retry = lambda **kwargs: conn
    mod._CONCEPTS = []
    mod._configure_radiology_concept_classes = lambda cur: True

    assert mod.bootstrap(host="h", database="d", user="u", password="p") == 0
    assert cursor.updates == [("7", "dashboard.overview.showConcepts")]
    assert conn.committed and conn.closed


def test_the_gp_name_is_the_one_the_legacy_dashboard_reads():
    """Pin the property name: a typo here is silent, and the symptom is a widget that stays."""
    mod = _load_bootstrap_module()
    assert mod.OVERVIEW_SHOW_CONCEPTS_GP == "dashboard.overview.showConcepts"


def test_the_pinned_o3_image_has_the_overview_box():
    """The removal and the o3 pin move together. On an image without the module's Overview box the
    overview widget is the only surface, so this bootstrap would hide every critical-result
    notification from the chart. The pin moved to o3-048ef7d6 in !203, which has the box; a
    rollback to an older tag fails here instead of on a physician's screen."""
    tags = re.findall(r"lh-radiology/o3:(o3-[0-9a-f]+)", COMPOSE_PATH.read_text())
    assert tags, "no pinned o3 image found in docker-compose.yml"
    stale = sorted(set(tags) & O3_TAGS_WITHOUT_THE_PANEL)
    assert not stale, (
        f"docker-compose.yml pins {stale}, built before the module's notifications Overview box. "
        "Bump the pin to an o3 image that has it before removing the overview registration.")
