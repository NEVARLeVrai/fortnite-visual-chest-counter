from copy import deepcopy
import json
import math

from fortnite_chest_counter import counter, overlay_layout, settings


def summary(certain, total, *, boost=1.0, matches=1, players=1):
    return {"certain": certain, "total": total, "log_no_drop": total * math.log1p(-boost / 137_500),
            "boost": boost, "matches": matches, "players": players, "names": "Alex · Sam",
            "types": "Chest types · Rare chest 2"}


def context():
    return {"version": 1, "state": {"certain": 3, "approximate": 5, "session_started_at": "first"},
            "personal": summary(3, 5), "team": summary(5, 9, players=2),
            "cumulative": summary(12, 20, matches=4), "current_saved": False, "active": False}


def display():
    result = deepcopy(settings.DEFAULTS["display"])
    result.update(use_custom_sections=True, screen_show_matches=True, screen_show_types=True, screen_show_token=True)
    return result


def test_same_context_renders_identically_in_obs_and_graphical_sections(tmp_path):
    value = context()
    value["profile_path"] = str(tmp_path / "state.json")
    value["personal"]["names"] = "Alex"
    value["team"]["member_totals"] = [("Alex", 5), ("Sam", 4)]
    (tmp_path / "overlay_context.json").write_text(json.dumps(value), encoding="utf-8")
    state = {**value["state"], "approximate": 7}
    advanced = overlay_layout.reconcile_context(value, state)
    sections = overlay_layout.render_sections(display(), advanced)
    assert sections[1].splitlines()[:3] == ["Chests · 11", "Alex · 7", "Sam · 4"]
    styled = overlay_layout.render_sections(display(), advanced, styled=True)
    assert styled[1].startswith("<b>Chests · 11</b><br>Alex · 7<br>Sam · 4")
    expected = overlay_layout.format_text(display(), sections)
    counter.write_obs_files(state, tmp_path, display(), state_path=value["profile_path"])
    assert (tmp_path / "chest_counter.txt").read_text(encoding="utf-8") == expected + "\n"
    assert (tmp_path / "approximate_chests.txt").read_text().endswith("7\n")


def test_worker_delta_updates_each_section_once_with_personal_boost():
    original = context()
    original["personal"] = summary(3, 5, boost=2.0)
    untouched = deepcopy(original)
    state = {"certain": 4, "approximate": 7, "session_started_at": "first"}
    advanced = overlay_layout.reconcile_context(original, state)
    rate = math.log1p(-2 / 137_500)
    assert original == untouched
    for scope, before in (("personal", 5), ("team", 9), ("cumulative", 20)):
        assert advanced[scope]["total"] == before + 2
        assert math.isclose(advanced[scope]["log_no_drop"], original[scope]["log_no_drop"] + 2 * rate)
    assert advanced["personal"]["certain"] == 4
    assert advanced["team"]["certain"] == 6
    assert advanced["cumulative"]["certain"] == 13
    assert overlay_layout.reconcile_context(advanced, state) == advanced


def test_new_match_keeps_saved_history_and_discards_old_team():
    original = context()
    original["current_saved"] = True
    state = {"certain": 1, "approximate": 3, "session_started_at": "second"}
    advanced = overlay_layout.reconcile_context(original, state)
    assert advanced["team"] is None
    assert advanced["personal"]["total"] == 3
    assert advanced["cumulative"]["total"] == 23
    assert advanced["cumulative"]["matches"] == 5
    assert not advanced["current_saved"]


def test_new_match_removes_unsaved_current_counts_before_adding_new_counts():
    original = context()
    state = {"certain": 1, "approximate": 3, "session_started_at": "second"}
    advanced = overlay_layout.reconcile_context(original, state)
    assert advanced["team"] is None
    assert advanced["cumulative"]["total"] == 14
    assert advanced["cumulative"]["certain"] == 8
    assert math.isclose(advanced["cumulative"]["log_no_drop"], summary(8, 14)["log_no_drop"])
    assert advanced["cumulative"]["matches"] == 4
    state = {"certain": 0, "approximate": 0, "session_started_at": "second"}
    empty = overlay_layout.reconcile_context(original, state)
    assert empty["cumulative"]["total"] == 11 and empty["cumulative"]["matches"] == 3


def test_saved_match_worker_delta_does_not_rewrite_cumulative_totals():
    original = context()
    original["current_saved"] = True
    state = {"certain": 4, "approximate": 7, "session_started_at": "first"}
    advanced = overlay_layout.reconcile_context(original, state)
    assert advanced["personal"]["total"] == 7
    for key in ("total", "certain", "log_no_drop", "matches"):
        assert advanced["cumulative"][key] == original["cumulative"][key]


def test_first_chest_adds_current_match_count_once():
    original = context()
    original["state"].update(certain=0, approximate=0)
    original["personal"] = summary(0, 0, matches=0)
    original["cumulative"] = summary(2, 4, matches=2)
    state = {"certain": 1, "approximate": 1, "session_started_at": "first"}
    advanced = overlay_layout.reconcile_context(original, state)
    assert advanced["personal"]["matches"] == 1 and advanced["cumulative"]["matches"] == 3
    assert overlay_layout.reconcile_context(advanced, state)["cumulative"]["matches"] == 3


def test_templates_respect_scope_and_optional_token_controls():
    value = context()
    value["team"] = None
    options = display()
    options.update(screen_personal_template="**{total}** chests\n\nChance {chance}\nGames {matches}\n{types}\n{name_unknown}",
                   screen_cumulative_enabled=False, screen_show_token=False, screen_show_matches=False, screen_show_types=False)
    assert overlay_layout.render_sections(options, value) == ("5 chests\n\n{name_unknown}", "", "")


def test_wrong_profile_or_invalid_context_falls_back_to_legacy_obs_output(tmp_path):
    options = display()
    value = context()
    value["profile_path"] = str(tmp_path / "other.json")
    file = tmp_path / "overlay_context.json"
    file.write_text(json.dumps(value), encoding="utf-8")
    counter.write_obs_files(value["state"], tmp_path, options, state_path=tmp_path / "selected.json")
    assert (tmp_path / "chest_counter.txt").read_text(encoding="utf-8") == counter.overlay_text(options, value["state"]) + "\n"
    file.write_text("{broken", encoding="utf-8")
    assert counter.read_overlay_context(tmp_path, value["state"]) is None
    assert len(overlay_layout.render_sections(options, {})) == 3


def test_team_status_and_member_age_are_rendered_and_discarded_on_new_match():
    value = context()
    value["team_status"] = "Discord sync paused. Retrying automatically."
    value["team_member_status"] = {"alex": "Local", "sam": "Last update 45s ago"}
    value["team"]["member_totals"] = [("Alex", 5), ("Sam", 4)]
    team = overlay_layout.render_sections(display(), value)[1]
    assert "Sam · 4 · Last update 45s ago" in team
    assert team.endswith(value["team_status"])
    value["team"] = None
    assert overlay_layout.render_sections(display(), value)[1] == value["team_status"]
    assert not overlay_layout.render_sections({**display(), "screen_team_enabled": False}, value)[1]
    advanced = overlay_layout.reconcile_context(value, {"session_started_at": "new", "certain": 0, "approximate": 0})
    assert not overlay_layout.render_sections(display(), advanced)[1]
