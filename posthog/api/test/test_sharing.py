from datetime import timedelta
from unittest.mock import patch, Mock, MagicMock

from django.utils.timezone import now
from freezegun import freeze_time
from parameterized import parameterized
from rest_framework import status

from posthog.api.sharing import shared_url_as_png
from posthog.models import ExportedAsset, ActivityLog
from posthog.models.dashboard import Dashboard
from posthog.models.filters.filter import Filter
from posthog.models.insight import Insight
from posthog.models.sharing_configuration import SharingConfiguration
from posthog.models.user import User
from posthog.test.base import APIBaseTest
from django.utils import timezone


@parameterized.expand(
    [
        ["http://localhost:8000/something", "http://localhost:8000/something.png"],
        [
            "http://localhost:8000/something?query=string",
            "http://localhost:8000/something.png?query=string",
        ],
        [
            "http://localhost:8000/something?query=string&another=one",
            "http://localhost:8000/something.png?query=string&another=one",
        ],
        [
            "http://localhost:8000/something?query=string&another=one#withhash",
            "http://localhost:8000/something.png?query=string&another=one#withhash",
        ],
        [
            "http://localhost:8000/something#withhash",
            "http://localhost:8000/something.png#withhash",
        ],
    ]
)
def test_shared_image_alternative(url: str, expected_url: str) -> None:
    assert shared_url_as_png(url) == expected_url


