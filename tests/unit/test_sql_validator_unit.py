import asyncio
from typing import Optional
from unittest.mock import Mock, patch
import pytest
import httpx
import respx
from spectacles.validators.sql import Query, SqlValidator
from spectacles.lookml import Explore, Dimension
from spectacles.exceptions import LookerApiError


@pytest.fixture
def validator(looker_client) -> SqlValidator:
    # TODO: Make sure we're mocking the login calls on client instantiation
    return SqlValidator(looker_client)


@pytest.fixture
def queries_to_run() -> asyncio.Queue[Optional[Query]]:
    """Creates a queue of Queries or a sentinel None."""
    queue: asyncio.Queue[Optional[Query]] = asyncio.Queue()
    return queue


@pytest.fixture
def running_queries() -> asyncio.Queue[str]:
    """Creates a queue of query task IDs."""
    queue: asyncio.Queue[str] = asyncio.Queue()
    return queue


@pytest.fixture
def query_slot() -> asyncio.Semaphore:
    """Creates a semaphore to limit query concurrency."""
    semaphore = asyncio.Semaphore(1)
    return semaphore


@pytest.fixture
def query(explore: Explore, dimension: Dimension) -> Query:
    return Query(explore, (dimension,), query_id=12345)


async def test_compile_explore_without_dimensions_should_not_work(
    explore: Explore, validator: SqlValidator
):
    with pytest.raises(AttributeError):
        await validator.compile_explore(explore)


async def test_compile_explore_compiles_sql(
    mocked_api: respx.MockRouter,
    explore: Explore,
    dimension: Dimension,
    validator: SqlValidator,
):
    query_id = 12345
    sql = "SELECT * FROM users"
    explore.dimensions = [dimension]
    mocked_api.post("queries", params={"fields": "id"}, name="create_query").respond(
        200, json={"id": query_id}
    )
    mocked_api.get(f"queries/{query_id}/run/sql", name="run_query").respond(
        200, text=sql
    )
    compiled = await validator.compile_explore(explore)
    assert compiled.explore_name == explore.name
    assert compiled.model_name == explore.model_name
    assert compiled.sql == sql
    assert compiled.dimension_name is None
    mocked_api["create_query"].calls.assert_called_once()
    mocked_api["run_query"].calls.assert_called_once()


async def test_compile_dimension_compiles_sql(
    mocked_api: respx.MockRouter,
    dimension: Dimension,
    validator: SqlValidator,
):
    query_id = 12345
    sql = "SELECT * FROM users"
    mocked_api.post("queries", params={"fields": "id"}, name="create_query").respond(
        200, json={"id": query_id}
    )
    mocked_api.get(f"queries/{query_id}/run/sql", name="run_query").respond(
        200, text=sql
    )
    compiled = await validator.compile_dimension(dimension)
    assert compiled.explore_name == dimension.explore_name
    assert compiled.model_name == dimension.model_name
    assert compiled.sql == sql
    assert compiled.dimension_name is dimension.name
    mocked_api["create_query"].calls.assert_called_once()
    mocked_api["run_query"].calls.assert_called_once()


async def test_run_query_works(
    mocked_api: respx.MockRouter,
    query: Query,
    validator: SqlValidator,
    queries_to_run: asyncio.Queue,
    running_queries: asyncio.Queue,
    query_slot: asyncio.Semaphore,
):
    query_task_id = "abcdef12345"
    explore_url = "https://spectacles.looker.com/x"

    mocked_api.post(
        "queries", params={"fields": "id,share_url"}, name="create_query"
    ).respond(200, json={"id": query.query_id, "share_url": explore_url})
    mocked_api.post(
        "query_tasks",
        params={"fields": "id", "cache": "false"},
        name="create_query_task",
    ).respond(200, json={"id": query_task_id})

    task = asyncio.create_task(
        validator._run_query(queries_to_run, running_queries, query_slot)
    )

    await queries_to_run.put(query)
    await running_queries.get()
    # Have to manually mark the queue task as done, since normally this is handled by
    # `SqlValidator._get_query_results`
    queries_to_run.task_done()
    query_slot.release()
    await queries_to_run.join()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.gather(task)

    mocked_api["create_query"].calls.assert_called_once()
    mocked_api["create_query_task"].calls.assert_called_once()


