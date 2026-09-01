"""Artifact storage boundary with a local-filesystem implementation."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from auto_test.common.config_loader import artifact_storage_cfg
from auto_test.common.env import get_env


class ArtifactStorage(Protocol):
    backend: str
    root: Path

    def workspace(self, *parts: str) -> Path: ...
    def reference(self, path: str | Path) -> str: ...
    def resolve(self, reference: str | Path) -> Path: ...
    def resolve_file(self, reference: str | Path, *, container: str | Path | None = None) -> Path: ...
    def publish_tree(self, path: str | Path) -> list[str]: ...
    def materialize_tree(self, reference: str | Path) -> Path: ...


class LocalArtifactStorage:
    """Safe local storage used today and as the staging contract for MinIO."""

    backend = "local"

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe_parts(parts: tuple[str, ...]) -> list[str]:
        result: list[str] = []
        for value in parts:
            normalized = str(value).replace("\\", "/")
            parsed = [part for part in PurePosixPath(normalized).parts if part not in {"", ".", "/"}]
            if not parsed or any(part == ".." for part in parsed):
                raise ValueError(f"invalid artifact key: {value}")
            result.extend(parsed)
        return result

    def workspace(self, *parts: str) -> Path:
        path = self.root.joinpath(*self._safe_parts(tuple(parts))).resolve()
        path.relative_to(self.root)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def reference(self, path: str | Path) -> str:
        resolved = Path(path).resolve()
        resolved.relative_to(self.root)
        return str(resolved)

    def resolve(self, reference: str | Path) -> Path:
        value = Path(reference)
        resolved = value.resolve() if value.is_absolute() else (self.root / value).resolve()
        resolved.relative_to(self.root)
        return resolved

    def resolve_file(
        self, reference: str | Path, *, container: str | Path | None = None
    ) -> Path:
        path = self.resolve(reference)
        if container is not None:
            path.relative_to(self.resolve(container))
        if not path.is_file():
            raise FileNotFoundError(path)
        return path

    def publish_tree(self, path: str | Path) -> list[str]:
        root = self.resolve(path)
        if not root.is_dir():
            raise FileNotFoundError(root)
        return [self.reference(item) for item in sorted(root.rglob("*")) if item.is_file()]

    def materialize_tree(self, reference: str | Path) -> Path:
        path = self.resolve(reference)
        if not path.is_dir():
            raise FileNotFoundError(path)
        return path


class MinioArtifactStorage(LocalArtifactStorage):
    """MinIO storage with a local staging/cache directory for report builders."""

    backend = "minio"
    _BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")

    def __init__(
        self,
        root: str | Path,
        *,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        secure: bool = False,
        prefix: str = "liema",
        create_bucket: bool = False,
        client=None,
    ):
        super().__init__(root)
        self.endpoint = endpoint.strip().removeprefix("http://").removeprefix("https://").rstrip("/")
        self.bucket = bucket.strip().lower()
        if not self.endpoint:
            raise ValueError("artifact_storage.endpoint不能为空")
        if not self._BUCKET.fullmatch(self.bucket):
            raise ValueError("artifact_storage.bucket不是有效的MinIO bucket名称")
        normalized_prefix = prefix.strip().strip("/")
        if normalized_prefix and any(part in {".", ".."} for part in normalized_prefix.split("/")):
            raise ValueError("artifact_storage.prefix包含非法路径")
        self.prefix = normalized_prefix
        if client is None:
            try:
                from minio import Minio
            except ImportError as exc:
                raise RuntimeError("启用MinIO需要安装minio依赖") from exc
            client = Minio(
                self.endpoint,
                access_key=access_key,
                secret_key=secret_key,
                secure=bool(secure),
            )
        self.client = client
        if not self.client.bucket_exists(self.bucket):
            if not create_bucket:
                raise RuntimeError(f"MinIO bucket不存在：{self.bucket}")
            self.client.make_bucket(self.bucket)

    def _key_for_path(self, path: str | Path) -> str:
        relative = Path(path).resolve().relative_to(self.root).as_posix()
        return "/".join(part for part in (self.prefix, relative) if part)

    def _key_for_reference(self, reference: str | Path) -> str:
        value = str(reference).replace("\\", "/")
        prefix = f"minio://{self.bucket}/"
        if value.startswith(prefix):
            key = value[len(prefix):]
            if not key or any(part in {"", ".", ".."} for part in key.split("/")):
                raise ValueError("invalid MinIO artifact reference")
            if self.prefix and not key.startswith(self.prefix + "/"):
                raise ValueError("MinIO artifact reference is outside configured prefix")
            return key
        return self._key_for_path(super().resolve(reference))

    def _path_for_key(self, key: str) -> Path:
        relative = key[len(self.prefix) + 1:] if self.prefix else key
        path = (self.root / Path(*relative.split("/"))).resolve()
        path.relative_to(self.root)
        return path

    def reference(self, path: str | Path) -> str:
        return f"minio://{self.bucket}/{self._key_for_path(path)}"

    def resolve(self, reference: str | Path) -> Path:
        if str(reference).replace("\\", "/").startswith("minio://"):
            return self._path_for_key(self._key_for_reference(reference))
        return super().resolve(reference)

    def resolve_file(
        self, reference: str | Path, *, container: str | Path | None = None
    ) -> Path:
        path = self.resolve(reference)
        if container is not None:
            path.relative_to(self.resolve(container))
        if not path.is_file():
            key = self._key_for_reference(reference)
            path.parent.mkdir(parents=True, exist_ok=True)
            partial = path.with_name(path.name + ".part")
            try:
                self.client.fget_object(self.bucket, key, str(partial))
                partial.replace(path)
            except Exception:
                partial.unlink(missing_ok=True)
                raise FileNotFoundError(path)
        return path

    def publish_tree(self, path: str | Path) -> list[str]:
        root = self.resolve(path)
        if not root.is_dir():
            raise FileNotFoundError(root)
        references = []
        for item in sorted(root.rglob("*")):
            if not item.is_file() or item.name.endswith(".part"):
                continue
            key = self._key_for_path(item)
            self.client.fput_object(self.bucket, key, str(item))
            references.append(f"minio://{self.bucket}/{key}")
        return references

    def materialize_tree(self, reference: str | Path) -> Path:
        root = self.resolve(reference)
        key = self._key_for_reference(reference).rstrip("/")
        prefix = key + "/"
        root.mkdir(parents=True, exist_ok=True)
        found = False
        for item in self.client.list_objects(self.bucket, prefix=prefix, recursive=True):
            found = True
            object_key = str(item.object_name)
            relative = object_key[len(prefix):]
            if not relative or any(part in {"", ".", ".."} for part in relative.split("/")):
                continue
            target = (root / Path(*relative.split("/"))).resolve()
            target.relative_to(root)
            if target.is_file():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_name(target.name + ".part")
            try:
                self.client.fget_object(self.bucket, object_key, str(partial))
                partial.replace(target)
            except Exception:
                partial.unlink(missing_ok=True)
                raise
        if not found and not any(root.iterdir()):
            raise FileNotFoundError(reference)
        return root


def create_artifact_storage(
    base_dir: str | Path, overrides: dict[str, Any] | None = None
) -> LocalArtifactStorage | MinioArtifactStorage:
    values = dict(artifact_storage_cfg())
    values.update(overrides or {})
    backend = str(get_env("ARTIFACT_BACKEND", values.get("backend", "local"))).strip().lower()
    root_value = Path(str(values.get("root") or "artifacts"))
    root = root_value if root_value.is_absolute() else Path(base_dir).resolve() / root_value
    if backend == "local":
        return LocalArtifactStorage(root)
    if backend != "minio":
        raise ValueError(f"不支持的产物存储后端：{backend}")
    secure_value = str(get_env("MINIO_SECURE", str(values.get("secure", False))))
    return MinioArtifactStorage(
        root,
        endpoint=str(get_env("MINIO_ENDPOINT", str(values.get("endpoint") or ""))),
        access_key=str(get_env("MINIO_ACCESS_KEY", str(values.get("access_key") or ""))),
        secret_key=str(get_env("MINIO_SECRET_KEY", str(values.get("secret_key") or ""))),
        bucket=str(get_env("MINIO_BUCKET", str(values.get("bucket") or ""))),
        secure=secure_value.lower() in {"1", "true", "yes", "on"},
        prefix=str(values.get("prefix") or "liema"),
        create_bucket=bool(values.get("create_bucket", False)),
        client=values.get("client"),
    )
