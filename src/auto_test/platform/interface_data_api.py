"""Project-scoped dataset, recurring schedule and trend routes."""

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool


class DatasetInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    content: str = Field(max_length=1_000_000)
    format: str = Field(default="json", pattern="^(json|csv)$")


class DatasetRunInput(BaseModel):
    dataset_id: str = Field(min_length=1, max_length=64)
    concurrency: int = Field(default=3, ge=1, le=5)


class ScheduleInput(BaseModel):
    dataset_id: str = Field(default="", max_length=64)
    interval_seconds: int = Field(ge=60, le=2_592_000)
    concurrency: int = Field(default=3, ge=1, le=5)


class ScheduleToggleInput(BaseModel):
    enabled: bool


def create_interface_data_api(store, manager, *, router=None) -> APIRouter:
    router = router if router is not None else APIRouter()

    def scope(request):
        context = request.state.identity
        return context["current_project"]["id"], context["user"]["id"]

    async def call(function, *args, **kwargs):
        try:
            return await run_in_threadpool(function, *args, **kwargs)
        except KeyError as exc:
            raise HTTPException(404, "场景、数据集或计划不存在") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/interface-scenarios/{scenario_id}/automation")
    async def workspace(scenario_id: str, request: Request):
        project, _ = scope(request)
        if not await call(store.get_interface_scenario, project, scenario_id):
            raise HTTPException(404, "场景不存在")
        return {"datasets": await call(store.list_interface_datasets, project, scenario_id), "schedules": await call(store.list_interface_schedules, project, scenario_id), "trend": await call(store.interface_scenario_trend, project, scenario_id)}

    @router.post("/interface-scenarios/{scenario_id}/datasets", status_code=201)
    async def save_dataset(scenario_id: str, payload: DatasetInput, request: Request):
        project, actor = scope(request)
        return await call(store.save_interface_dataset, project, scenario_id, payload.name, payload.content, payload.format, actor)

    @router.delete("/interface-datasets/{dataset_id}")
    async def delete_dataset(dataset_id: str, request: Request):
        project, _ = scope(request)
        if not await call(store.delete_interface_dataset, project, dataset_id):
            raise HTTPException(404, "数据集不存在")
        return {"deleted": True}

    @router.post("/interface-scenarios/{scenario_id}/data-execute", status_code=202)
    async def execute_dataset(scenario_id: str, payload: DatasetRunInput, request: Request):
        project, actor = scope(request)
        result = await call(store.enqueue_interface_dataset, project, scenario_id, payload.dataset_id, actor, payload.concurrency)
        manager._notify_workers(result["queued"])
        return result

    @router.post("/interface-scenarios/{scenario_id}/schedules", status_code=201)
    async def save_schedule(scenario_id: str, payload: ScheduleInput, request: Request):
        project, actor = scope(request)
        return await call(store.save_interface_schedule, project, scenario_id, payload.dataset_id, payload.interval_seconds, payload.concurrency, actor)

    @router.patch("/interface-schedules/{schedule_id}")
    async def toggle_schedule(schedule_id: str, payload: ScheduleToggleInput, request: Request):
        project, actor = scope(request)
        if not await call(store.change_interface_schedule, project, schedule_id, payload.enabled, actor):
            raise HTTPException(404, "计划不存在")
        return {"enabled": payload.enabled}

    @router.delete("/interface-schedules/{schedule_id}")
    async def delete_schedule(schedule_id: str, request: Request):
        project, actor = scope(request)
        if not await call(store.change_interface_schedule, project, schedule_id, None, actor):
            raise HTTPException(404, "计划不存在")
        return {"deleted": True}

    return router