async def test_run_query_shuts_down_on_sentinel(
    validator: SqlValidator,
    queries_to_run: asyncio.Queue,
    running_queries: asyncio.Queue,
    query_slot: asyncio.Semaphore,
):
    task = asyncio.create_task(
        validator._run_query(queries_to_run, running_queries, query_slot)
    )

    await queries_to_run.put(None)
    await queries_to_run.join()
    await asyncio.gather(task)


async def test_run_query_handles_exceptions_raised_within(
    mocked_api: respx.MockRouter,
    query: Query,
    validator: SqlValidator,
    queries_to_run: asyncio.Queue,
    running_queries: asyncio.Queue,
    query_slot: asyncio.Semaphore,
):
    query_task_id = "abcdef12345"
    explore_url = "https://spectacles.looker.com/x"

    mocked_api.post(
        "queries", params={"fields": "id,share_url"}, name="create_query"
    ).mock(
        side_effect=(
            httpx.Response(200, json={"id": query.query_id, "share_url": explore_url}),
            httpx.Response(404),
        )
    )

    mocked_api.post(
        "query_tasks",
        params={"fields": "id", "cache": "false"},
        name="create_query_task",
    ).respond(200, json={"id": query_task_id})

    task = asyncio.create_task(
        validator._run_query(queries_to_run, running_queries, query_slot)
    )

    queries_to_run.put_nowait(query)  # This will succeed
    queries_to_run.put_nowait(query)  # This will fail with 404
    await running_queries.get()  # Retrieve the successfully query

    # Normally these steps are handled by _get_query_results
    queries_to_run.task_done()
    query_slot.release()
    await queries_to_run.join()

    with pytest.raises(LookerApiError):
        await asyncio.gather(task)

    assert running_queries.empty
    mocked_api["create_query"].calls.assert_called()


@pytest.mark.parametrize("fail_fast", (True, False))
async def test_get_query_results_works(
    fail_fast: bool,
    mocked_api: respx.MockRouter,
    validator: SqlValidator,
    queries_to_run: asyncio.Queue,
    running_queries: asyncio.Queue,
    query_slot: asyncio.Semaphore,
):
    mocked_api.get("query_tasks/multi_results", name="get_query_results").respond(
        200, json={}
    )

    query_task_id = "abcdef12345"
    task = asyncio.create_task(
        validator._get_query_results(
            queries_to_run, running_queries, fail_fast, query_slot
        )
    )

    await running_queries.put(query_task_id)
    await running_queries.join()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.gather(task)

    mocked_api["get_query_results"].calls.assert_called_once()


@pytest.mark.parametrize("fail_fast", (True, False))
async def test_get_query_results_error_query_is_divided(fail_fast: bool):
    ...


@pytest.mark.parametrize("fail_fast", (True, False))
@patch.object(Query, "divide")
async def test_get_query_results_passing_query_is_not_divided(
    mock_divide: Mock,
    fail_fast: bool,
    mocked_api: respx.MockRouter,
    query: Query,
    validator: SqlValidator,
    queries_to_run: asyncio.Queue,
    running_queries: asyncio.Queue,
    query_slot: asyncio.Semaphore,
):
    query_task_id = "abcdef12345"
    mocked_api.get("query_tasks/multi_results", name="get_query_results").respond(
        200,
        json={
            query_task_id: {
                "status": "complete",
                "data": {
                    "id": query_task_id,
                    "runtime": 460.0,
                    "sql": "SELECT * FROM users",
                },
            }
        },
    )
    validator._task_to_query[query_task_id] = query

    task = asyncio.create_task(
        validator._get_query_results(
            queries_to_run, running_queries, fail_fast, query_slot
        )
    )

    await queries_to_run.put(query)
    await running_queries.put(query_task_id)
    await running_queries.join()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.gather(task)

    mocked_api["get_query_results"].calls.assert_called_once()
    mock_divide.assert_not_called()
    assert query.errored is False
    assert query.explore.queried
    assert query in validator._long_running_queries


async def test_get_query_results_handles_exceptions_raised_within():
    ...
