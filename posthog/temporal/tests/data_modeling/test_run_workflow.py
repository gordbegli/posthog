import asyncio
import datetime as dt
import functools
import unittest.mock
import uuid
import os

import aioboto3
import deltalake
import pytest
import pytest_asyncio
from asgiref.sync import sync_to_async
import temporalio.common
import temporalio.worker
from django.conf import settings
from django.test import override_settings
from freezegun.api import freeze_time
import pyarrow as pa
from unittest.mock import patch

from posthog import constants
from posthog.hogql.database.database import create_hogql_database
from posthog.hogql.query import execute_hogql_query
from posthog.models import Team
from posthog.warehouse.models.data_modeling_job import DataModelingJob
from posthog.temporal.data_modeling.run_workflow import (
    BuildDagActivityInputs,
    CreateTableActivityInputs,
    ModelNode,
    RunDagActivityInputs,
    RunWorkflow,
    RunWorkflowInputs,
    Selector,
    build_dag_activity,
    create_table_activity,
    finish_run_activity,
    materialize_model,
    run_dag_activity,
    start_run_activity,
    create_job_model_activity,
    fail_jobs_activity,
    cleanup_running_jobs_activity,
    CleanupRunningJobsActivityInputs,
    CreateJobModelInputs,
)
from posthog.temporal.tests.utils.events import generate_test_events_in_clickhouse
from posthog.warehouse.models.datawarehouse_saved_query import DataWarehouseSavedQuery
from posthog.warehouse.models.modeling import DataWarehouseModelPath
from posthog.warehouse.models.table import DataWarehouseTable
from posthog.sync import database_sync_to_async

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db]

TEST_TIME = dt.datetime.now(dt.UTC)


@pytest_asyncio.fixture
async def posthog_tables(ateam):
    team = await database_sync_to_async(Team.objects.get)(id=ateam.pk)
    hogql_db = await database_sync_to_async(create_hogql_database)(team=team)
    posthog_tables = hogql_db.get_posthog_tables()

    return posthog_tables


@pytest.mark.parametrize(
    "dag",
    [
        {
            "events": ModelNode(label="events", children={"my_events_model"}),
            "persons": ModelNode(label="persons", children={"my_persons_model"}),
            "my_events_model": ModelNode(
                label="my_events_model", children={"my_joined_model"}, parents={"events"}, selected=True
            ),
            "my_persons_model": ModelNode(
                label="my_persons_model", children={"my_joined_model"}, parents={"persons"}, selected=True
            ),
            "my_joined_model": ModelNode(
                label="my_joined_model", parents={"my_events_model", "my_persons_model"}, selected=True
            ),
        },
    ],
)
async def test_run_dag_activity_activity_materialize_mocked(activity_environment, ateam, dag, posthog_tables):
    """Test all models are completed with a mocked materialize."""
    for model_label in dag.keys():
        if model_label not in posthog_tables:
            await database_sync_to_async(DataWarehouseSavedQuery.objects.create)(
                team=ateam,
                name=model_label,
                query={"query": f"SELECT * FROM events LIMIT 10", "kind": "HogQLQuery"},
            )

    job = await database_sync_to_async(DataModelingJob.objects.create)(
        team=ateam,
    )
    run_dag_activity_inputs = RunDagActivityInputs(team_id=ateam.pk, dag=dag, job_id=job.id)

    magic_mock = unittest.mock.AsyncMock(return_value=("test_key", unittest.mock.MagicMock(), uuid.uuid4()))

    with unittest.mock.patch("posthog.temporal.data_modeling.run_workflow.materialize_model", new=magic_mock):
        async with asyncio.timeout(10):
            results = await activity_environment.run(run_dag_activity, run_dag_activity_inputs)

        models_materialized = [model for model in dag.keys() if model not in posthog_tables]

    calls = magic_mock.mock_calls

    assert all(
        call.args[0] in models_materialized for call in calls
    ), f"Found models that shouldn't have been materialized: {tuple(call.args[0] for call in calls if call.args[0] not in models_materialized)}"
    assert all(
        call.args[1].pk == ateam.pk for call in calls
    ), f"Found team ids that do not match test team ({ateam.pk}): {tuple(call.args[1].pk for call in calls)}"
    assert len(calls) == len(models_materialized)
    assert results.completed == set(dag.keys())


async def test_create_table_activity(minio_client, activity_environment, ateam, bucket_name):
    query = """\
    select
      event as event,
      if(distinct_id != '0', distinct_id, null) as distinct_id,
      timestamp as timestamp
    from events
    where event = '$pageview'
    """
    saved_query = await DataWarehouseSavedQuery.objects.acreate(
        team=ateam,
        name="my_model",
        query={"query": query, "kind": "HogQLQuery"},
    )

    create_table_activity_inputs = CreateTableActivityInputs(team_id=ateam.pk, models=[saved_query.id.hex])
    with (
        override_settings(
            BUCKET_URL=f"s3://{bucket_name}",
            AIRBYTE_BUCKET_KEY=settings.OBJECT_STORAGE_ACCESS_KEY_ID,
            AIRBYTE_BUCKET_SECRET=settings.OBJECT_STORAGE_SECRET_ACCESS_KEY,
            AIRBYTE_BUCKET_REGION="us-east-1",
            AIRBYTE_BUCKET_DOMAIN="objectstorage:19000",
        ),
        unittest.mock.patch(
            "posthog.warehouse.models.table.DataWarehouseTable.get_columns",
            return_value={
                "id": {"clickhouse": "String", "hogql": "StringDatabaseField", "valid": True},
                "a_column": {"clickhouse": "String", "hogql": "StringDatabaseField", "valid": True},
            },
        ),
        # this mock is needed, otherwise the test takes ~30s b/c of the sync behavior of get_count
        unittest.mock.patch("posthog.warehouse.models.table.DataWarehouseTable.get_count", return_value=42),
    ):
        async with asyncio.timeout(10):
            await activity_environment.run(create_table_activity, create_table_activity_inputs)

    table = await DataWarehouseTable.objects.aget(team_id=ateam.pk, name=saved_query.name)
    assert table.name == saved_query.name
    assert saved_query.normalized_name in table.url_pattern


