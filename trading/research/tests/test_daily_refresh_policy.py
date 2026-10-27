import pytest

from trading.research import daily_refresh_policy as policy


def test_materializes_existing_committee_route_without_changing_argv(tmp_path):
    command = ["leader-entry-comparison", "publish", "--registration", "saved.json"]
    result = policy.command(command, root=tmp_path)
    assert result == [str(tmp_path / "env/python.exe"), "-B", "-X", "utf8",
        str(tmp_path / "run_committee.py"), *command]


def test_materializes_runtime_repair_as_its_registered_module(tmp_path):
    result = policy.command(["leader-type-router-runtime-repair", "forward"], root=tmp_path)
    assert result == [str(tmp_path / "env/python.exe"), "-B", "-X", "utf8", "-m",
        "trading.research.leader_type_router_runtime_repair", "forward"]


@pytest.mark.parametrize("command", [
    ["leader-entry-comparison", "run"],
    ["seat-daily", "register"],
    ["leader-type-router-forward", "publish"],
])
def test_route_policy_rejects_unregistered_subcommands(command, tmp_path):
    with pytest.raises(ValueError):
        policy.command(command, root=tmp_path)


def test_seat_collect_keeps_the_fixed_three_page_exact_day_bound(tmp_path):
    valid = ["seat-collect", "--execute", "--max-pages", "3", "--start", "2026-10-26",
        "--end", "2026-10-26"]
    policy.command(valid, root=tmp_path)
    with pytest.raises(ValueError):
        policy.command([*valid[:-1], "2026-10-27"], root=tmp_path)


def test_forbidden_training_or_live_flags_are_rejected(tmp_path):
    for flag in ("--save-next", "--refit", "--train", "--submit", "--live"):
        with pytest.raises(ValueError):
            policy.command(["leader-environment", flag], root=tmp_path)

