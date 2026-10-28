"""``_same_repo`` must treat the two spellings of a Windows host path as one."""

import projects

BACK = chr(92)


def _win(path: str) -> str:
    return path.replace("/", BACK)


def test_forward_and_back_slashes_are_the_same_repo():
    assert projects._same_repo("X:/nodes/Alpha", _win("X:/nodes/Alpha"))


def test_drive_letter_case_and_trailing_separator_are_ignored():
    assert projects._same_repo("x:/nodes/Alpha/", _win("X:/nodes/alpha"))


def test_different_repos_stay_different():
    assert not projects._same_repo("X:/nodes/Alpha", _win("X:/nodes/Beta"))
    assert not projects._same_repo("Y:/nodes/Gamma", _win("X:/nodes/Gamma"))


def test_empty_path_matches_nothing():
    assert not projects._same_repo("", "")
    assert not projects._same_repo("X:/nodes/Alpha", "")