@pytest.mark.parametrize(
    "dag,make_fail",
    [
        (
            {
                "events": ModelNode(label="events", children={"my_events_model"}),
                "persons": ModelNode(label="persons", children={"my_persons_model"}),
                "my_events_model": ModelNode(
                    label="my_events_model", children={"my_joined_model"}, parents={"events"}, selected=True
                ),
                "my_persons_model": ModelNode(
                    label="my_persons_model", children={"my_joined_model"}, parents={"persons"}, selected=True
                ),
                "my_joined_model": ModelNode(
                    label="my_joined_model",
                    children={"my_read_from_joined_model"},
                    parents={"my_events_model", "my_persons_model"},
                    selected=True,
                ),
                "my_read_from_joined_model": ModelNode(
                    label="my_read_from_joined_model", parents={"my_joined_model"}, selected=True
                ),
            },
            ("my_events_model",),
        ),
    ],
)
async def test_run_dag_activity_activity_skips_if_ancestor_failed_mocked(
    activity_environment, ateam, dag, make_fail, posthog_tables
):
    """Test some models are completed while some fail with a mocked materialize.

    Args:
        dag: The dictionary of `ModelNode`s representing the model DAG.
        make_fail: A sequence of model labels of models that should fail to check they are
            handled properly.
    """
    # Create the necessary saved queries for the test
    for model_label in dag.keys():
        if model_label not in posthog_tables:
            await database_sync_to_async(DataWarehouseSavedQuery.objects.create)(
                team=ateam,
                name=model_label,
                query={"query": f"SELECT * FROM events LIMIT 10", "kind": "HogQLQuery"},
            )

    job = await database_sync_to_async(DataModelingJob.objects.create)(
        team=ateam,
    )
    run_dag_activity_inputs = RunDagActivityInputs(team_id=ateam.pk, dag=dag, job_id=job.id)
    assert all(model not in posthog_tables for model in make_fail), "PostHog tables cannot fail"

    def raise_if_should_make_fail(model_label, *args, **kwargs):
        if model_label in make_fail:
            raise ValueError("Oh no!")
        return ("test_key", unittest.mock.MagicMock(), uuid.uuid4())

    expected_failed = set()
    expected_ancestor_failed = set()

    for model in make_fail:
        expected_failed.add(model)

        children_to_fail = list(dag[model].children)
        while children_to_fail:
            child = children_to_fail.pop()
            expected_ancestor_failed.add(child)

            children_to_fail.extend(list(dag[child].children))

    expected_completed = {
        key for key in dag.keys() if key not in expected_failed and key not in expected_ancestor_failed
    }

    magic_mock = unittest.mock.AsyncMock(side_effect=raise_if_should_make_fail)
    with unittest.mock.patch("posthog.temporal.data_modeling.run_workflow.materialize_model", new=magic_mock):
        async with asyncio.timeout(10):
            results = await activity_environment.run(run_dag_activity, run_dag_activity_inputs)

        models_materialized = [model for model in expected_failed | expected_completed if model not in posthog_tables]

    calls = magic_mock.mock_calls

    assert all(
        call.args[0] in models_materialized for call in calls
    ), f"Found models that shouldn't have been materialized: {tuple(call.args[0] for call in calls if call.args[0] not in models_materialized)}"
    assert all(
        call.args[1].pk == ateam.pk for call in calls
    ), f"Found team ids that do not match test team ({ateam.pk}): {tuple(call.args[1].pk for call in calls)}"
    assert len(calls) == len(models_materialized)

    assert results.completed == expected_completed
    assert results.failed == expected_failed
    assert results.ancestor_failed == expected_ancestor_failed


TEST_ROOT_BUCKET = "test-data-modeling"
SESSION = aioboto3.Session()
create_test_client = functools.partial(SESSION.client, endpoint_url=settings.OBJECT_STORAGE_ENDPOINT)


@pytest.fixture
def bucket_name(request) -> str:
    """Name for a test S3 bucket."""
    try:
        return request.param
    except AttributeError:
        return f"{TEST_ROOT_BUCKET}-{str(uuid.uuid4())}"


@pytest_asyncio.fixture
async def minio_client(bucket_name):
    """Manage an S3 client to interact with a MinIO bucket.

    Yields the client after creating a bucket. Upon resuming, we delete
    the contents and the bucket itself.
    """
    async with create_test_client(
        "s3",
        aws_access_key_id=settings.OBJECT_STORAGE_ACCESS_KEY_ID,
        aws_secret_access_key=settings.OBJECT_STORAGE_SECRET_ACCESS_KEY,
    ) as minio_client:
        try:
            await minio_client.head_bucket(Bucket=bucket_name)
        except:
            await minio_client.create_bucket(Bucket=bucket_name)

        yield minio_client


def mock_to_session_credentials(class_self):
    return {
        "aws_access_key_id": settings.OBJECT_STORAGE_ACCESS_KEY_ID,
        "aws_secret_access_key": settings.OBJECT_STORAGE_SECRET_ACCESS_KEY,
        "endpoint_url": settings.OBJECT_STORAGE_ENDPOINT,
        "aws_session_token": None,
        "AWS_ALLOW_HTTP": "true",
        "AWS_S3_ALLOW_UNSAFE_RENAME": "true",
    }


def mock_to_object_store_rs_credentials(class_self):
    return {
        "aws_access_key_id": settings.OBJECT_STORAGE_ACCESS_KEY_ID,
        "aws_secret_access_key": settings.OBJECT_STORAGE_SECRET_ACCESS_KEY,
        "endpoint_url": settings.OBJECT_STORAGE_ENDPOINT,
        "region": "us-east-1",
        "AWS_ALLOW_HTTP": "true",
        "AWS_S3_ALLOW_UNSAFE_RENAME": "true",
    }


@pytest_asyncio.fixture
async def pageview_events(clickhouse_client, ateam):
    start_time, end_time = dt.datetime.now(dt.UTC) - dt.timedelta(days=1), dt.datetime.now(dt.UTC)
    events, _, events_from_other_team = await generate_test_events_in_clickhouse(
        clickhouse_client,
        ateam.pk,
        start_time,
        end_time,
        event_name="$pageview",
        count=50,
        count_outside_range=0,
        distinct_ids=["a", "b"],
        table="sharded_events",
    )
    return (events, events_from_other_team)