class TestSharing(APIBaseTest):
    dashboard: Dashboard = None  # type: ignore
    insight: Insight = None  # type: ignore

    insight_filter_dict = {
        "events": [{"id": "$pageview"}],
        "properties": [{"key": "$browser", "value": "Mac OS X"}],
    }

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()

        cls.dashboard = Dashboard.objects.create(team=cls.team, name="example dashboard", created_by=cls.user)
        cls.insight = Insight.objects.create(
            filters=Filter(data=cls.insight_filter_dict).to_dict(),
            team=cls.team,
            created_by=cls.user,
        )

    @freeze_time("2022-01-01")
    @patch("posthog.api.exports.exporter.export_asset.delay")
    def test_gets_sharing_config(self, patched_exporter_task: Mock):
        assert SharingConfiguration.objects.count() == 0

        response = self.client.get(f"/api/projects/{self.team.id}/dashboards/{self.dashboard.id}/sharing")
        assert SharingConfiguration.objects.count() == 0
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data == {
            "access_token": data["access_token"],
            "created_at": None,
            "enabled": False,
        }

    @freeze_time("2022-01-01")
    @patch("posthog.api.exports.exporter.export_asset.delay")
    def test_does_not_change_token_when_toggling_enabled_state(self, patched_exporter_task: Mock):
        assert SharingConfiguration.objects.count() == 0
        response = self.client.patch(
            f"/api/projects/{self.team.id}/dashboards/{self.dashboard.id}/sharing",
            {"enabled": True},
        )
        initial_data = response.json()
        assert SharingConfiguration.objects.count() == 1
        response = self.client.get(f"/api/projects/{self.team.id}/dashboards/{self.dashboard.id}/sharing")
        assert response.json() == {
            "access_token": initial_data["access_token"],
            "created_at": "2022-01-01T00:00:00Z",
            "enabled": True,
        }

        response = self.client.patch(
            f"/api/projects/{self.team.id}/dashboards/{self.dashboard.id}/sharing",
            {"enabled": False},
        )
        assert response.json() == {
            "access_token": initial_data["access_token"],
            "created_at": "2022-01-01T00:00:00Z",
            "enabled": False,
        }

    @patch("posthog.api.exports.exporter.export_asset.delay")
    def test_can_edit_enabled_state(self, patched_exporter_task: Mock):
        response = self.client.patch(
            f"/api/projects/{self.team.id}/dashboards/{self.dashboard.id}/sharing",
            {"enabled": True},
        )
        data = response.json()
        assert response.status_code == status.HTTP_200_OK
        assert data["enabled"]

        response = self.client.get(f"/api/projects/{self.team.id}/dashboards/{self.dashboard.id}")

        assert response.json()["is_shared"]
        assert ActivityLog.objects.count() == 0

    @patch("posthog.api.exports.exporter.export_asset.delay")
    def test_can_edit_enabled_state_for_insight(self, patched_exporter_task: Mock):
        assert ActivityLog.objects.count() == 0

        response = self.client.patch(
            f"/api/projects/{self.team.id}/insights/{self.insight.id}/sharing",
            {"enabled": True},
        )
        data = response.json()
        assert response.status_code == status.HTTP_200_OK
        assert data["enabled"]

        response = self.client.patch(
            f"/api/projects/{self.team.id}/insights/{self.insight.id}/sharing",
            {"enabled": False},
        )
        data = response.json()
        assert response.status_code == status.HTTP_200_OK
        assert data["enabled"] is False

        assert [x.activity for x in list(ActivityLog.objects.order_by("created_at"))] == [
            "sharing enabled",
            "exported for opengraph image",
            "sharing disabled",
        ]

    @patch("posthog.api.exports.exporter.export_asset.delay")
    def test_exports_image_when_sharing(self, patched_exporter_task: Mock):
        assert ExportedAsset.objects.count() == 0

        self.client.patch(
            f"/api/projects/{self.team.id}/dashboards/{self.dashboard.id}/sharing",
            {"enabled": True},
        )

        assert ExportedAsset.objects.count() == 1
        asset = ExportedAsset.objects.first()
        assert asset is not None
        assert asset.export_format == "image/png"

    @patch("posthog.api.exports.exporter.export_asset.delay")
    def test_should_update_to_match_existing_dashboard_sharing_token(self, patched_exporter_task: Mock):
        dashboard = Dashboard.objects.create(team=self.team, name="example dashboard", created_by=self.user)
        response = self.client.get(f"/api/projects/{self.team.id}/dashboards/{dashboard.id}/sharing")
        initial_token = response.json()["access_token"]
        assert initial_token
        assert not response.json()["enabled"]

        dashboard.share_token = "my_test_token"
        dashboard.is_shared = True
        dashboard.save()

        response = self.client.get(f"/api/projects/{self.team.id}/dashboards/{dashboard.id}/sharing")
        data = response.json()
        assert data["access_token"] == "my_test_token"
        assert data["enabled"]

        dashboard.share_token = None
        dashboard.is_shared = False
        dashboard.save()

        response = self.client.get(f"/api/projects/{self.team.id}/dashboards/{dashboard.id}/sharing")
        data = response.json()
        assert data["access_token"] == "my_test_token"
        assert data["enabled"]

    @patch("posthog.api.exports.exporter.export_asset.delay")
    def test_should_not_be_affected_by_collaboration_rules(self, _patched_exporter_task: Mock):
        other_user = User.objects.create_and_join(self.organization, "a@x.com", None)
        dashboard = Dashboard.objects.create(
            team=self.team,
            name="example dashboard",
            created_by=other_user,
            restriction_level=Dashboard.RestrictionLevel.ONLY_COLLABORATORS_CAN_EDIT,
        )

        response = self.client.patch(
            f"/api/projects/{self.team.id}/dashboards/{dashboard.id}/sharing",
            {"enabled": True},
        )

        assert response.status_code == 200

    @patch("posthog.api.exports.exporter.export_asset.delay")
    def test_should_not_get_deleted_item(self, _patched_exporter_task: Mock):
        dashboard = Dashboard.objects.create(
            team=self.team,
            name="example dashboard",
            created_by=self.user,
            share_token="my_test_token",
            is_shared=True,
        )
        response = self.client.patch(
            f"/api/projects/{self.team.id}/dashboards/{dashboard.id}/sharing",
            {"enabled": True},
        )
        response = self.client.get(f"/shared_dashboard/my_test_token")
        assert response.status_code == 200
        response = self.client.patch(f"/api/projects/{self.team.id}/dashboards/{dashboard.id}", {"deleted": True})
        assert response.status_code == 200
        response = self.client.get(f"/shared_dashboard/my_test_token")
        assert response.status_code == 404

    @parameterized.expand(
        [
            "/exporter/something.png?token=my_test_token",
            "/shared_dashboard/something.png?token=my_test_token",
        ]
    )
    @patch("posthog.api.exports.exporter.export_asset.delay")
    @patch("posthog.models.exported_asset.object_storage.read_bytes")
    @patch("posthog.api.sharing.asset_for_token")
    def test_can_get_shared_dashboard_asset_with_no_content_but_content_location(
        self,
        url: str,
        patched_asset_for_token,
        patched_object_storage,
        _patched_exporter_task: Mock,
    ) -> None:
        asset = ExportedAsset.objects.create(
            team_id=self.team.id,
            export_format=ExportedAsset.ExportFormat.PNG,
            content=None,
            content_location="some object url",
        )
        patched_asset_for_token.return_value = asset

        patched_object_storage.return_value = b"the image bytes"

        response = self.client.get(url)

        assert response.status_code == 200
        assert response.headers.get("Content-Type") == "image/png"
        assert response.content == b"the image bytes"

    @parameterized.expand(["insights", "dashboards"])
    @patch("posthog.models.exported_asset.object_storage.read_bytes")
    @patch("posthog.api.exports.exporter.export_asset.delay")
    def test_shared_thing_can_generate_open_graph_image(
        self, type: str, patched_exporter_task: Mock, patched_object_storage: Mock
    ) -> None:
        patched_object_storage.return_value = b"the image bytes"

        target = self.insight if type == "insights" else self.dashboard

        self._setup_patched_exporter(patched_exporter_task)

        assert ExportedAsset.objects.count() == 0

        share_response = self.client.patch(
            f"/api/projects/{self.team.id}/{type}/{target.pk}/sharing",
            {"enabled": True},
        )
        access_token = share_response.json()["access_token"]

        item_opengraph_image = self.client.get("/shared/" + access_token + ".png")

        assert ExportedAsset.objects.count() == 1
        assert item_opengraph_image.status_code == 200
        assert item_opengraph_image.headers["Content-Type"] == "image/png"
        assert item_opengraph_image.content == b"the image bytes"

    @parameterized.expand(["insights", "dashboards"])
    @patch("posthog.models.exported_asset.object_storage.read_bytes")
    @patch("posthog.api.exports.exporter.export_asset.delay")
    def test_shared_thing_can_reuse_existing_generated_open_graph_image(
        self, type: str, patched_exporter_task: Mock, patched_object_storage: Mock
    ) -> None:
        patched_object_storage.return_value = b"the image bytes"

        self._setup_patched_exporter(patched_exporter_task)

        target = self.insight if type == "insights" else self.dashboard

        share_response = self.client.patch(
            f"/api/projects/{self.team.id}/{type}/{target.pk}/sharing",
            {"enabled": True},
        )
        access_token = share_response.json()["access_token"]

        # generation was called when sharing was enabled
        patched_exporter_task.reset_mock()

        item_opengraph_image = self.client.get("/shared/" + access_token + ".png")

        # and not again on loading the image
        patched_exporter_task.assert_not_called()

        assert ExportedAsset.objects.count() == 1
        assert item_opengraph_image.status_code == 200
        assert item_opengraph_image.headers["Content-Type"] == "image/png"
        assert item_opengraph_image.content == b"the image bytes"

    def _setup_patched_exporter(self, patched_exporter_task):
        def add_content_location_on_task_run(*args, **kwargs):
            asset = ExportedAsset.objects.get(team_id=self.team.id)
            asset.content_location = "some object url"
            asset.save()

            return MagicMock()

        patched_exporter_task.side_effect = add_content_location_on_task_run

    @parameterized.expand(["insights", "dashboards"])
    @patch("posthog.models.exported_asset.object_storage.read_bytes")
    @patch("posthog.api.exports.exporter.export_asset.delay")
    def test_shared_insight_can_regenerate_stale_existing_generated_open_graph_image(
        self, type: str, patched_exporter_task: Mock, patched_object_storage: Mock
    ) -> None:
        patched_object_storage.return_value = b"the image bytes"
        self._setup_patched_exporter(patched_exporter_task)

        target = self.insight if type == "insights" else self.dashboard

        # the existing asset is stale because it is more than 3 hours old
        time_in_the_past = now() - timedelta(hours=4)
        with freeze_time(time_in_the_past):
            share_response = self.client.patch(
                f"/api/projects/{self.team.id}/{type}/{target.pk}/sharing",
                {"enabled": True},
            )
            # enabling creates an asset
            assert ExportedAsset.objects.count() == 1
            original_asset = ExportedAsset.objects.first()

        access_token = share_response.json()["access_token"]

        # times passes and the asset is stale
        assert ExportedAsset.objects.count() == 0

        item_opengraph_image = self.client.get("/shared/" + access_token + ".png")
        assert item_opengraph_image.status_code == 200
        assert item_opengraph_image.headers["Content-Type"] == "image/png"
        assert item_opengraph_image.content == b"the image bytes"

        assert ExportedAsset.objects.count() == 1
        final_asset = ExportedAsset.objects.first()
        assert final_asset is not None
        assert original_asset is not None
        assert final_asset.id != original_asset.id

    @patch("posthog.api.exports.exporter.export_asset.delay")
    def test_can_refresh_sharing_access_token_for_dashboard(self, patched_exporter_task: Mock):
        # Enable sharing
        response = self.client.patch(
            f"/api/projects/{self.team.id}/dashboards/{self.dashboard.id}/sharing",
            {"enabled": True},
        )
        initial_data = response.json()
        initial_token = initial_data["access_token"]
        assert initial_token

        # Refresh the token
        response = self.client.post(f"/api/projects/{self.team.id}/dashboards/{self.dashboard.id}/sharing/refresh/")
        assert response.status_code == status.HTTP_200_OK
        refreshed_data = response.json()

        # Token should be different
        assert refreshed_data["access_token"] != initial_token
        assert refreshed_data["enabled"] is True

        # Verify the token persists
        response = self.client.get(f"/api/projects/{self.team.id}/dashboards/{self.dashboard.id}/sharing")
        assert response.json()["access_token"] == refreshed_data["access_token"]

    @patch("posthog.api.exports.exporter.export_asset.delay")
    def test_can_refresh_sharing_access_token_for_insight(self, patched_exporter_task: Mock):
        # First enable sharing
        response = self.client.patch(
            f"/api/projects/{self.team.id}/insights/{self.insight.id}/sharing",
            {"enabled": True},
        )
        initial_data = response.json()
        initial_token = initial_data["access_token"]
        assert initial_token

        # Refresh the token
        response = self.client.post(f"/api/projects/{self.team.id}/insights/{self.insight.id}/sharing/refresh/")
        assert response.status_code == status.HTTP_200_OK
        refreshed_data = response.json()

        # Token should be different
        assert refreshed_data["access_token"] != initial_token
        assert refreshed_data["enabled"] is True

        # Verify activity log was created
        activity_logs = ActivityLog.objects.filter(activity="access token refreshed")
        assert activity_logs.count() == 1
        first = activity_logs.first()
        assert first is not None
        assert first.item_id == str(self.insight.id)

    @freeze_time("2025-01-01 00:00:00")
    @patch("posthog.api.exports.exporter.export_asset.delay")
    def test_refresh_token_grace_period(self, patched_exporter_task: Mock):
        # Enable sharing
        response = self.client.patch(
            f"/api/projects/{self.team.id}/dashboards/{self.dashboard.id}/sharing",
            {"enabled": True},
        )
        initial_token = response.json()["access_token"]

        # Refresh the token
        response = self.client.post(f"/api/projects/{self.team.id}/dashboards/{self.dashboard.id}/sharing/refresh/")
        assert response.status_code == status.HTTP_200_OK
        new_token = response.json()["access_token"]
        assert new_token != initial_token

        # Old token should still work immediately after refresh
        response = self.client.get(f"/shared/{initial_token}")
        assert response.status_code == 200

        # New token should also work
        response = self.client.get(f"/shared/{new_token}")
        assert response.status_code == 200

        # Within grace period (4 minutes later), old token should still work
        # Note: Grace period is 5 minutes (SHARING_TOKEN_GRACE_PERIOD_SECONDS)
        with freeze_time("2025-01-01 00:04:00"):
            response = self.client.get(f"/shared/{initial_token}")
            assert response.status_code == 200

        # After grace period (6 minutes later), old token should not work
        with freeze_time("2025-01-01 00:06:00"):
            response = self.client.get(f"/shared/{initial_token}")
            assert response.status_code == 404

        # New token should still work after grace period
        with freeze_time("2025-01-01 00:06:00"):
            response = self.client.get(f"/shared/{new_token}")
            assert response.status_code == 200

    def test_token_uniqueness_constraints(self):
        """Test that token uniqueness is enforced at the database level"""
        from posthog.models.sharing_configuration import SharingConfiguration

        # Create first sharing configuration with a specific token
        config1 = SharingConfiguration.objects.create(
            team=self.team,
            dashboard=self.dashboard,
            enabled=True,
        )
        # Token should be auto-generated
        assert config1.access_token is not None
        original_token = config1.access_token

        # Try to manually set another config with the same token - should fail due to DB constraint
        config2 = SharingConfiguration(
            team=self.team,
            insight=self.insight,
            enabled=True,
            access_token=original_token,  # Duplicate token
        )

        # This should raise IntegrityError due to unique constraint
        from django.db import IntegrityError

        with self.assertRaises(IntegrityError):
            config2.save()

    def test_token_rotation_creates_new_config(self):
        """Test that token rotation creates a new configuration and expires the old one"""
        from posthog.models.sharing_configuration import SharingConfiguration

        # Enable sharing
        self.client.patch(
            f"/api/projects/{self.team.id}/dashboards/{self.dashboard.id}/sharing",
            {"enabled": True},
        )
        original_config = SharingConfiguration.objects.get(dashboard=self.dashboard, expires_at__isnull=True)

        # Refresh the token
        response = self.client.post(f"/api/projects/{self.team.id}/dashboards/{self.dashboard.id}/sharing/refresh/")
        assert response.status_code == status.HTTP_200_OK
        new_token = response.json()["access_token"]

        # Should have created a new config
        new_config = SharingConfiguration.objects.get(dashboard=self.dashboard, expires_at__isnull=True)
        assert new_config.access_token == new_token
        assert new_config.pk != original_config.pk

        # Old config should be expired
        original_config.refresh_from_db()
        assert original_config.expires_at is not None
        assert original_config.expires_at > timezone.now()  # Should be in the future
