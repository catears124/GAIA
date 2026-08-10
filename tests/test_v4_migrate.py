from gaia.v4_migrate import sanitize_previous_snapshot


def test_legacy_unverified_family_is_not_carried_into_v4_as_direct():
    previous = {
        "schema_version": 3,
        "family_index": [
            {
                "family_key": "lead",
                "company": "Lead Co",
                "title": "Software Engineering Intern",
                "verified": False,
                "direct_openings": 0,
                "openings": [
                    {
                        "apply_url": "https://example.test/lead",
                        "source": "old-index",
                    }
                ],
            },
            {
                "family_key": "verified",
                "company": "Verified Co",
                "title": "Software Engineering Intern",
                "verified": True,
                "direct_openings": 1,
                "openings": [
                    {
                        "apply_url": "https://example.test/verified",
                        "source": "greenhouse:verified",
                    }
                ],
            },
        ],
    }
    migrated, changed = sanitize_previous_snapshot(previous)
    assert changed is True
    assert [family["family_key"] for family in migrated["family_index"]] == ["verified"]
    assert migrated["family_index"][0]["openings"][0]["source_mode"] == "direct"


def test_v4_snapshot_is_left_untouched():
    snapshot = {"schema_version": 4, "family_index": [{"family_key": "x"}]}
    migrated, changed = sanitize_previous_snapshot(snapshot)
    assert changed is False
    assert migrated is snapshot