async def test_materialize_model(ateam, bucket_name, minio_client, pageview_events):
    query = """\
    select
      event as event,
      if(distinct_id != '0', distinct_id, null) as distinct_id,
      timestamp as timestamp
    from events
    where event = '$pageview'
    """
    saved_query = await DataWarehouseSavedQuery.objects.acreate(
        team=ateam,
        name="my_model",
        query={"query": query, "kind": "HogQLQuery"},
    )

    with override_settings(
        BUCKET_URL=f"s3://{bucket_name}",
        AIRBYTE_BUCKET_KEY=settings.OBJECT_STORAGE_ACCESS_KEY_ID,
        AIRBYTE_BUCKET_SECRET=settings.OBJECT_STORAGE_SECRET_ACCESS_KEY,
        AIRBYTE_BUCKET_REGION="us-east-1",
        AIRBYTE_BUCKET_DOMAIN="objectstorage:19000",
    ):
        job = await database_sync_to_async(DataModelingJob.objects.create)(
            team=ateam,
            status=DataModelingJob.Status.RUNNING,
            workflow_id="test_workflow",
        )

        key, delta_table, job_id = await materialize_model(
            saved_query.id.hex,
            ateam,
            saved_query,
            job,
            unittest.mock.AsyncMock(),
            unittest.mock.AsyncMock(),
        )

    s3_objects = await minio_client.list_objects_v2(
        Bucket=bucket_name, Prefix=f"team_{ateam.pk}_model_{saved_query.id.hex}/"
    )
    table = delta_table.to_pyarrow_table(columns=["event", "distinct_id", "timestamp"])
    events, _ = pageview_events
    expected_events = sorted(
        [
            {
                k: dt.datetime.fromisoformat(v) if k == "timestamp" else v
                for k, v in event.items()
                if k in ("event", "distinct_id", "timestamp")
            }
            for event in events
        ],
        key=lambda d: (d["distinct_id"], d["timestamp"]),
    )

    assert any(f"{saved_query.normalized_name}__query" in obj["Key"] for obj in s3_objects["Contents"])
    assert table.num_rows == len(expected_events)
    assert table.num_columns == 3
    assert table.column_names == ["event", "distinct_id", "timestamp"]
    assert len(s3_objects["Contents"]) != 0
    assert key == saved_query.normalized_name
    assert sorted(table.to_pylist(), key=lambda d: (d["distinct_id"], d["timestamp"])) == expected_events

    # Ensure we can query the table
    await sync_to_async(execute_hogql_query)(f"SELECT * FROM {saved_query.name}", ateam)


async def test_materialize_model_with_pascal_cased_name(ateam, bucket_name, minio_client, pageview_events):
    query = """\
    select
      event as event,
      if(distinct_id != '0', distinct_id, null) as distinct_id,
      timestamp as timestamp
    from events
    where event = '$pageview'
    """
    saved_query = await DataWarehouseSavedQuery.objects.acreate(
        team=ateam,
        name="PascalCasedView",
        query={"query": query, "kind": "HogQLQuery"},
    )

    with override_settings(
        BUCKET_URL=f"s3://{bucket_name}",
        AIRBYTE_BUCKET_KEY=settings.OBJECT_STORAGE_ACCESS_KEY_ID,
        AIRBYTE_BUCKET_SECRET=settings.OBJECT_STORAGE_SECRET_ACCESS_KEY,
        AIRBYTE_BUCKET_REGION="us-east-1",
        AIRBYTE_BUCKET_DOMAIN="objectstorage:19000",
    ):
        job = await database_sync_to_async(DataModelingJob.objects.create)(
            team=ateam,
            status=DataModelingJob.Status.RUNNING,
            workflow_id="test_workflow",
        )

        key, delta_table, job_id = await materialize_model(
            saved_query.id.hex,
            ateam,
            saved_query,
            job,
            unittest.mock.AsyncMock(),
            unittest.mock.AsyncMock(),
        )

    s3_objects = await minio_client.list_objects_v2(
        Bucket=bucket_name, Prefix=f"team_{ateam.pk}_model_{saved_query.id.hex}/"
    )
    table = delta_table.to_pyarrow_table(columns=["event", "distinct_id", "timestamp"])
    events, _ = pageview_events
    expected_events = sorted(
        [
            {
                k: dt.datetime.fromisoformat(v) if k == "timestamp" else v
                for k, v in event.items()
                if k in ("event", "distinct_id", "timestamp")
            }
            for event in events
        ],
        key=lambda d: (d["distinct_id"], d["timestamp"]),
    )

    assert any(f"{saved_query.normalized_name}__query" in obj["Key"] for obj in s3_objects["Contents"])
    assert table.num_rows == len(expected_events)
    assert table.num_columns == 3
    assert table.column_names == ["event", "distinct_id", "timestamp"]
    assert len(s3_objects["Contents"]) != 0
    assert key == saved_query.normalized_name
    assert sorted(table.to_pylist(), key=lambda d: (d["distinct_id"], d["timestamp"])) == expected_events

    # Ensure we can query the table
    await sync_to_async(execute_hogql_query)(f"SELECT * FROM {saved_query.name}", ateam)


@pytest_asyncio.fixture
async def saved_queries(ateam):
    parent_query = """\
      select
        events.event as event,
        events.distinct_id as distinct_id,
        events.timestamp as timestamp
      from events
      where events.event = '$pageview'
    """
    parent_saved_query = await database_sync_to_async(DataWarehouseSavedQuery.objects.create)(
        team=ateam,
        name="my_model",
        query={"query": parent_query, "kind": "HogQLQuery"},
    )
    child_saved_query = await database_sync_to_async(DataWarehouseSavedQuery.objects.create)(
        team=ateam,
        name="my_model_child",
        query={"query": "select * from my_model where distinct_id = 'b'", "kind": "HogQLQuery"},
    )
    child_2_saved_query = await database_sync_to_async(DataWarehouseSavedQuery.objects.create)(
        team=ateam,
        name="my_model_child_2",
        query={"query": "select * from my_model where distinct_id = 'a'", "kind": "HogQLQuery"},
    )
    grand_child_saved_query = await database_sync_to_async(DataWarehouseSavedQuery.objects.create)(
        team=ateam,
        name="my_model_grand_child",
        query={"query": "select * from my_model_child union all select * from my_model_child_2", "kind": "HogQLQuery"},
    )
    await database_sync_to_async(DataWarehouseModelPath.objects.create_from_saved_query)(parent_saved_query)
    await database_sync_to_async(DataWarehouseModelPath.objects.create_from_saved_query)(child_saved_query)
    await database_sync_to_async(DataWarehouseModelPath.objects.create_from_saved_query)(child_2_saved_query)
    await database_sync_to_async(DataWarehouseModelPath.objects.create_from_saved_query)(grand_child_saved_query)

    yield parent_saved_query, child_saved_query, child_2_saved_query, grand_child_saved_query


