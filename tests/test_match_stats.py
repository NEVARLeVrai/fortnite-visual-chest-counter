from types import SimpleNamespace

from fortnite_chest_counter import discord_group, match_stats, overlay_layout, settings


def member(name, total, boost=1.0):
    return SimpleNamespace(name=name, certain=total, total=total, boost=boost)


def test_team_and_cumulative_count_each_session_once_and_preserve_solo():
    session = SimpleNamespace(players=(member("Alex", 10), member("Sam", 20, 2)))
    rows = [
        SimpleNamespace(snapshot="one", certain=10, total=10, boost=1, session_fingerprint="shared",
                        group_webhook_id="group", player="Alex"),
        SimpleNamespace(snapshot="two", certain=5, total=5, boost=1, session_fingerprint="solo",
                        group_webhook_id="", player="Alex"),
    ]
    team = match_stats.team(session, "alex", current=(11, 12, 1))
    assert team.member_totals == (("alex", 12), ("Sam", 20))
    assert sum(count for _, count in team.member_totals) == team.total
    sessions = {"shared": session}
    result = match_stats.cumulative(rows, sessions, "group", snapshot="one", total=10)
    assert result.total == 35
    expected = match_stats.personal(15, 15, 1) + match_stats.personal(20, 20, 2)
    assert result.chance == expected.chance
    # A completed match's still-visible counter contributes once.
    assert match_stats.cumulative(rows, sessions, "group", snapshot="two", total=5).total == 35
    # A new live game replaces its own delayed shared snapshot, then adds its teammate.
    live = match_stats.cumulative(rows, {"next": session, **sessions}, "group",
                                 snapshot="three", fingerprint="next", player="Alex",
                                 certain=12, total=12, boost=1, sharing=True)
    assert live.total == 67


def test_private_match_and_unverified_solo_have_no_team():
    solo = SimpleNamespace(players=(member("Alex", 10),))
    assert match_stats.team(solo, "Alex") is None
    assert match_stats.team(solo, "Sam") is None
    assert match_stats.team(None, "Alex") is None
    shared = SimpleNamespace(players=(member("Alex", 10), member("Sam", 20)))
    assert match_stats.team(shared, "Other") is None
    match = SimpleNamespace(session_fingerprint="x", group_webhook_id="", player="Alex")
    assert match_stats.saved_team(match, {"x": shared}, "group") is None


def test_mixed_boost_odds_agree_across_history_overlay_and_discord():
    players = (
        discord_group.SessionPlayer("Alex", 5, 10, 1.0, finished=True),
        discord_group.SessionPlayer("Sam", 12, 20, 2.0, finished=True),
    )
    session = discord_group.TeamSession("a" * 64, players)
    team = match_stats.team(session, "Alex")
    assert team is not None
    expected = 1 - (1 - 1 / 137_500) ** 10 * (1 - 2 / 137_500) ** 20
    assert team.chance == session.chance_percent == f"{expected:.6%}"
    assert (team.total, team.certain) == (30, 17)
    display = dict(settings.DEFAULTS["display"], use_custom_sections=True,
                   screen_team_enabled=True, screen_team_template="{total} | {chance}")
    context = {"team": {"certain": team.certain, "total": team.total,
                        "log_no_drop": team.log_no_drop}}
    assert overlay_layout.render_sections(display, context)[1] == f"30 | {team.chance}"

def test_capture_comparison_uses_live_types_and_excludes_unclassified():
    from dataclasses import replace
    from fortnite_chest_counter import match_stats
    from fortnite_chest_counter.match_history import Match

    match = Match("a", "2026-11-13 10:00", "p", "Default", "Duo", 8, 0, "0%",
                  chest_types={"chest": 8}, capture_config={"kind": "desktop-hdr", "hud_scale": 0.73, "process_fps": 15},
                  replay={"direct": {"chest_types": {"chest": 3, "unknown": 2, "unclassified": 3}}})
    older = replace(match, capture_config={}, replay={}, chest_types={"unclassified": 8})
    results = match_stats.capture_comparison([match, older])
    assert results == {"Screen · HDR → SDR · HUD 73% · 15 fps": [1, 5, 2], "Unknown configuration": [1, 0, 0]}
