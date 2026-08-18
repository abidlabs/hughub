from hughub.assets.dispatcher import event_targets, matches


def test_repo_updates_route_branches_and_pr_refs():
    payload = {
        "event": {"scope": "repo.content", "action": "update"},
        "updatedRefs": [
            {"ref": "refs/heads/main", "newSha": "aaa"},
            {"ref": "refs/pr/7", "newSha": "bbb"},
            {"ref": "refs/tags/v1", "newSha": "ccc"},
            {"ref": "refs/heads/old", "newSha": None},
        ],
    }

    assert event_targets(payload) == [
        ("push", "refs/heads/main", "aaa"),
        ("pull_request", "refs/pr/7", "bbb"),
    ]


def test_push_branch_filters_match_github_shape():
    workflow = {
        "on": {"push": {"branches": ["main", "release/*"], "branches-ignore": ["release/old"]}}
    }

    assert matches(workflow, "push", "refs/heads/main")
    assert matches(workflow, "push", "refs/heads/release/2")
    assert not matches(workflow, "push", "refs/heads/release/old")
    assert not matches(workflow, "push", "refs/heads/feature")