async def test_build_dag_activity_select_all_ancestors(activity_environment, ateam, saved_queries):
    """Test the build dag activity with a sample set of models.

    In this test we attempt to select all ancestors of a model using a single '+' prefix.
    """
    parent_saved_query, child_saved_query, _, grand_child_saved_query = saved_queries

    select = [Selector(label=child_saved_query.id.hex, ancestors="ALL")]
    inputs = BuildDagActivityInputs(team_id=ateam.pk, select=select)

    async with asyncio.timeout(10):
        dag = await activity_environment.run(build_dag_activity, inputs)

    assert dag[parent_saved_query.id.hex].children == {child_saved_query.id.hex}
    assert dag[parent_saved_query.id.hex].selected is True

    assert dag[child_saved_query.id.hex].parents == {parent_saved_query.id.hex}
    assert dag[child_saved_query.id.hex].children == {grand_child_saved_query.id.hex}
    assert dag[child_saved_query.id.hex].selected is True

    selected = (
        child_saved_query.id.hex,
        parent_saved_query.id.hex,
    )
    assert all(dag[other].selected is False for other in dag.keys() if other not in selected)


async def test_build_dag_activity_select_all_descendants(activity_environment, ateam, saved_queries):
    """Test the build dag activity with a sample set of models.

    In this test we attempt to select all descendants of a model using a single '+' suffix.
    """
    parent_saved_query, child_saved_query, child_2_saved_query, grand_child_saved_query = saved_queries

    select = [Selector(label=parent_saved_query.id.hex, descendants="ALL")]
    inputs = BuildDagActivityInputs(team_id=ateam.pk, select=select)

    async with asyncio.timeout(10):
        dag = await activity_environment.run(build_dag_activity, inputs)

    assert dag[parent_saved_query.id.hex].children == {child_saved_query.id.hex, child_2_saved_query.id.hex}
    assert dag[parent_saved_query.id.hex].selected is True

    assert dag[child_saved_query.id.hex].parents == {parent_saved_query.id.hex}
    assert dag[child_saved_query.id.hex].children == {grand_child_saved_query.id.hex}
    assert dag[child_saved_query.id.hex].selected is True

    assert dag[child_2_saved_query.id.hex].parents == {parent_saved_query.id.hex}
    assert dag[child_2_saved_query.id.hex].children == {grand_child_saved_query.id.hex}
    assert dag[child_2_saved_query.id.hex].selected is True

    assert dag[grand_child_saved_query.id.hex].parents == {child_saved_query.id.hex, child_2_saved_query.id.hex}
    assert not dag[grand_child_saved_query.id.hex].children
    assert dag[grand_child_saved_query.id.hex].selected is True

    selected = (
        grand_child_saved_query.id.hex,
        child_2_saved_query.id.hex,
        child_saved_query.id.hex,
        parent_saved_query.id.hex,
    )
    assert all(dag[other].selected is False for other in dag.keys() if other not in selected)


async def test_build_dag_activity_select_multiple_individual_models(activity_environment, ateam, saved_queries):
    """Test the build dag activity with a sample set of models.

    In this test we select multiple individual models to assert that:
    * All selected models are marked as selected to run.
    * Additional models are included to account for paths connecting models.
    * These additional models are not marked as selected.
    """
    parent_saved_query, child_saved_query, child_2_saved_query, _ = saved_queries

    select = [
        Selector(label=parent_saved_query.id.hex),
        Selector(label=child_saved_query.id.hex),
        Selector(label=child_2_saved_query.id.hex),
    ]
    inputs = BuildDagActivityInputs(team_id=ateam.pk, select=select)

    async with asyncio.timeout(10):
        dag = await activity_environment.run(build_dag_activity, inputs)

    assert len(dag) == 5
    assert dag[parent_saved_query.id.hex].children == {child_saved_query.id.hex, child_2_saved_query.id.hex}

    assert dag[child_saved_query.id.hex].parents == {parent_saved_query.id.hex}
    assert dag[child_2_saved_query.id.hex].parents == {parent_saved_query.id.hex}

    selected = tuple(selected.label for selected in select)
    assert all(dag[selected].selected is True for selected in selected)
    assert all(dag[other].selected is False for other in dag.keys() if other not in selected)


async def test_build_dag_activity_select_first_parents(activity_environment, ateam, saved_queries):
    """Test the build dag activity with a sample set of models.

    In this test we attempt to select first parents of a model using a '1+' prefix.
    """
    _, child_saved_query, child_2_saved_query, grand_child_saved_query = saved_queries

    select = [Selector(label=grand_child_saved_query.id.hex, ancestors=1)]
    inputs = BuildDagActivityInputs(team_id=ateam.pk, select=select)

    async with asyncio.timeout(10):
        dag = await activity_environment.run(build_dag_activity, inputs)

    assert dag[child_2_saved_query.id.hex].children == {grand_child_saved_query.id.hex}
    assert dag[child_saved_query.id.hex].children == {grand_child_saved_query.id.hex}
    assert dag[grand_child_saved_query.id.hex].parents == {child_2_saved_query.id.hex, child_saved_query.id.hex}

    selected = (
        child_saved_query.id.hex,
        child_2_saved_query.id.hex,
        grand_child_saved_query.id.hex,
    )
    assert all(dag[selected].selected is True for selected in selected)
    assert all(dag[other].selected is False for other in dag.keys() if other not in selected)


async def test_build_dag_activity_select_first_children(activity_environment, ateam, saved_queries):
    """Test the build dag activity with a sample set of models.

    In this test we attempt to select first children of a model using a '+1' suffix.
    """
    parent_saved_query, child_saved_query, child_2_saved_query, _ = saved_queries

    select = [Selector(label=parent_saved_query.id.hex, descendants=1)]
    inputs = BuildDagActivityInputs(team_id=ateam.pk, select=select)

    async with asyncio.timeout(10):
        dag = await activity_environment.run(build_dag_activity, inputs)

    assert dag[child_2_saved_query.id.hex].parents == {parent_saved_query.id.hex}
    assert dag[child_saved_query.id.hex].parents == {parent_saved_query.id.hex}
    assert dag[parent_saved_query.id.hex].children == {child_2_saved_query.id.hex, child_saved_query.id.hex}

    selected = (
        child_saved_query.id.hex,
        child_2_saved_query.id.hex,
        parent_saved_query.id.hex,
    )
    assert all(dag[selected].selected is True for selected in selected)
    assert all(dag[other].selected is False for other in dag.keys() if other not in selected)


async def test_build_dag_activity_select_first_family(activity_environment, ateam, saved_queries):
    """Test the build dag activity with a sample set of models.

    In this test we attempt to select first children and first parents of a model using a
    both a'+1' suffix and a '1+' prefix.
    """
    parent_saved_query, child_saved_query, _, grand_child_saved_query = saved_queries

    select = [Selector(label=child_saved_query.id.hex, descendants=1, ancestors=1)]
    inputs = BuildDagActivityInputs(team_id=ateam.pk, select=select)

    async with asyncio.timeout(10):
        dag = await activity_environment.run(build_dag_activity, inputs)

    assert dag[child_saved_query.id.hex].parents == {parent_saved_query.id.hex}
    assert dag[grand_child_saved_query.id.hex].parents == {child_saved_query.id.hex}
    assert dag[parent_saved_query.id.hex].children == {child_saved_query.id.hex}

    selected = (
        child_saved_query.id.hex,
        parent_saved_query.id.hex,
        grand_child_saved_query.id.hex,
    )
    assert all(dag[selected].selected is True for selected in selected)
    assert all(dag[other].selected is False for other in dag.keys() if other not in selected)


async def test_build_dag_activity_select_all(activity_environment, ateam, saved_queries):
    """Test the build dag activity with a sample set of models.

    In this test we attempt to select all models by not passing any selectors.
    """
    parent_saved_query, child_saved_query, child_2_saved_query, grand_child_saved_query = saved_queries

    inputs = BuildDagActivityInputs(team_id=ateam.pk)

    async with asyncio.timeout(10):
        dag = await activity_environment.run(build_dag_activity, inputs)

    assert dag[child_saved_query.id.hex].parents == {parent_saved_query.id.hex}
    assert dag[child_saved_query.id.hex].children == {grand_child_saved_query.id.hex}
    assert dag[child_2_saved_query.id.hex].parents == {parent_saved_query.id.hex}
    assert dag[child_2_saved_query.id.hex].children == {grand_child_saved_query.id.hex}
    assert dag[grand_child_saved_query.id.hex].parents == {child_saved_query.id.hex, child_2_saved_query.id.hex}
    assert dag[parent_saved_query.id.hex].children == {child_saved_query.id.hex, child_2_saved_query.id.hex}

    assert all(dag[selected].selected is True for selected in dag.keys() if selected not in {"events", "persons"})


async def test_run_workflow_with_minio_bucket(
    minio_client,
    ateam,
    bucket_name,
    pageview_events,
    saved_queries,
    temporal_client,
):
    """Test run workflow end-to-end using a local MinIO bucket."""
    events, _ = pageview_events
    all_expected_events = sorted(
        [
            {
                k: dt.datetime.fromisoformat(v) if k == "timestamp" else v
                for k, v in event.items()
                if k in ("event", "distinct_id", "timestamp")
            }
            for event in events
        ],
        key=lambda d: (d["distinct_id"], d["timestamp"]),
    )
    expected_events_a = [event for event in all_expected_events if event["distinct_id"] == "a"]
    expected_events_b = [event for event in all_expected_events if event["distinct_id"] == "b"]

    for query in saved_queries:
        attached_table = await DataWarehouseTable.objects.acreate(
            name=query.name,
            team=ateam,
            format="Delta",
            url_pattern=f"s3://{bucket_name}/team_{ateam.pk}_model_{query.id.hex}/modeling/{query.normalized_name}",
            credential=None,
        )
        # link the saved query to the table
        query.table_id = attached_table.id
        await database_sync_to_async(query.save)()

    workflow_id = str(uuid.uuid4())
    inputs = RunWorkflowInputs(team_id=ateam.pk)

    with (
        override_settings(
            BUCKET_URL=f"s3://{bucket_name}",
            AIRBYTE_BUCKET_KEY=settings.OBJECT_STORAGE_ACCESS_KEY_ID,
            AIRBYTE_BUCKET_SECRET=settings.OBJECT_STORAGE_SECRET_ACCESS_KEY,
            AIRBYTE_BUCKET_REGION="us-east-1",
            AIRBYTE_BUCKET_DOMAIN="objectstorage:19000",
        ),
        freeze_time(TEST_TIME),
    ):
        async with temporalio.worker.Worker(
            temporal_client,
            task_queue=constants.DATA_MODELING_TASK_QUEUE,
            workflows=[RunWorkflow],
            activities=[
                start_run_activity,
                build_dag_activity,
                run_dag_activity,
                finish_run_activity,
                create_table_activity,
                create_job_model_activity,
                fail_jobs_activity,
                cleanup_running_jobs_activity,
            ],
            workflow_runner=temporalio.worker.UnsandboxedWorkflowRunner(),
        ):
            # Ensure the team exists in the DB context before running workflow
            await database_sync_to_async(Team.objects.get)(pk=ateam.pk)
            await temporal_client.execute_workflow(
                RunWorkflow.run,
                inputs,
                id=workflow_id,
                task_queue=constants.DATA_MODELING_TASK_QUEUE,
                retry_policy=temporalio.common.RetryPolicy(maximum_attempts=1),
                execution_timeout=dt.timedelta(seconds=30),
            )

            tables_and_queries = {}

            for query in saved_queries:
                await database_sync_to_async(query.refresh_from_db)()
                db_table = await DataWarehouseTable.objects.aget(id=query.table_id)

                delta_table = deltalake.DeltaTable(
                    table_uri=db_table.url_pattern,
                    storage_options={
                        "aws_access_key_id": str(settings.AIRBYTE_BUCKET_KEY),
                        "aws_secret_access_key": str(settings.AIRBYTE_BUCKET_SECRET),
                        "endpoint_url": settings.OBJECT_STORAGE_ENDPOINT,
                        "region_name": str(settings.AIRBYTE_BUCKET_REGION),
                        "AWS_ALLOW_HTTP": "true",
                        "AWS_S3_ALLOW_UNSAFE_RENAME": "true",
                    },
                )

                # All test tables have the same columns, which is a limitation of our test
                table = delta_table.to_pyarrow_table(columns=["event", "distinct_id", "timestamp"])
                tables_and_queries[query.normalized_name] = (table, query)

            for key, table_and_query in tables_and_queries.items():
                table, query = table_and_query

                if "distinct_id = 'a'" in query.query["query"]:
                    expected_data = expected_events_a
                elif "distinct_id = 'b'" in query.query["query"]:
                    expected_data = expected_events_b
                else:
                    expected_data = all_expected_events

                assert table.num_rows == len(expected_data)
                assert table.num_columns == 3
                assert table.column_names == ["event", "distinct_id", "timestamp"]
                assert key == query.normalized_name
                assert sorted(table.to_pylist(), key=lambda d: (d["distinct_id"], d["timestamp"])) == expected_data
                assert query.status == DataWarehouseSavedQuery.Status.COMPLETED
                assert query.last_run_at == TEST_TIME

                # Verify row count was updated in the DataWarehouseTable
                warehouse_table = await DataWarehouseTable.objects.aget(team_id=ateam.pk, id=query.table_id)
                assert warehouse_table is not None, f"DataWarehouseTable for {query.name} not found"
                # Match the 50 page_view events defined above
                assert warehouse_table.row_count == len(
                    expected_data
                ), f"Row count for {query.name} not the expected value"


async def test_run_workflow_with_minio_bucket_with_errors(
    minio_client,
    ateam,
    bucket_name,
    pageview_events,
    saved_queries,
    temporal_client,
):
    """Test run workflow end-to-end using a local MinIO bucket."""
    for query in saved_queries:
        attached_table = await DataWarehouseTable.objects.acreate(
            name=query.name,
            team=ateam,
            format="Delta",
            url_pattern=f"s3://{bucket_name}/team_{ateam.pk}_model_{query.id.hex}",
            credential=None,
        )
        # link the saved query to the table
        query.table_id = attached_table.id
        await database_sync_to_async(query.save)()

    workflow_id = str(uuid.uuid4())
    inputs = RunWorkflowInputs(team_id=ateam.pk)

    async def mock_materialize_model(model_label, team, saved_query, job):
        raise Exception("testing exception")

    with (
        override_settings(
            BUCKET_URL=f"s3://{bucket_name}",
            AIRBYTE_BUCKET_KEY=settings.OBJECT_STORAGE_ACCESS_KEY_ID,
            AIRBYTE_BUCKET_SECRET=settings.OBJECT_STORAGE_SECRET_ACCESS_KEY,
            AIRBYTE_BUCKET_REGION="us-east-1",
            AIRBYTE_BUCKET_DOMAIN="objectstorage:19000",
        ),
        freeze_time(TEST_TIME),
        unittest.mock.patch("posthog.temporal.data_modeling.run_workflow.materialize_model", mock_materialize_model),
    ):
        async with temporalio.worker.Worker(
            temporal_client,
            task_queue=constants.DATA_MODELING_TASK_QUEUE,
            workflows=[RunWorkflow],
            activities=[
                start_run_activity,
                build_dag_activity,
                run_dag_activity,
                finish_run_activity,
                create_table_activity,
                create_job_model_activity,
                fail_jobs_activity,
                cleanup_running_jobs_activity,
            ],
            workflow_runner=temporalio.worker.UnsandboxedWorkflowRunner(),
        ):
            # Ensure the team exists in the DB context before running workflow
            await database_sync_to_async(Team.objects.get)(pk=ateam.pk)
            await temporal_client.execute_workflow(
                RunWorkflow.run,
                inputs,
                id=workflow_id,
                task_queue=constants.DATA_MODELING_TASK_QUEUE,
                retry_policy=temporalio.common.RetryPolicy(maximum_attempts=1),
                execution_timeout=dt.timedelta(seconds=30),
            )

    job = await DataModelingJob.objects.aget(workflow_id=workflow_id)
    assert job is not None
    assert job.status == DataModelingJob.Status.FAILED


async def test_dlt_direct_naming(ateam, bucket_name, minio_client, pageview_events):
    """Test that setting SCHEMA__NAMING=direct preserves original column casing when materializing models."""
    # Query with CamelCase and PascalCase column names, not snake_case
    query = """\
    select
      event as Event,
      if(distinct_id != '0', distinct_id, null) as DistinctId,
      timestamp as TimeStamp,
      'example' as CamelCaseColumn
    from events
    where event = '$pageview'
    """
    saved_query = await DataWarehouseSavedQuery.objects.acreate(
        team=ateam,
        name="camel_case_model",
        query={"query": query, "kind": "HogQLQuery"},
    )

    # Make sure we have pageview events for the query to work with
    events, _ = pageview_events

    with (
        override_settings(
            BUCKET_URL=f"s3://{bucket_name}",
            AIRBYTE_BUCKET_KEY=settings.OBJECT_STORAGE_ACCESS_KEY_ID,
            AIRBYTE_BUCKET_SECRET=settings.OBJECT_STORAGE_SECRET_ACCESS_KEY,
            AIRBYTE_BUCKET_REGION="us-east-1",
            AIRBYTE_BUCKET_DOMAIN="objectstorage:19000",
        ),
        unittest.mock.patch.dict(os.environ, {"SCHEMA__NAMING": "direct"}, clear=True),
    ):
        job = await database_sync_to_async(DataModelingJob.objects.create)(
            team=ateam,
            status=DataModelingJob.Status.RUNNING,
            workflow_id="test_workflow",
        )

        # Check that SCHEMA__NAMING is set to direct in the environment
        assert os.environ.get("SCHEMA__NAMING") == "direct", "SCHEMA__NAMING should be 'direct'"

        key, delta_table, job_id = await materialize_model(
            saved_query.id.hex,
            ateam,
            saved_query,
            job,
            unittest.mock.AsyncMock(),
            unittest.mock.AsyncMock(),
        )

    # Check that the column names maintain their original casing
    table_columns = delta_table.to_pyarrow_table().column_names
    # Verify the original capitalization is preserved
    assert "Event" in table_columns, "Column 'Event' should maintain its original capitalization"
    assert "DistinctId" in table_columns, "Column 'DistinctId' should maintain its original capitalization"
    assert "TimeStamp" in table_columns, "Column 'TimeStamp' should maintain its original capitalization"
    assert "CamelCaseColumn" in table_columns, "Column 'CamelCaseColumn' should maintain its original capitalization"


async def test_materialize_model_with_decimal256_fix(ateam, bucket_name, minio_client):
    """Test that materialize_model successfully transforms Decimal256 types to float since decimal128 is not precise enough."""
    query = "SELECT 1 as test_column FROM events LIMIT 1"
    saved_query = await DataWarehouseSavedQuery.objects.acreate(
        team=ateam,
        name="decimal_fix_test_model",
        query={"query": query, "kind": "HogQLQuery"},
    )

    def mock_hogql_table(*args, **kwargs):
        from decimal import Decimal

        high_precision_decimal_type = pa.decimal256(76, 32)
        problematic_data = pa.array(
            [Decimal("12345678901234567890123456789012345678901234.12345678901234567890123456789012")],
            type=high_precision_decimal_type,
        )

        table = pa.table({"high_precision_decimal": problematic_data, "regular_column": pa.array([1], type=pa.int64())})

        async def async_generator():
            yield table

        return async_generator()

    with (
        override_settings(
            BUCKET_URL=f"s3://{bucket_name}",
            AIRBYTE_BUCKET_KEY=settings.OBJECT_STORAGE_ACCESS_KEY_ID,
            AIRBYTE_BUCKET_SECRET=settings.OBJECT_STORAGE_SECRET_ACCESS_KEY,
            AIRBYTE_BUCKET_REGION="us-east-1",
            AIRBYTE_BUCKET_DOMAIN="objectstorage:19000",
        ),
        unittest.mock.patch("posthog.temporal.data_modeling.run_workflow.hogql_table", mock_hogql_table),
    ):
        job = await database_sync_to_async(DataModelingJob.objects.create)(
            team=ateam,
            status=DataModelingJob.Status.RUNNING,
            workflow_id="test_workflow",
        )

        key, delta_table, job_id = await materialize_model(
            saved_query.id.hex,
            ateam,
            saved_query,
            job,
            unittest.mock.AsyncMock(),
            unittest.mock.AsyncMock(),
        )

        assert key == saved_query.normalized_name

        table = delta_table.to_pyarrow_table()
        assert table.num_rows == 1
        assert "high_precision_decimal" in table.column_names
        assert "regular_column" in table.column_names

        high_precision_column = table.column("high_precision_decimal")
        # Should be Decimal128 with reduced precision, not float64
        assert pa.types.is_decimal(high_precision_column.type)
        assert isinstance(high_precision_column.type, pa.Decimal128Type)
        assert high_precision_column.type.precision == 38
        assert high_precision_column.type.scale == 37

        await database_sync_to_async(job.refresh_from_db)()
        assert job.status == DataModelingJob.Status.COMPLETED


async def test_materialize_model_with_decimal256_downscale_to_decimal128(ateam, bucket_name, minio_client):
    """Test that materialize_model successfully downscales Decimal256 to Decimal128 when the value fits."""
    query = "SELECT 1 as test_column FROM events LIMIT 1"
    saved_query = await DataWarehouseSavedQuery.objects.acreate(
        team=ateam,
        name="decimal_downscale_test_model",
        query={"query": query, "kind": "HogQLQuery"},
    )

    def mock_hogql_table(*args, **kwargs):
        from decimal import Decimal

        high_precision_decimal_type = pa.decimal256(50, 10)
        manageable_data = pa.array(
            [Decimal("1234567890123456789012345678.1234567890")],
            type=high_precision_decimal_type,
        )

        table = pa.table({"manageable_decimal": manageable_data, "regular_column": pa.array([1], type=pa.int64())})

        async def async_generator():
            yield table

        return async_generator()

    with (
        override_settings(
            BUCKET_URL=f"s3://{bucket_name}",
            AIRBYTE_BUCKET_KEY=settings.OBJECT_STORAGE_ACCESS_KEY_ID,
            AIRBYTE_BUCKET_SECRET=settings.OBJECT_STORAGE_SECRET_ACCESS_KEY,
            AIRBYTE_BUCKET_REGION="us-east-1",
            AIRBYTE_BUCKET_DOMAIN="objectstorage:19000",
        ),
        unittest.mock.patch("posthog.temporal.data_modeling.run_workflow.hogql_table", mock_hogql_table),
    ):
        job = await database_sync_to_async(DataModelingJob.objects.create)(
            team=ateam,
            status=DataModelingJob.Status.RUNNING,
            workflow_id="test_workflow",
        )

        key, delta_table, job_id = await materialize_model(
            saved_query.id.hex,
            ateam,
            saved_query,
            job,
            unittest.mock.AsyncMock(),
            unittest.mock.AsyncMock(),
        )

        assert key == saved_query.normalized_name

        table = delta_table.to_pyarrow_table()
        assert table.num_rows == 1
        assert "manageable_decimal" in table.column_names
        assert "regular_column" in table.column_names

        manageable_decimal_column = table.column("manageable_decimal")
        # Should be Decimal128, not float64
        assert pa.types.is_decimal(manageable_decimal_column.type)
        assert isinstance(manageable_decimal_column.type, pa.Decimal128Type)
        assert manageable_decimal_column.type.precision == 38
        assert manageable_decimal_column.type.scale == 10

        await database_sync_to_async(job.refresh_from_db)()
        assert job.status == DataModelingJob.Status.COMPLETED


async def test_cleanup_running_jobs_activity(activity_environment, ateam):
    """Test cleanup marks all existing RUNNING jobs as FAILED when starting a new run."""
    old_job = await database_sync_to_async(DataModelingJob.objects.create)(
        team=ateam, status=DataModelingJob.Status.RUNNING, workflow_id="old-1", workflow_run_id="run-1"
    )
    recent_job = await database_sync_to_async(DataModelingJob.objects.create)(
        team=ateam, status=DataModelingJob.Status.RUNNING, workflow_id="recent-1", workflow_run_id="run-2"
    )
    completed_job = await database_sync_to_async(DataModelingJob.objects.create)(
        team=ateam, status=DataModelingJob.Status.COMPLETED, workflow_id="completed-1", workflow_run_id="run-3"
    )

    await activity_environment.run(cleanup_running_jobs_activity, CleanupRunningJobsActivityInputs(team_id=ateam.pk))

    await database_sync_to_async(old_job.refresh_from_db)()
    await database_sync_to_async(recent_job.refresh_from_db)()
    await database_sync_to_async(completed_job.refresh_from_db)()

    assert old_job.status == DataModelingJob.Status.FAILED
    assert old_job.error is not None
    assert "Job timed out" in old_job.error
    assert recent_job.status == DataModelingJob.Status.FAILED
    assert recent_job.error is not None
    assert "Job timed out" in recent_job.error
    assert completed_job.status == DataModelingJob.Status.COMPLETED


async def test_create_job_model_activity_cleans_up_running_jobs(activity_environment, ateam, temporal_client):
    """Test that orphaned jobs are cleaned up when running the full workflow."""
    # Create old orphaned job
    orphaned_job = await database_sync_to_async(DataModelingJob.objects.create)(
        team=ateam, status=DataModelingJob.Status.RUNNING, workflow_id="orphaned-1", workflow_run_id="run-1"
    )
    await database_sync_to_async(DataModelingJob.objects.filter(id=orphaned_job.id).update)(
        updated_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=2)
    )

    saved_query = await database_sync_to_async(DataWarehouseSavedQuery.objects.create)(
        team=ateam, name="test_query", query={"query": "SELECT * FROM events LIMIT 10", "kind": "HogQLQuery"}
    )

    await activity_environment.run(cleanup_running_jobs_activity, CleanupRunningJobsActivityInputs(team_id=ateam.pk))

    await database_sync_to_async(orphaned_job.refresh_from_db)()
    assert orphaned_job.status == DataModelingJob.Status.FAILED
    assert orphaned_job.error is not None
    assert "Job timed out" in orphaned_job.error

    with unittest.mock.patch("temporalio.activity.info") as mock_info:
        mock_info.return_value.workflow_id = "new-workflow"
        mock_info.return_value.workflow_run_id = "new-run"

        new_job_id = await activity_environment.run(
            create_job_model_activity,
            CreateJobModelInputs(
                team_id=ateam.pk, select=[Selector(label=saved_query.id.hex, ancestors=0, descendants=0)]
            ),
        )

    new_job = await database_sync_to_async(DataModelingJob.objects.get)(id=new_job_id)
    assert new_job.status == DataModelingJob.Status.RUNNING
    assert new_job.workflow_id == "new-workflow"
    assert new_job.workflow_run_id == "new-run"


async def test_materialize_model_progress_tracking(ateam, bucket_name, minio_client):
    """Test that materialize_model tracks progress during S3 writes."""
    query = "SELECT 1 as test_column FROM events LIMIT 1"
    saved_query = await DataWarehouseSavedQuery.objects.acreate(
        team=ateam,
        name="progress_tracking_test_model",
        query={"query": query, "kind": "HogQLQuery"},
    )

    def mock_hogql_table(*args, **kwargs):
        # Create multiple batches to test progress tracking
        batch1 = pa.table({"test_column": pa.array([1, 2, 3], type=pa.int64())})
        batch2 = pa.table({"test_column": pa.array([4, 5], type=pa.int64())})
        batch3 = pa.table({"test_column": pa.array([6], type=pa.int64())})

        async def async_generator():
            yield batch1
            yield batch2
            yield batch3

        return async_generator()

    with (
        override_settings(
            BUCKET_URL=f"s3://{bucket_name}",
            AIRBYTE_BUCKET_KEY=settings.OBJECT_STORAGE_ACCESS_KEY_ID,
            AIRBYTE_BUCKET_SECRET=settings.OBJECT_STORAGE_SECRET_ACCESS_KEY,
            AIRBYTE_BUCKET_REGION="us-east-1",
            AIRBYTE_BUCKET_DOMAIN="objectstorage:19000",
        ),
        unittest.mock.patch("posthog.temporal.data_modeling.run_workflow.hogql_table", mock_hogql_table),
        unittest.mock.patch("posthog.temporal.data_modeling.run_workflow.get_query_row_count", return_value=6),
    ):
        job = await database_sync_to_async(DataModelingJob.objects.create)(
            team=ateam,
            status=DataModelingJob.Status.RUNNING,
            workflow_id="test_workflow",
        )

        # Verify initial state
        assert job.rows_materialized == 0

        key, delta_table, job_id = await materialize_model(
            saved_query.id.hex,
            ateam,
            saved_query,
            job,
            unittest.mock.AsyncMock(),
            unittest.mock.AsyncMock(),
        )

        # Verify final state
        await database_sync_to_async(job.refresh_from_db)()
        assert job.status == DataModelingJob.Status.COMPLETED
        assert job.rows_materialized == 6
        assert job.rows_expected == 6


async def test_create_table_activity_row_count_functionality(minio_client, activity_environment, ateam):
    """Test that create_table_activity properly sets row count using get_count() method."""

    saved_query = await DataWarehouseSavedQuery.objects.acreate(
        team=ateam,
        name="test_row_count_query",
        query={"query": "SELECT 1 as id, 'test' as name UNION ALL SELECT 2 as id, 'test2' as name"},
    )

    from posthog.warehouse.models import DataWarehouseTable, DataWarehouseCredential

    credential = await DataWarehouseCredential.objects.acreate(
        team=ateam,
        access_key="test_key",
        access_secret="test_secret",
    )

    table = await DataWarehouseTable.objects.acreate(
        team=ateam,
        name="test_table",
        credential=credential,
        format=DataWarehouseTable.TableFormat.DeltaS3Wrapper,
        url_pattern="https://test-bucket/test-path",
        row_count=0,
    )

    saved_query.table = table
    await saved_query.asave()

    create_table_activity_inputs = CreateTableActivityInputs(
        models=[str(saved_query.id)],  # Pass UUID, not name
        team_id=ateam.pk,
    )

    with (
        patch("posthog.temporal.data_modeling.run_workflow.create_table_from_saved_query") as mock_create_table,
        patch.object(DataWarehouseTable, "get_count", return_value=42) as mock_get_count,
    ):
        async with asyncio.timeout(10):
            await activity_environment.run(create_table_activity, create_table_activity_inputs)

    mock_create_table.assert_called_once_with(str(saved_query.id), ateam.pk)
    mock_get_count.assert_called_once()
    await table.arefresh_from_db()
    assert table.row_count == 42


@pytest.mark.asyncio
async def test_create_table_activity_invalid_uuid_fails(activity_environment, ateam):
    """Test that create_table_activity fails fast when given non-UUID model identifier."""

    create_table_activity_inputs = CreateTableActivityInputs(
        models=["invalid_model_name"],  # Name instead of UUID
        team_id=ateam.pk,
    )

    with (
        patch("posthog.temporal.data_modeling.run_workflow.create_table_from_saved_query") as mock_create_table,
        patch("posthog.temporal.data_modeling.run_workflow.bind_temporal_worker_logger") as mock_logger,
    ):
        mock_logger.return_value.aerror = unittest.mock.AsyncMock()

        async with asyncio.timeout(10):
            await activity_environment.run(create_table_activity, create_table_activity_inputs)

    mock_create_table.assert_not_called()

    mock_logger.return_value.aerror.assert_called_once()
    error_message = mock_logger.return_value.aerror.call_args[0][0]
    assert "Invalid model identifier 'invalid_model_name': expected UUID format" in error_message
